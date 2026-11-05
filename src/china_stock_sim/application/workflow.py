"""One recoverable end-of-day cycle, using only public layer interfaces."""

import json
import os
from dataclasses import asdict
from datetime import datetime, timedelta
from time import monotonic
from uuid import uuid4

from china_stock_sim.application.execution import ExecutionApplication
from china_stock_sim.application.strategy import StrategyApplication
from china_stock_sim.application.workflow_data import WorkflowData
from china_stock_sim.application.workflow_ledger import LedgerWorkflow, safe_error
from china_stock_sim.application.workflow_observation import observed, stage_outcome
from china_stock_sim.common.policies import policy_digest
from china_stock_sim.common.strategy_definitions import get_definition
from china_stock_sim.ledger import LedgerService
from china_stock_sim.ledger.decision_evidence import verification_identity, verification_scope
from china_stock_sim.ledger.store import read_snapshot
from china_stock_sim.market.contracts import aware_time
from china_stock_sim.market.service import MarketDataService
from china_stock_sim.reporting.ledger import write_ledger_report
from china_stock_sim.runtime.recording import stage
from china_stock_sim.scheduler.planning import automatic_window, plan_schedule
from china_stock_sim.scheduler.process import probe_process
from china_stock_sim.scheduler.state import WorkflowJournal
from china_stock_sim.scheduler.workspace import writer_lock

SUCCESS = {'APPLIED', 'ALREADY_APPLIED', 'COMPLETE', 'READY', 'FROZEN', 'SKIPPED', 'UNCHANGED'}
STRATEGY_KEYS = {'S1': 's1', 'S1-LT': 's1_lt', 'LLM-Daily': 'llm'}


def compact(value):
    """Operational journal references authoritative records instead of copying them."""
    keys = {'state', 'status', 'reason', 'reasons', 'completion', 'completion_status',
            'plan_id', 'session', 'target_session', 'account_id', 'backup_id', 'manifest',
            'requests_this_run', 'next_retry_at', 'changed', 'exception_type', 'published',
            'accounting_status', 'valuation_status', 'budget_exhausted', 'cooldown'}
    keys.update({'failure_fingerprint', 'deterministic_failure', 'validation_metrics'})
    if not isinstance(value, dict):
        return {'state': 'FAILED', 'reason': 'INVALID_STEP_RESULT'}
    return {k: v for k, v in value.items() if k in keys}


