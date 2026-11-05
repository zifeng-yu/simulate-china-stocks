"""Day ordering through public ledger APIs; no SQL or financial calculations."""

from china_stock_sim.common.event_accounting_scope import event_accounting_scope
from china_stock_sim.common.policies import policy_digest
from china_stock_sim.common.wire import to_wire
from china_stock_sim.market.contracts import session_date


def safe_error(exc):
    """Do not persist provider exception text, endpoint URLs or credentials."""
    from china_stock_sim.runtime.recording import record_exception
    record_exception(exc, component='workflow_ledger')
    return {'state': 'FAILED', 'reason': getattr(exc, 'code', 'STEP_FAILED'),
            'exception_type': type(exc).__name__}


class LedgerWorkflow:
    def __init__(self, settings, *, ledger, market, clock):
        self.settings, self.ledger, self.market, self.clock = settings, ledger, market, clock

    def write(self, method, account, payload, *, tag=None):
        day = payload.get('session', payload.get('effective_date', payload.get('target_session')))
        current = self.ledger.account_state(account, day)
        identity = policy_digest({'method': method, 'payload': payload, 'tag': tag})
        return getattr(self.ledger, method)(account, payload,
            operation_id='workflow:' + identity, expected_revision=current['account_revision'])

    def _evidence(self, code, day, operation, event_ids=None):
        cutoff = self.clock().isoformat()
        kwargs = {'code': code, 'session': day, 'knowledge_cutoff': cutoff,
                  'operation': operation}
        if event_ids:
            kwargs['event_ids'] = event_ids
        # Preserve the public query verbatim for the ledger's evidence verification.
        result = self.market.event_settlement(**kwargs)
        return result, {'code': code, 'effective_date': day, 'knowledge_cutoff': cutoff,
            'data_evidence': [{'method': 'event_settlement', 'kwargs': kwargs,
                               'result': to_wire(result.as_dict())}]}

    def _codes(self, account, day):
        required = self.ledger.required_market_inputs(account, day)
        historical = self.ledger.positions(account, day)
        return sorted({*required['holdings'], *required['pending_orders'],
                       *required['pending_actions'], *(p['code'] for p in historical)})

    def _register(self, account, day, *, include_today):
        results = []
        for code in self._codes(account, day):
            try:
                facts, _ = self._evidence(code, day, 'eligibility')
                eligible, observed = [], set()
                cases = self.ledger.corporate_action_cases(account, code=code)
                for event in facts.data or []:
                    scope = event_accounting_scope(event, day, cases)
                    if scope != 'REQUIRED':
                        observed.add(event['event_id'])
                        results.append({'state': 'SKIPPED', 'reason': 'NO_CURRENT_EVENT_OBLIGATION',
                                        'code': code, 'event_id': event['event_id'],
                                        'accounting_scope': scope})
                        continue
                    record = event.get('record_date')
                    if record and (record <= day if include_today else record < day):
                        aliases = {event['event_id'], *(e['event_id'] for e in event.get('evidence', []))}
                        existing = [c for c in cases if c.get('status') != 'MERGED'
                                    and aliases.intersection(c['event_ids'])]
                        derived = {'settlement_readiness', 'operation_date', 'operation_applicable'}
                        fact = {k: v for k, v in event.items() if k not in derived}
                        if len(existing) == 1:
                            case = existing[0]
                            original = {k: v for k, v in case['event'].items() if k not in derived}
                            if (case.get('actions', {}).get('eligibility', {}).get('state') in
                                    {'APPLIED', 'NOT_APPLICABLE'} and
                                    case.get('status') not in {'WAITING_DATA', 'IDENTITY_PENDING', 'REVISION_PENDING'}
                                    and to_wire(original) == to_wire(fact)):
                                continue
                        eligible.extend(event.get('event_ids') or [event['event_id']])
                if not eligible:
                    observation_only = (facts.state in {'READY', 'PARTIAL', 'WAITING_DATA'} and bool(facts.data)
                        and all(e['event_id'] in observed for e in facts.data)
                        and all(isinstance(issue, dict) and issue.get('event_id') in observed
                                for issue in facts.issues))
                    if facts.state not in {'READY', 'PARTIAL'} and not observation_only:
                        results.append({'state': 'WAITING_DATA', 'code': code,
                                        'reason': 'EVENT_ELIGIBILITY_UNAVAILABLE'})
                    continue
                _, payload = self._evidence(code, day, 'eligibility', sorted(set(eligible)))
                payload['event_ids'] = sorted(set(eligible))
                results.append(self.write('register_entitlements', account, payload))
            except Exception as exc:
                results.append({**safe_error(exc), 'code': code, 'step': 'eligibility'})
        return results

    def _actions(self, account, day, operations):
        results = []
        for case in self.ledger.corporate_action_cases(account):
            if case.get('status') in {'MERGED', 'NO_ENTITLEMENT'}:
                continue
            for operation in operations:
                if case.get('actions', {}).get(operation, {}).get('state') in {
                        'APPLIED', 'NOT_APPLICABLE'}:
                    continue
                try:
                    facts, payload = self._evidence(case['code'], day, operation, case['event_ids'])
                    rows = facts.data or []
                    # Known future dates are an ordinary pending receivable, not a failure.
                    if rows and all(e.get('operation_applicable') is False or
                            (e.get('operation_date') and e['operation_date'] > day) for e in rows):
                        continue
                    payload.update(case_id=case['case_id'], event_ids=case['event_ids'],
                                   operation=operation)
                    method = ('recognize_entitlements' if operation.endswith('accrual')
                              else 'settle_entitlements')
                    result = self.write(method, account, payload)
                    results.append({'case_id': case['case_id'], 'operation': operation,
                                    'result': result})
                except Exception as exc:
                    results.append({**safe_error(exc), 'case_id': case['case_id'],
                                    'operation': operation})
        return results

    def before_execution(self, account, day):
        """Never register today's holdings until all original orders are resolved."""
        day = session_date(day)
        return {'registrations': self._register(account, day, include_today=False),
                'actions': self._actions(account, day,
                    ('cash_accrual', 'share_accrual', 'share_delivery'))}

    def after_execution(self, account, day, *, orders_complete=True):
        day = session_date(day)
        results = {'registrations': (self._register(account, day, include_today=True)
                                    if orders_complete else []),
                   'actions': self._actions(account, day,
                       ('cash_accrual', 'share_accrual', 'cash_payment', 'share_delivery'))}
        if not orders_complete:
            results['registration_wait'] = 'TARGET_SESSION_ORDERS_UNRESOLVED'
        taxes = self.ledger.tax_items(account)
        if any(t.get('status') not in {'PAID', 'RESOLVED', 'NOT_TRIGGERED'} and
               (not t.get('due_date') or t['due_date'] <= day) for t in taxes):
            results['tax'] = self.write('record_tax', account, {'effective_date': day},
                tag=policy_digest(taxes))
        return results

    def close(self, account, day):
        payload = {'session': session_date(day), 'knowledge_cutoff': self.clock().isoformat()}
        # A further correction can arrive at the same recorded clock instant.
        # Bind the close to its financial revision, not only day and cutoff.
        receipt = self.write('close_session', account, payload,
                             tag=self.ledger.account_state(account, day)['account_revision'])
        return {'receipt': receipt, 'statement': self.ledger.daily_statement(account, day)}

    def missing_plan(self, account, day, reason='OFFLINE_NO_PREMARKET_PLAN'):
        if self.ledger.get_plan_header(account, target_session=day):
            return {'state': 'SKIPPED', 'reason': 'FROZEN_PLAN_EXISTS'}
        existing = self.ledger.no_plan_status(account, day)
        if existing:
            return {'state': 'ALREADY_APPLIED', 'no_plan': existing}
        return self.write('record_no_plan', account, {'target_session': day, 'reason': reason})
