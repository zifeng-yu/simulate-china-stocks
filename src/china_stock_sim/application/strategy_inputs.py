"""Read-only strategy input assembly through public market and ledger contracts."""

from datetime import datetime, time
from zoneinfo import ZoneInfo

from china_stock_sim.common.policies import policy_digest
from china_stock_sim.common.strategy_definitions import get_definition, normalize_parameters
from china_stock_sim.common.wire import to_wire
from china_stock_sim.ledger.kernel import LedgerError
from china_stock_sim.market.contracts import aware_time, session_date
from china_stock_sim.strategies.base import PlanningContext

SHANGHAI = ZoneInfo('Asia/Shanghai')


def context_from_bundle(bundle):
    values = dict(bundle['context'])
    values['task_sessions'] = tuple(values['task_sessions'])
    return PlanningContext(**values, input_bundle=bundle)


def assemble_inputs(settings, market, ledger, account_id, target_session, now,
                    knowledge_cutoff=None):
    """No freeze, downloads, model calls, activation or writes, even for missing inputs."""
    target = session_date(target_session)
    now = aware_time(now)
    cutoff = aware_time(knowledge_cutoff or now)
    deadline = datetime.combine(datetime.fromisoformat(target).date(), time(9), SHANGHAI)
    if cutoff > now:
        return {'state': 'INVALID_INPUT', 'reasons': [{'kind': 'FUTURE_KNOWLEDGE_CUTOFF'}]}
    if now >= deadline:
        return {'state': 'MISSED_CUTOFF', 'reasons': [{'kind': 'MISSED_CUTOFF'}]}
    evidence, required = [], []
    read_results = {}

    def query(method, *, prepared_inputs=None, **kwargs):
        kwargs['knowledge_cutoff'] = cutoff.isoformat()
        call_kwargs = kwargs if prepared_inputs is None else {
            **kwargs, 'prepared_inputs': prepared_inputs}
        result = getattr(market, method)(**call_kwargs)
        wire = to_wire(result.as_dict())
        evidence.append({'method': method, 'kwargs': kwargs, 'result': wire})
        read_results[method] = result
        return wire

    day_result = query('calendar_operation', operation='previous_session', day=target)
    calendar = query('calendar', start=target, end=target)
    if day_result['state'] != 'READY' or calendar['state'] != 'READY':
        return {'state': 'WAITING_INPUT', 'reasons': [{'kind': 'CALENDAR_UNAVAILABLE'}],
                'data_evidence': evidence}
    rows = calendar['data']
    if len(rows) != 1 or str(rows[0].get('is_trading_day')) not in {'1', 'True'}:
        return {'state': 'INVALID_INPUT', 'reasons': [{'kind': 'TARGET_NOT_TRADING_SESSION'}]}
    day = day_result['data']
    if cutoff < datetime.fromisoformat(day + 'T18:10:00+08:00'):
        return {'state': 'WAITING_INPUT', 'reasons': [{'kind': 'MARKET_DAY_NOT_COMPLETE'}]}
    snapshot = ledger.report_snapshot(account_id, target, mode='recent')
    account = snapshot['account']
    policy_row = ledger.get_account_policy(account_id, target)
    definition = get_definition(account['strategy_id'], settings)
    if (policy_row['definition_status'] != 'REGISTERED'
            or any(policy_row['definition'].get(k) != definition.get(k)
                   for k in ('strategy_version', 'implementation_hash', 'uses_model'))):
        return {'state': 'CONFIG_CHANGED', 'account_id': account_id, 'reasons': [{
            'kind': 'FUTURE_POLICY_UPDATE_REQUIRED',
            'action': 'Use explicit account-policy-set for a future target session; historical policy stays unchanged'}]}
    policy = policy_row['policy']
    parameters = normalize_parameters(definition, policy['parameters'])
    if not account['can_plan']:
        return {'state': 'WAITING_INPUT', 'account_id': account_id,
                'reasons': account['blocking_reasons'], 'account_revision': account['account_revision']}
    statements = [s for s in snapshot['statements'] if s['session'] == day]
    if statements:
        statement = max(statements, key=lambda s: s.get('statement_revision', 0))
        if (statement['accounting_status'] != 'COMPLETE' or statement['valuation_status'] != 'FINAL'
                or type(statement.get('net_assets_fen')) is not int):
            return {'state': 'WAITING_INPUT', 'reasons': [{'kind': 'FINAL_NAV_REQUIRED'}]}
        assets = statement['net_assets_fen']
        nav_evidence = statement
    elif (account['simulation_start_session'] == target and not account['positions']
          and not snapshot['orders'] and not snapshot['entitlements'] and not snapshot['cases']):
        assets = account['cash_fen']
        nav_evidence = {'basis': 'INITIAL_CONFIRMED_CASH', 'cash_fen': assets}
    else:
        return {'state': 'WAITING_INPUT', 'reasons': [{'kind': 'FINAL_NAV_REQUIRED'}]}
    uses_model = definition['uses_model']
    length = 21 if uses_model else parameters['lookback_sessions']
    window = query('calendar_operation', operation='window', day=day, size=length)
    signal_window = read_results['calendar_operation']
    eligibility_window = query('calendar_operation', operation='window', day=day, size=121)
    pool = query('pool', as_of=target)
    month = query('calendar_operation', operation='is_month_end', day=day)
    if any(v['state'] != 'READY' for v in (window, eligibility_window, pool, month)):
        return {'state': 'WAITING_INPUT', 'reasons': [{'kind': 'POOL_OR_WINDOW_UNAVAILABLE'}],
                'data_evidence': evidence}
    members = {s['code']: s for s in pool['data'].get('selected', [])}
    tasks = [t for t in snapshot['rebalance_tasks'] if t['status'] == 'ACTIVE'
             and target <= t['end_session']]
    if len(tasks) > 1:
        return {'state': 'INVALID', 'reasons': [{'kind': 'MULTIPLE_ACTIVE_PARENT_TASKS'}]}
    parent = tasks[0] if tasks else None
    if parent and (parent.get('policy_hash') != policy_row['policy_hash']
                   or parent.get('strategy_implementation_hash') != definition['implementation_hash']):
        try:
            compatible = ledger.get_rebalance_policy_compatibility(account_id, parent['task_id'], target)
            parent = {**parent, 'policy_transition': compatible['transition']}
        except LedgerError as exc:
            return {'state': 'CONFIG_CHANGED', 'reasons': [{'kind': 'PARENT_TASK_IDENTITY_CHANGED',
                                                           'details': exc.as_dict()}]}
    task_sessions = [target]
    if not uses_model and parent is None:
        for _ in range(parameters['rebalance_window_sessions'] - 1):
            nxt = query('calendar_operation', operation='next_session', day=task_sessions[-1])
            if nxt['state'] != 'READY':
                required.append({'kind': 'PARENT_WINDOW_CALENDAR_UNAVAILABLE'})
                break
            task_sessions.append(nxt['data'])
    elif parent:
        task_sessions = parent['sessions']
    held = {p['code'] for p in account['positions']}
    codes = sorted(set(members) | held | {t['code'] for t in (parent or {}).get('targets', [])})
    securities = {}
    for code in codes:
        security = query('security', code=code)
        prices = query('prices', code=code, start=window['data'][0], end=day)
        coverage = query('event_coverage', code=code, start=window['data'][0], end=day)
        events = query('events', code=code)
        signals = query('signal_prices', code=code, as_of=day, size=length,
            prepared_inputs={'code': code, 'as_of': day, 'size': length,
                'knowledge_cutoff': cutoff.isoformat(), 'window': signal_window,
                **{key: read_results[key] for key in ('prices', 'events', 'event_coverage')}})
        facts = security['data'] if isinstance(security['data'], dict) else {}
        bars = prices['data'] if isinstance(prices['data'], list) else []
        last = next((p for p in reversed(bars) if p.get('date') == day), {})
        event_rows = events['data'] if isinstance(events['data'], list) else []
        complex_actions = [event for event in event_rows if isinstance(event, dict)
            and event.get('kind') == 'COMPLEX' and event.get('status') != 'CANCELLED'
            and not event.get('conflicts')
            and (event.get('ex_date') is None or event['ex_date'] >= day)]
        known_excluded = (code not in members or facts.get('source_status') == 0
                          or facts.get('status') in {'delisted', 'DELISTED'}
                          or (facts.get('delisted') and facts['delisted'] <= day)
                          or (facts.get('board') and facts['board'] not in {'沪市主板', '深市主板', 'MAIN'})
                          or (facts.get('listed') and facts['listed'] > eligibility_window['data'][0])
                          or last.get('isST') == 1 or bool(complex_actions))
        meta_ok = (security['state'] == 'READY'
                   and facts.get('board') in {'沪市主板', '深市主板', 'MAIN'}
                   and facts.get('listed') and facts['listed'] <= eligibility_window['data'][0])
        usable = (meta_ok and prices['state'] == 'READY' and coverage['state'] == 'READY'
                  and signals['state'] == 'READY' and len(signals['data']) == length
                  and len(bars) == length and last.get('isST') == 0)
        issues = [{'query': name, 'state': result['state'], 'issues': result['issues']}
                  for name, result in [('security', security), ('prices', prices),
                                       ('event_coverage', coverage), ('events', events),
                                       ('signal_prices', signals)] if result['state'] != 'READY']
        if not meta_ok:
            issues.append({'kind': 'SECURITY_ELIGIBILITY_UNCONFIRMED'})
        if complex_actions:
            issues.append({'kind': 'KNOWN_COMPLEX_ACTION',
                           'event_ids': [e.get('event_id') for e in complex_actions]})
        eligibility = 'EXCLUDED' if known_excluded else 'READY' if usable else 'UNAVAILABLE'
        securities[code] = {'code': code, 'name': facts.get('name', members.get(code, {}).get('name', code)),
            'in_pool': code in members, 'eligibility': eligibility, 'issues': issues,
            'adjusted_close': [row['adjusted_close'] for row in signals['data']]
                if signals['state'] == 'READY' else [],
            'raw_bars': bars, 'close': last.get('close')}
    initial_evaluated = ledger.initial_strategy_evaluated(account_id, target)
    values = {'account_id': account_id, 'strategy_id': account['strategy_id'],
        'target_session': target, 'market_as_of': day, 'input_cutoff': cutoff.isoformat(),
        'generated_at': now.isoformat(), 'account': account, 'policy': policy,
        'net_assets_fen': assets, 'securities': securities, 'fee_policy': settings.raw['fees'],
        'parent_task': parent, 'initial_evaluated': initial_evaluated,
        'month_end': bool(month['data']), 'task_sessions': task_sessions}
    bundle = {'schema_version': 1, 'account_revision': account['account_revision'],
        'account_state_id': account['state_id'], 'market_snapshot_ids': [],
        'data_evidence': evidence, 'context': values, 'nav_evidence': nav_evidence,
        'definition': definition, 'policy_hash': policy_row['policy_hash'],
        'required_history_sessions': max(121, length)}
    return {'state': 'WAITING_INPUT' if required else 'READY', 'account_id': account_id,
            'input_bundle': bundle, 'input_hash': policy_digest(bundle), 'reasons': required,
            'required_inputs': required}


def collection_requirements(ledger, account_ids, target_session):
    """Every future collection call must retain the largest active account window."""
    rows = [ledger.get_account_policy(account, target_session)['policy'] for account in account_ids]
    return {'history_sessions': max([121, *[p['parameters'].get('lookback_sessions', 21)
                                           for p in rows]]), 'accounts': list(account_ids)}