class WorkflowApplication:
    def __init__(self, settings, *, market=None, ledger=None, clock=None, test_mode=False,
                 data=None, strategy=None, execution=None, renderer=None, journal=None):
        self.settings, self.root = settings, settings.root
        self.clock = clock or (lambda: datetime.now(settings.timezone))
        self.market = market or MarketDataService(self.root)
        self.ledger = ledger or LedgerService(settings, market=self.market,
                                             clock=clock, test_mode=test_mode)
        self.data = data or WorkflowData(settings, market=self.market, ledger=self.ledger,
                                         clock=self.clock)
        self.strategy = strategy or StrategyApplication(settings, market=self.market,
            ledger=self.ledger, clock=clock, test_mode=test_mode)
        self.execution = execution or ExecutionApplication(settings, market=self.market,
            ledger=self.ledger, clock=clock, test_mode=test_mode)
        self.days = LedgerWorkflow(settings, market=self.market, ledger=self.ledger,
                                    clock=self.clock)
        self.renderer = renderer or write_ledger_report
        self.journal = journal or WorkflowJournal(settings)
        self.run_id = None
        self.deadline = None
        self.steps = []
        self.observation_context = {}
        self.observing = False

    def _now(self):
        return aware_time(self.clock()).astimezone(self.settings.timezone)

    def _observe(self, phase, function, *, session=None, target_session=None, account_id=None):
        if not self.observing:
            return function()
        identity = {'session': session or self.observation_context.get('business_session'),
                    'target_session': target_session or self.observation_context.get('target_session'),
                    'account_id': account_id}
        self.journal.phase(self.run_id, phase, now=self._now(), state='RUNNING', **identity)
        try:
            result = stage('workflow.' + phase, function, run_id=self.run_id, **identity)
        except Exception as exc:
            self.journal.phase(self.run_id, phase, now=self._now(), state='FAILED',
                               result=safe_error(exc), **identity)
            raise
        observed_result = stage_outcome(phase, result)
        status = observed_result.get('state', 'COMPLETE')
        state = ('SKIPPED' if status == 'SKIPPED' else 'COMPLETE' if status in SUCCESS
                 else 'FAILED' if status in {'FAILED', 'ERROR', 'INTEGRITY_ERROR', 'INVALID_INPUT'}
                 else 'WAITING')
        summary = compact(observed_result)
        if len(json.dumps(summary, ensure_ascii=False, default=str)) > 18000:
            summary = {'state': status, 'reason': 'PHASE_DETAILS_TRUNCATED',
                       'next_retry_at': observed_result.get('next_retry_at')}
        self.journal.phase(self.run_id, phase, now=self._now(), state=state,
                           result=summary, **identity)
        return result

    @observed('calendar')
    def _schedule(self):
        facts = self.market.calendar(knowledge_cutoff=self._now().isoformat())
        config = self.settings.raw['schedule']
        plan = plan_schedule(self._now(), facts.data if facts.ready else [],
            daily_start=config['daily_start'], plan_cutoff=config['plan_cutoff'],
            awake_check_minutes=config['awake_check_minutes'])
        if self.observing:
            self.observation_context = {'business_session': plan.get('completed_session'),
                                        'target_session': plan.get('planning_target')}
            self.journal.context(self.run_id, now=self._now(),
                                 **self.observation_context)
        return plan

    def _accounts(self):
        return self.ledger.list_accounts()

    @observed('recovery')
    def _due(self, account, schedule):
        pending = self.ledger.workflow_status(account, through=schedule.get('completed_session'))
        start, end = pending.get('simulation_start_session'), schedule.get('completed_session')
        if not start or not end or start > end:
            return []
        if start not in schedule.get('trading_sessions', []):
            raise ValueError('观察起点的交易日历缺失，不能跳过历史账务')
        sessions = [d for d in schedule.get('trading_sessions', []) if start <= d <= end]
        boundaries = [d for d in (pending.get('restate_from'),
            *(o['target_session'] for o in pending['orders'])) if d and start <= d <= end]
        # Both accounting and valuation must have a continuous usable history.
        statements = {r['session']: r for r in pending['statements']}
        changes = (self.ledger.statement_input_changes(
            account, end, knowledge_cutoff=self._now().isoformat())
            if not self.observing or self._remaining() > 0 else {})
        if changes.get('affected_from'):
            boundaries.append(changes['affected_from'])
        for day in sessions:
            row = statements.get(day)
            if not row or row['accounting_status'] != 'COMPLETE' or row['valuation_status'] != 'FINAL':
                boundaries.append(day)
                break
        if not boundaries:
            return []
        return [d for d in sessions if d >= min(boundaries)]

    def inspect(self):
        schedule = self._schedule()
        rows = []
        for account in self._accounts():
            identity = account['account_id']
            try:
                pending = self.ledger.workflow_status(identity, through=schedule.get('completed_session'))
                policy_current, enabled = False, False
                target = schedule.get('planning_target')
                if target:
                    policy = self.ledger.get_account_policy(identity, target)
                    name = policy['policy']['strategy_id']
                    definition = get_definition(name, self.settings)
                    policy_current = (policy['policy']['strategy_version'] == definition['strategy_version']
                        and policy['definition']['implementation_hash'] == definition['implementation_hash'])
                    key = STRATEGY_KEYS.get(name)
                    enabled = bool(key and self.settings.raw['strategies'][key]['enabled'])
                rows.append({'account_id': identity,
                    'simulation_start_session': pending['simulation_start_session'],
                    'due_sessions': self._due(identity, schedule),
                    'settled_through': pending['settled_through'],
                    'valued_through': pending['valued_through'],
                    'report_pending': pending['report_pending'],
                    'pending_orders': len(pending['orders']),
                    'strategy_enabled': enabled, 'policy_current': policy_current})
            except Exception as exc:
                rows.append({'account_id': identity, **safe_error(exc)})
        operational = (schedule['state'] == 'READY' and self.settings.raw['market']['enabled']
            and self.settings.raw['execution']['enabled'] and any(r.get('simulation_start_session')
                and r.get('policy_current') and r.get('strategy_enabled') for r in rows))
        return {'state': schedule['state'], 'read_only': True, 'workflow_implemented': True,
                'configured_for_forward_workflow': operational,
                'schedule': {k: v for k, v in schedule.items() if k != 'trading_sessions'},
                'automatic_execution': automatic_window(self._now(), self.settings.raw['schedule']),
                'accounts': rows, 'configuration': {
                    'scheduled_enabled': self.settings.raw['schedule']['enabled'],
                    'market_enabled': self.settings.raw['market']['enabled'],
                    'execution_enabled': self.settings.raw['execution']['enabled'],
                    'strategy_enabled': {k: v['enabled'] for k, v in
                                         self.settings.raw['strategies'].items()}},
                'journal': self.journal.summary()}

    def _remaining(self):
        return max(0, self.deadline - monotonic())

    def _data_budget(self):
        return int(min(self._remaining(), self.settings.raw['market']['max_active_collection_seconds']))

    def _step(self, key, function, *, cooldown=True, fingerprint=None):
        now = self._now()
        prior = self.journal.get_task(key)
        fingerprint_function = fingerprint if callable(fingerprint) else None
        fingerprint = fingerprint_function() if fingerprint_function and self._remaining() > 0 else fingerprint
        if callable(fingerprint):
            fingerprint = None
        previous = prior.get('result', {}) if prior else {}
        if (fingerprint and previous.get('deterministic_failure')
                and previous.get('failure_fingerprint') == fingerprint):
            result = {'state': 'FAILED', 'reason': 'UNCHANGED_DETERMINISTIC_FAILURE',
                      'failure_fingerprint': fingerprint, 'deterministic_failure': True,
                      'exception_type': previous.get('exception_type')}
            self.steps.append({'step': key, **result})
            return result
        changed_failure = (fingerprint and previous.get('deterministic_failure')
                           and previous.get('failure_fingerprint') != fingerprint)
        if (cooldown and not changed_failure and prior and prior.get('next_retry_at')
                and aware_time(prior['next_retry_at']) > now):
            result = {'state': 'WAITING', 'reason': 'RETRY_COOLDOWN',
                      'next_retry_at': prior['next_retry_at'], 'cooldown': True}
            self.steps.append({'step': key, **result})
            return result
        if self._remaining() <= 0:
            result = {'state': 'WAITING', 'reason': 'RUN_BUDGET_EXHAUSTED', 'budget_exhausted': True}
            self.steps.append({'step': key, **result})
            return result
        self.journal.step(self.run_id, key, now=now, state='RUNNING', result={})
        try:
            result = function()
        except Exception as exc:
            result = safe_error(exc)
        if fingerprint and result.get('state') in {'FAILED', 'ERROR', 'INTEGRITY_ERROR', 'INVALID_INPUT'}:
            deterministic = (result.get('exception_type') in {
                'OverflowError', 'RecursionError', 'TypeError', 'KeyError', 'AssertionError'}
                or result.get('reason') in {'INTEGRITY_ERROR', 'INVALID_INPUT', 'RECORD_TOO_LARGE'})
            if deterministic:
                if fingerprint_function:
                    fingerprint = fingerprint_function()
                result = {**result, 'deterministic_failure': True, 'failure_fingerprint': fingerprint}
        status = result.get('state', 'FAILED')
        state = ('COMPLETE' if status in SUCCESS else 'FAILED' if status in {
            'FAILED', 'ERROR', 'INTEGRITY_ERROR', 'INVALID_INPUT'} else 'WAITING')
        retry = result.get('next_retry_at') if state in {'WAITING', 'FAILED'} else None
        if retry and aware_time(retry) <= self._now():
            retry = None
        if state in {'WAITING', 'FAILED'} and not retry:
            retry = (self._now() + timedelta(minutes=self.settings.raw['schedule'].get(
                'retry_minutes', 15))).isoformat()
        summary = compact(result)
        self.journal.step(self.run_id, key, now=self._now(), state=state, result=summary)
        self.journal.save_task(key, now=self._now(), state=state, result=summary, next_retry_at=retry)
        self.steps.append({'step': key, **summary})
        return result

    def _failure_fingerprint(self, account, day, action):
        """Only known, scoped dependencies may suppress a deterministic repeat."""
        fingerprinter = getattr(self.market, 'statement_input_fingerprint', None)
        if not callable(fingerprinter) or self._remaining() <= 0:
            return None
        try:
            revision = self.ledger.revision_state(account)
            inputs = self.ledger.required_market_inputs(account, day)
            codes = sorted(set(inputs['holdings'] + inputs['pending_orders'] + inputs['pending_actions']))
            if len(codes) > 1000:
                return None
            cutoff = self._now().isoformat()
            checks = [('calendar', {'start': day, 'end': day})]
            for code in codes:
                checks.extend((('prices', {'code': code, 'start': day, 'end': day}),
                               ('events', {'code': code}),
                               ('event_coverage', {'code': code, 'start': day, 'end': day})))
            fingerprints = []
            for method, kwargs in checks:
                value = fingerprinter(method, {**kwargs, 'knowledge_cutoff': cutoff})
                if value.get('state') != 'READY':
                    return None
                fingerprints.append(value['fingerprint'])
            return policy_digest({'account_id': account, 'session': day, 'action': action,
                'account_revision': revision['account_revision'],
                'restate_from': revision.get('restate_from'),
                'restate_through': revision.get('restate_through'),
                'inputs': fingerprints, 'implementation': verification_identity()})
        except Exception:
            return None

    @observed('data')
    def _collect(self, day, accounts, target, download, *, suffix=''):
        if not self.settings.raw['market']['enabled']:
            return {'state': 'SKIPPED', 'reason': 'MARKET_DISABLED_CACHE_ONLY'}
        try:
            pools = [self.market.pool(d, knowledge_cutoff=self._now().isoformat())
                     for d in dict.fromkeys((day, target or day))]
            codes = sorted({r['code'] for pool in pools if pool.ready for r in pool.data['selected']})
            candidates = codes if codes else None
            scope = self.data.build_request(day, accounts, target_session=target, candidates=candidates)
            fingerprint = policy_digest({'request': asdict(scope['request']),
                'pools': [{'state': p.state, 'observation_ids': list(p.observation_ids)} for p in pools]})
        except Exception as exc:
            failure = safe_error(exc)
            return self._step('data-scope:' + day, lambda: failure)
        # A changed pool, new account or longer future policy changes scope even after
        # a prior month-end task completed; frozen pool selection is still reused.
        def collect():
            result = self.data.collect(day, accounts, download=download, target_session=target,
                candidates=candidates, budget_seconds=self._data_budget())
            issues = [{'kind': 'POOL_QUERY_UNAVAILABLE', 'state': pool.state,
                       'issues': list(pool.issues)} for pool in pools if not pool.ready]
            if issues:
                result = {**result, 'state': 'PARTIAL', 'completion': False,
                          'reasons': [*result.get('reasons', []), *issues]}
            return result
        return self._step('data:' + day + ':' + fingerprint + suffix, collect)

    def _settle_day(self, account, day):
        def phase(name, function):
            if self._remaining() <= 0:
                return {'state': 'WAITING', 'reason': 'RUN_BUDGET_EXHAUSTED', 'budget_exhausted': True}
            return self._observe(name, function, session=day, account_id=account)
        before = phase('entitlements_before', lambda: self.days.before_execution(account, day))
        if before.get('budget_exhausted'):
            return before
        def execute():
            plan = self.ledger.get_plan_header(account, target_session=day)
            return (self.execution.settle(account, plan['plan_id'],
                operation_id='workflow-execution:' + policy_digest({'account': account, 'day': day}),
                knowledge_cutoff=self._now().isoformat(),
                should_continue=lambda: self._remaining() > 0) if plan else
                self.days.missing_plan(account, day))
        execution = phase('execution', execute)
        if execution.get('budget_exhausted'):
            return execution
        unresolved = [o for o in self.ledger.workflow_status(account, through=day)['orders']
                      if o['target_session'] <= day]
        after = phase('entitlements_after', lambda: self.days.after_execution(
            account, day, orders_complete=not unresolved))
        if after.get('budget_exhausted'):
            return after
        closed = phase('close', lambda: self.days.close(account, day))
        if closed.get('budget_exhausted'):
            return closed
        statement = closed['statement']
        complete = bool(statement and statement['accounting_status'] == 'COMPLETE'
                        and statement['valuation_status'] == 'FINAL')
        return {'state': 'COMPLETE' if complete else 'WAITING_DATA', 'account_id': account,
                'session': day, 'accounting_status': (statement or {}).get('accounting_status'),
                'valuation_status': (statement or {}).get('valuation_status'),
                'before': before, 'execution': execution, 'after': after, 'close': closed}

    @observed('input_review')
    def _refresh_inputs(self, account, day):
        return self._step('statement-inputs:' + day + ':' + account,
            lambda: self.days.write('refresh_statement_inputs', account, {
                'session': day, 'knowledge_cutoff': self._now().isoformat()},
                tag=self.ledger.revision_state(account)['account_revision']))

    def _publish(self):
        snapshot = stage('report.snapshot', lambda: read_snapshot(self.settings),
                         run_id=self.run_id)
        outcome = stage('report.render', lambda: self.renderer(
            self.settings, snapshot, ledger=self.ledger), run_id=self.run_id)
        acknowledgments = []
        for item in outcome['published']:
            pending = self.ledger.revision_state(item['account_id'])
            if not pending['report_pending']:
                continue
            receipt = self.ledger.record_step_result(item['account_id'], {
                'effective_date': self._now().date().isoformat(), 'step': 'report',
                'status': 'SUCCEEDED', 'rendered_revision': item['report_revision']},
                operation_id=f'published:{item["report_revision"]}',
                expected_revision=item['account_revision'])
            acknowledgments.append(receipt)
        return {'state': 'COMPLETE' if all(r['state'] in SUCCESS for r in acknowledgments)
                else 'WAITING', 'published': outcome['published']}

    @observed('report_daily')
    def _report(self, *, force=False, phase='report_daily'):
        if self._remaining() <= 0:
            return self._step('report', self._publish)
        pending = False
        for account in self._accounts():
            try:
                pending |= self.ledger.revision_state(account['account_id'])['report_pending']
            except Exception:
                pending = True
        if force or pending or not self.settings.report.is_file():
            return self._step('report', self._publish)
        return {'state': 'SKIPPED', 'reason': 'CURRENT_REPORT_ALREADY_PUBLISHED'}

    def _plan(self, account, schedule):
        if schedule['state'] != 'READY':
            return {'state': 'WAITING_CALENDAR', 'reason': 'PLANNING_CALENDAR_NOT_READY'}
        target = schedule.get('planning_target')
        if not target:
            return {'state': 'SKIPPED', 'reason': 'NO_KNOWN_NEXT_SESSION'}
        start = self.ledger.revision_state(account)['simulation_start_session']
        if not start or target < start:
            return {'state': 'SKIPPED', 'reason': 'ACCOUNT_NOT_ACTIVATED'}
        if not schedule.get('plan_allowed'):
            existing = self.ledger.get_strategy_decision(account, target_session=target)
            if existing and existing['state'] not in {'FROZEN', 'MISSED_CUTOFF', 'CONFIG_CHANGED',
                    'STALE_INPUT', 'MODEL_FAILED', 'INVALID'}:
                return self.strategy.recover(account, target)
            return self.days.missing_plan(account, target, 'MISSED_CUTOFF')
        policy = self.ledger.get_account_policy(account, target)['policy']
        key = STRATEGY_KEYS.get(policy['strategy_id'])
        if not key or not self.settings.raw['strategies'][key]['enabled']:
            return {'state': 'SKIPPED', 'reason': 'STRATEGY_DISABLED'}
        return self.strategy.recover(account, target)

    @observed('backup')
    def _backup(self, day):
        if self._remaining() <= 0:
            return self._step('backup:' + day, lambda: None)
        versions = []
        for account in self._accounts():
            snapshot = stage('backup.snapshot', lambda: self.ledger.revision_state(
                account['account_id']), run_id=self.run_id,
                account_id=account['account_id'], session=day)
            versions.append([account['account_id'], snapshot['account_revision'],
                             snapshot['report_revision']])
        key = 'backup:' + day + ':' + policy_digest(versions)
        previous = self.journal.get_task(key)
        prior_verification = None
        if previous and previous['state'] == 'COMPLETE':
            if self._remaining() <= 0:
                return self._step(key, lambda: None)
            prior_verification = stage('backup.verify', lambda: self.ledger.verify_backup(
                previous['result'].get('backup_id')), run_id=self.run_id, session=day)
            if prior_verification['state'] == 'READY':
                return {'state': 'SKIPPED', 'reason': 'COMPLETE_BACKUP_ALREADY_EXISTS'}
        def backup():
            result = stage('backup.write', lambda: self.ledger.backup(label='daily'),
                           run_id=self.run_id, session=day)
            if prior_verification:
                result = {**result, 'reasons': [{'kind': 'PREVIOUS_BACKUP_UNAVAILABLE',
                    'state': prior_verification['state'],
                    'backup_id': previous['result'].get('backup_id')}]}
            return result
        return self._step(key, backup)

    def run(self, *, download=False, scheduled=False):
        config = self.settings.raw
        if scheduled and not config['schedule']['enabled']:
            return {'state': 'DISABLED', 'reason': 'SCHEDULE_DISABLED'}
        window = automatic_window(self._now(), config['schedule'])
        if scheduled and window['quiet']:
            # CLI runtime observations retain the skipped trigger; no business
            # journal, task/cooldown, account read, lock, or financial work occurs.
            return {'state': 'SKIPPED', 'reason': 'SCHEDULE_QUIET_WINDOW',
                    'next_retry_at': window['next_allowed_at'], 'changed': False}
        if not any([config['market']['enabled'], config['execution']['enabled'],
                    *(s['enabled'] for s in config['strategies'].values())]):
            return {'state': 'DISABLED', 'reason': 'ALL_WORKFLOW_COMPONENTS_DISABLED'}
        with writer_lock(self.settings):
            self.run_id, self.steps = uuid4().hex, []
            self.deadline = monotonic() + config['schedule'].get('max_run_seconds', 3600)
            self.journal.start(self.run_id, now=self._now(),
                               metadata={'download': download, 'scheduled': scheduled,
                                         'process_birth': probe_process(os.getpid()).get('birth')})
            self.observing = True
            try:
                with verification_scope(self.root, persist=True) as verification_metrics:
                    result = self._run(download)
                result = {**result, 'validation_metrics': dict(verification_metrics)}
            except Exception as exc:
                result = safe_error(exc)
            state = 'COMPLETE' if result['state'] in SUCCESS else 'PARTIAL'
            self.journal.finish(self.run_id, now=self._now(), state=state, result=compact(result))
            self.observing = False
            return {**result, 'run_id': self.run_id, 'steps': self.steps}

    def _run(self, download):
        schedule = self._schedule()
        if schedule['state'] != 'READY' and self.settings.raw['market']['enabled']:
            self._step('calendar', lambda: self.data.ensure_calendar(
                self._now().date().isoformat(), download=download,
                budget_seconds=self._data_budget()))
            schedule = self._schedule()
        day = schedule.get('completed_session')
        if schedule['state'] != 'READY' or not day:
            self._report()
            return {'state': 'WAITING_DATA', 'reason': 'CALENDAR_NOT_READY'}
        accounts, account_errors, due = [], {}, {}
        for row in self._accounts():
            account = row['account_id']
            try:
                if self.ledger.pending_work(account)['simulation_start_session']:
                    accounts.append(account)
            except Exception as exc:
                account_errors[account] = safe_error(exc)
                self._step('account-inspect:' + account,
                           lambda a=account: account_errors[a])
        target = schedule.get('planning_target')
        self._collect(day, accounts, target, download)
        month = schedule.get('last_completed_month_end')
        if month and self.settings.raw['market']['enabled']:
            key = 'month-end:' + month
            previous = self.journal.get_task(key)
            if not previous or previous['state'] != 'COMPLETE':
                month_result = self._observe('month_end', lambda: self._step(key,
                    lambda: self.data.refresh_month_end(month, accounts=accounts,
                        download=download, budget_seconds=self._data_budget())),
                    session=month, target_session=target)
                if month_result.get('completion') and month != day:
                    self._collect(day, accounts, target, download, suffix=':new-pool')
        # Newly collected facts can invalidate an already FINAL statement. Persist
        # that boundary before bounded recovery so a restart cannot lose later days.
        for account in accounts:
            try:
                if self.settings.raw['execution']['enabled']:
                    refreshed = self._refresh_inputs(account, day)
                    if refreshed['state'] not in SUCCESS:
                        account_errors[account] = refreshed
                        continue
                due[account] = self._due(account, schedule)
            except Exception as exc:
                account_errors[account] = safe_error(exc)
                self._step('account-inputs:' + account, lambda a=account: account_errors[a])
        blocked = set(account_errors)
        maximum = self.settings.raw['schedule'].get('max_sessions_per_run', 20)
        all_days = sorted({d for dates in due.values() for d in dates})
        if self.settings.raw['execution']['enabled']:
            for old_day in all_days[:maximum]:
                active = [a for a in accounts if a not in blocked and old_day in due.get(a, [])]
                if not active or self._remaining() <= 0:
                    continue
                if old_day != day:
                    self._collect(old_day, active, target, download)
                for account in active:
                    # Historical supplementation can discover a revision after the
                    # first scan. Save its full range before replacing this day's
                    # statement, including on the last day of this run's budget.
                    if old_day != day and self.settings.raw['market']['enabled']:
                        refreshed = self._refresh_inputs(account, day)
                        if refreshed['state'] not in SUCCESS:
                            account_errors[account] = refreshed
                            blocked.add(account)
                            continue
                    result = self._observe('settlement', lambda a=account, d=old_day:
                        self._step('settle:' + d + ':' + a, lambda: self._settle_day(a, d),
                            fingerprint=lambda: self._failure_fingerprint(a, d, 'settle')),
                        session=old_day, account_id=account)
                    if result['state'] != 'COMPLETE':
                        blocked.add(account)
        self._report()
        # Use a fresh real clock after data/settlement: crossing 09:00 cannot backdate plans.
        schedule = self._schedule()
        if schedule['state'] != 'READY':
            self._step('planning-calendar', lambda: {
                'state': 'WAITING_CALENDAR', 'reason': 'PLANNING_CALENDAR_NOT_READY'})
        def model_last(account):
            try:
                return self.ledger.get_account_policy(account,
                    schedule['planning_target']).get('definition', {}).get('uses_model', True)
            except Exception:
                return True
        for account in sorted(accounts if schedule['state'] == 'READY' else [], key=model_last):
            try:
                if account in account_errors:
                    continue
                if self._due(account, schedule):
                    continue
                planning = self._schedule()
                self._observe('plan', lambda a=account, s=planning: self._step(
                    'plan:' + str(s.get('planning_target')) + ':' + a,
                    lambda: self._plan(a, s)), session=planning.get('completed_session'),
                    target_session=planning.get('planning_target'), account_id=account)
            except Exception as exc:
                account_errors[account] = safe_error(exc)
                self._step('account-plan:' + account, lambda a=account: account_errors[a])
        self._report(phase='report_plan')
        if accounts:
            try:
                self._backup(day)
            except Exception as exc:
                error = safe_error(exc)
                self._step('backup-input', lambda: error)
        reasons = [{'kind': 'ACCOUNT_UNAVAILABLE', 'account_id': a} for a in account_errors]
        for account in accounts:
            try:
                if self._due(account, schedule):
                    reasons.append({'kind': 'ACCOUNT_RECOVERY_PENDING', 'account_id': account})
            except Exception:
                reasons.append({'kind': 'ACCOUNT_UNAVAILABLE', 'account_id': account})
        exhausted = self._remaining() <= 0 or len(all_days) > maximum
        waiting = bool(reasons or exhausted or any(step.get('state') not in SUCCESS for step in self.steps))
        return {'state': 'PARTIAL' if waiting else 'COMPLETE', 'session': day,
                'target_session': schedule.get('planning_target'),
                'budget_exhausted': exhausted, 'reasons': reasons}
