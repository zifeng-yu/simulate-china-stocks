"""Independent date/process/evidence acceptance for the lightweight run homepage."""

import hashlib
import json
import sqlite3
from copy import deepcopy
from datetime import datetime, timedelta
from importlib.util import find_spec

import pytest

from china_stock_sim.ledger.monitoring import read_runtime_facts
from china_stock_sim.ledger.store import initialize
from china_stock_sim.scheduler.state import WorkflowJournal
from china_stock_sim.scheduler.status import build_status
from china_stock_sim.scheduler.workspace import writer_lock

TODAY = '2026-11-02'
PREVIOUS = '2026-10-30'
NEXT = '2026-11-03'
CALENDAR = [{'date': day, 'is_trading_day': flag} for day, flag in (
    ('2026-10-29', '1'), ('2026-10-30', '1'), ('2026-10-31', '0'),
    ('2026-11-01', '0'), ('2026-11-02', '1'), ('2026-11-03', '1'),
    ('2026-11-04', '1'), ('2026-11-05', '1'), ('2026-11-06', '1'))]


@pytest.fixture
def home_settings(settings):
    for key in ('market', 'execution', 'schedule'):
        settings.raw[key]['enabled'] = True
    for row in settings.raw['strategies'].values():
        row['enabled'] = True
    return settings


def account_facts(*, start=TODAY, frozen=True):
    accounts = []
    for identity, strategy in [('S1', 'S1'), ('S1-LT', 'S1-LT'), ('LLM', 'LLM-Daily')]:
        rows = {day: {'display_name': identity, 'plan': None, 'statement': None,
                      'order_count': 0, 'pending_order_count': 0, 'no_plan_reason': None}
                for day in (PREVIOUS, TODAY, NEXT)}
        if frozen:
            rows[TODAY]['plan'] = {'plan_id': 'plan:' + identity, 'target_session': TODAY,
                'status': 'FROZEN', 'frozen_at': '2026-11-01T21:00:00+08:00', 'order_count': 2}
        accounts.append({'account_id': identity, 'strategy_id': strategy, 'state': 'READY',
            'simulation_start_session': start, 'account_revision': 2, 'report_revision': 4,
            'last_rendered_revision': 4, 'settled_through': None, 'valued_through': None,
            'restate_from': None, 'sessions': rows})
    return {'state': 'READY', 'accounts': accounts, 'accounts_truncated': False, 'issues': []}


def journal_at(now, phases=(), *, state='RUNNING', session=TODAY, target=NEXT, birth='born-1'):
    now = datetime.fromisoformat(now)
    entries = []
    for index, value in enumerate(phases):
        entries.append({'phase': value['phase'], 'session': session, 'target_session': target,
            'account_id': None, 'recorded_at': (now - timedelta(seconds=60-index)).isoformat(),
            'result': {}, **value})
    attempt = {'state': state, 'attempt': 1, 'pid': 123,
               'started_at': (now - timedelta(seconds=120)).isoformat(),
               'finished_at': None if state == 'RUNNING' else (now - timedelta(seconds=1)).isoformat(),
               'metadata': {'business_session': session, 'target_session': target, 'process_birth': birth},
               'result': {'session': session, 'target_session': target}, 'phases': entries, 'steps': []}
    return {'schema_version': 1, 'state': state, 'current': 'run-1',
            'runs': {'run-1': {'run_id': 'run-1', 'state': state, 'attempts': [attempt]}}, 'tasks': {}}


def project_status(settings, *, now='2026-11-02T10:00:00+08:00', facts=None, journal=None,
                   calendar=None, process_probe=None):
    return build_status(settings, now=datetime.fromisoformat(now),
        ledger_facts=account_facts() if facts is None else facts,
        journal={'schema_version': 1, 'state': 'NOT_STARTED', 'runs': {}, 'tasks': {}}
        if journal is None else journal,
        calendar=deepcopy(CALENDAR) if calendar is None else calendar,
        process_probe=process_probe or (lambda pid: {'state': 'ALIVE', 'birth': 'born-1'}),
        scheduler_info={'workflow': {'state': 'INSTALLED', 'enabled': True, 'installed': True,
            'loaded': True, 'file_matches': True}, 'monitor': {'state': 'NOT_INSTALLED'}})


def nodes(result):
    return {row['id']: row for row in result['nodes']}


def test_home_projects_upload_slot_without_modifying_business_cutoff(home_settings, monkeypatch):
    if find_spec('china_stock_sim.publishing') is None:
        pytest.skip('Local publishing component is not installed')
    publication = {'state': 'COMPLETE', 'scheduled_for': '2026-11-02T09:00:00+08:00',
                   'last_success_slot': '2026-11-02T09:00:00+08:00',
                   'next_due_at': '2026-11-02T21:00:00+08:00',
                   'schedule': 'Asia/Shanghai 09:00 and 21:00 daily'}
    monkeypatch.setattr('china_stock_sim.publishing.state.read_publication_status',
                        lambda *args, **kwargs: publication)
    value = project_status(home_settings)
    assert value['publication'] == publication
    assert nodes(value)['morning_plan']['deadline_at'] == '2026-11-02T09:00:00+08:00'


def final_statement(facts, *, day=TODAY):
    for row in facts['accounts']:
        row['settled_through'] = row['valued_through'] = day
        row['sessions'][day]['statement'] = {'accounting_status': 'COMPLETE',
            'valuation_status': 'FINAL', 'processed_at': day + 'T18:20:00+08:00',
            'statement_revision': 1, 'requires_restatement': False}


def test_old_completed_cycle_cannot_green_today_or_select_wrong_next_plan(home_settings):
    journal = journal_at('2026-11-02T08:00:00+08:00',
        [{'phase': name, 'state': 'COMPLETE'} for name in ('data', 'settlement', 'backup')],
        state='COMPLETE', session=PREVIOUS, target=TODAY)
    result = project_status(home_settings, journal=journal)
    assert result['summary']['state'] == 'SCHEDULED'
    assert result['current_run']['state'] == 'COMPLETE'
    assert result['current_run']['session'] == PREVIOUS
    assert result['calendar']['completed_session'] == PREVIOUS
    assert nodes(result)['morning_plan']['state'] == 'COMPLETE'
    assert nodes(result)['next_plan']['target_session'] == NEXT
    for key in ('data', 'input_review', 'settlement', 'report_daily', 'next_plan', 'report_plan', 'backup'):
        assert nodes(result)[key]['state'] == 'SCHEDULED', key
    assert nodes(result)['month_end']['state'] == 'NOT_REQUIRED'


@pytest.mark.parametrize(('at', 'expected'), [('18:09:59', 'SCHEDULED'), ('18:10:00', 'SCHEDULED'),
                                            ('19:00:00', 'PENDING')])
def test_real_1810_boundary(home_settings, at, expected):
    result = project_status(home_settings, now=TODAY + 'T' + at + '+08:00')
    assert nodes(result)['data']['state'] == expected
    assert nodes(result)['data']['session'] == TODAY
    assert nodes(result)['data']['scheduled_at'] == TODAY + 'T18:10:00+08:00'
    if at < '19:00:00':
        assert result['automatic_execution']['quiet'] is True
        assert nodes(result)['data']['next_retry_at'] == TODAY + 'T19:00:00+08:00'
        assert result['summary']['state'] == 'SCHEDULED'


def test_quiet_window_does_not_shorten_source_retry_after(home_settings):
    journal = journal_at(TODAY + 'T18:30:00+08:00', [
        {'phase': 'data', 'state': 'WAITING', 'result': {
            'reason': 'RETRY_COOLDOWN', 'next_retry_at': TODAY + 'T19:30:00+08:00'}}],
        state='PARTIAL')
    result = project_status(home_settings, now=TODAY + 'T18:45:00+08:00', journal=journal)
    node = nodes(result)['data']
    assert node['state'] == 'COOLDOWN'
    assert node['next_retry_at'] == TODAY + 'T19:30:00+08:00'
    assert result['automatic_execution']['next_allowed_at'] == TODAY + 'T19:00:00+08:00'


@pytest.mark.parametrize(('at', 'expected'), [('08:59:59', 'PENDING'), ('09:00:00', 'MISSED')])
def test_plan_cutoff_equality_without_frozen_facts(home_settings, at, expected):
    result = project_status(home_settings, now=TODAY + 'T' + at + '+08:00',
                            facts=account_facts(frozen=False))
    assert nodes(result)['morning_plan']['state'] == expected
    assert nodes(result)['morning_plan']['deadline_at'] == TODAY + 'T09:00:00+08:00'
    assert nodes(result)['morning_plan']['scheduled_at'] == PREVIOUS + 'T18:10:00+08:00'


def test_empty_frozen_plan_stays_complete_after_cutoff(home_settings):
    facts = account_facts()
    for row in facts['accounts']:
        row['sessions'][TODAY]['plan'].update(status='EMPTY', order_count=0)
    result = project_status(home_settings, facts=facts)
    assert nodes(result)['morning_plan']['state'] == 'COMPLETE'
    assert all('空计划' in row['detail'] for row in nodes(result)['morning_plan']['accounts'])


def test_timezone_midnight_and_nontrading_maintenance_use_exchange_calendar(home_settings):
    result = project_status(home_settings, now='2026-11-01T16:00:00+00:00')
    assert result['today'] == TODAY
    assert result['sampled_at'] == TODAY + 'T00:00:00+08:00'
    holiday = project_status(home_settings, now='2026-11-01T12:00:00+08:00')
    assert holiday['calendar']['is_trading_day'] is False
    assert nodes(holiday)['data']['session'] == PREVIOUS
    assert nodes(holiday)['morning_plan']['target_session'] == TODAY
    assert '非交易日' in holiday['summary']['detail']


@pytest.mark.parametrize('calendar', [[], CALENDAR[:-4], [*CALENDAR, CALENDAR[0]]])
def test_missing_future_or_corrupt_calendar_is_unknown_not_weekday_guess(home_settings, calendar):
    result = project_status(home_settings, calendar=calendar)
    assert result['calendar']['state'] == 'UNKNOWN'
    assert result['summary']['state'] == 'UNKNOWN'
    assert all(row['state'] != 'COMPLETE' for row in result['nodes'])


@pytest.mark.parametrize(('observed', 'birth', 'expected'), [
    ({'state': 'ALIVE', 'birth': 'born-1'}, 'born-1', 'RUNNING'),
    ({'state': 'ALIVE', 'birth': 'reused-pid'}, 'born-1', 'INTERRUPTED'),
    ({'state': 'DEAD', 'birth': None}, 'born-1', 'INTERRUPTED'),
    ({'state': 'UNKNOWN', 'birth': None}, 'born-1', 'UNKNOWN'),
    ({'state': 'ALIVE', 'birth': None}, 'born-1', 'UNKNOWN'),
    ({'state': 'ALIVE', 'birth': 'born-1'}, None, 'UNKNOWN'),
])
def test_process_birth_identity_and_readonly_interrupt_projection(home_settings, observed, birth, expected):
    now = '2026-11-02T20:00:00+08:00'
    journal = journal_at(now, [{'phase': 'backup', 'state': 'RUNNING'}], birth=birth)
    original = deepcopy(journal)
    result = project_status(home_settings, now=now, journal=journal, process_probe=lambda pid: observed)
    assert result['current_run']['state'] == expected
    assert journal == original
    if expected == 'RUNNING':
        assert result['current_run']['phase'] == 'backup'
        assert nodes(result)['backup']['state'] == 'RUNNING'
        assert result['summary']['state'] == 'RUNNING'
    else:
        assert result['current_run']['phase'] is None


def test_process_probe_exception_and_old_missing_birth_do_not_claim_alive(home_settings):
    def unavailable(pid):
        raise PermissionError('private process details')
    journal = journal_at('2026-11-02T10:00:00+08:00', [{'phase': 'backup', 'state': 'RUNNING'}])
    assert project_status(home_settings, journal=journal, process_probe=unavailable)['current_run']['state'] == 'UNKNOWN'
    journal['runs']['run-1']['attempts'][0]['metadata'].pop('process_birth')
    assert project_status(home_settings, journal=journal)['current_run']['state'] == 'UNKNOWN'


def test_active_phase_uses_latest_state_of_same_phase_identity(home_settings):
    now = '2026-11-02T20:00:00+08:00'
    journal = journal_at(now, [
        {'phase': 'settlement', 'account_id': 'S1', 'state': 'RUNNING'},
        {'phase': 'close', 'account_id': 'S1', 'state': 'RUNNING'},
        {'phase': 'close', 'account_id': 'S1', 'state': 'COMPLETE'},
        {'phase': 'settlement', 'account_id': 'S1', 'state': 'COMPLETE'},
        {'phase': 'backup', 'state': 'RUNNING'}])
    result = project_status(home_settings, now=now, journal=journal)
    assert result['current_run']['phase'] == 'backup'
    assert result['current_run']['phase_started_at']
    assert result['current_run']['finished_at'] is None


def test_account_failure_waiting_and_success_remain_separate(home_settings):
    now = '2026-11-02T20:00:00+08:00'
    journal = journal_at(now, [
        {'phase': 'input_review', 'account_id': 'S1', 'state': 'COMPLETE'},
        {'phase': 'input_review', 'account_id': 'S1-LT', 'state': 'FAILED',
         'result': {'reason': 'ACCOUNT_UNAVAILABLE'}},
        {'phase': 'input_review', 'account_id': 'LLM', 'state': 'WAITING',
         'result': {'reason': 'RETRY_COOLDOWN', 'next_retry_at': '2026-11-02T20:15:00+08:00'}}],
        state='PARTIAL')
    result = project_status(home_settings, now=now, journal=journal)
    states = {row['account_id']: row['state'] for row in nodes(result)['input_review']['accounts']}
    assert states == {'S1': 'COMPLETE', 'S1-LT': 'FAILED', 'LLM': 'COOLDOWN'}
    assert nodes(result)['input_review']['state'] == 'FAILED'


def test_latest_phase_failure_cannot_be_overridden_by_older_completion(home_settings):
    now = '2026-11-02T20:00:00+08:00'
    journal = journal_at(now, [{'phase': 'data', 'state': 'COMPLETE'},
                              {'phase': 'data', 'state': 'FAILED'}], state='PARTIAL')
    assert nodes(project_status(home_settings, now=now, journal=journal))['data']['state'] == 'FAILED'


def test_old_no_phase_cycle_only_ledger_facts_can_prove_completion(home_settings):
    facts = account_facts()
    final_statement(facts)
    journal = journal_at('2026-11-02T20:00:00+08:00', state='COMPLETE')
    journal['runs']['run-1']['attempts'][0].pop('phases')
    result = project_status(home_settings, now='2026-11-02T20:00:00+08:00', facts=facts, journal=journal)
    assert nodes(result)['settlement']['state'] == 'COMPLETE'
    assert nodes(result)['backup']['state'] != 'COMPLETE'
    assert nodes(result)['report_daily']['state'] != 'COMPLETE'
    assert nodes(result)['report_plan']['state'] != 'COMPLETE'
    assert result['summary']['state'] != 'COMPLETE'


def test_reports_are_distinct_and_latest_unpublished_revision_stays_pending(home_settings):
    now = '2026-11-02T20:00:00+08:00'
    facts = account_facts()
    facts['accounts'][0]['report_revision'] = 5
    journal = journal_at(now, [{'phase': 'report_daily', 'state': 'COMPLETE'},
                              {'phase': 'report_plan', 'state': 'COMPLETE'}], state='COMPLETE')
    result = project_status(home_settings, now=now, facts=facts, journal=journal)
    assert nodes(result)['report_daily']['state'] == 'COMPLETE'
    rows = {row['account_id']: row for row in nodes(result)['report_plan']['accounts']}
    assert rows['S1']['state'] == 'WAITING_UPSTREAM'
    assert rows['LLM']['state'] == rows['S1-LT']['state'] == 'COMPLETE'


def test_restate_marker_and_missing_statement_cannot_be_hidden_by_complete_phase(home_settings):
    now = '2026-11-02T20:00:00+08:00'
    facts = account_facts()
    final_statement(facts)
    facts['accounts'][0]['restate_from'] = TODAY
    facts['accounts'][1]['sessions'][TODAY]['statement'] = None
    journal = journal_at(now, [{'phase': 'settlement', 'state': 'COMPLETE'}], state='COMPLETE')
    result = project_status(home_settings, now=now, facts=facts, journal=journal)
    states = {row['account_id']: row['state'] for row in nodes(result)['settlement']['accounts']}
    assert states == {'S1': 'WAITING_DATA', 'S1-LT': 'UNKNOWN', 'LLM': 'COMPLETE'}


def test_committed_frozen_plan_survives_process_exit_before_phase_end(home_settings):
    now = '2026-11-02T08:00:00+08:00'
    journal = journal_at(now, [{'phase': 'plan', 'state': 'RUNNING', 'account_id': 'S1'}],
                         session=PREVIOUS, target=TODAY)
    result = project_status(home_settings, now=now, journal=journal,
                            process_probe=lambda pid: {'state': 'DEAD', 'birth': None})
    assert result['current_run']['state'] == 'INTERRUPTED'
    assert nodes(result)['morning_plan']['state'] == 'COMPLETE'


@pytest.mark.parametrize(('reason', 'expected'), [
    ('COMPLETE_BACKUP_ALREADY_EXISTS', 'COMPLETE'), ('NOT_DUE', 'NOT_REQUIRED'),
    ('EXECUTION_DISABLED', 'DISABLED'), ('UNEXPLAINED_SKIP', 'UNKNOWN')])
def test_skipped_is_not_unconditionally_complete(home_settings, reason, expected):
    now = '2026-11-02T20:00:00+08:00'
    journal = journal_at(now, [{'phase': 'backup', 'state': 'SKIPPED',
                              'result': {'reason': reason}}], state='COMPLETE')
    assert nodes(project_status(home_settings, now=now, journal=journal))['backup']['state'] == expected


def test_month_end_partial_pool_refresh_is_not_complete(home_settings):
    now = '2026-11-01T12:00:00+08:00'
    journal = journal_at(now, [{'phase': 'month_end', 'state': 'COMPLETE',
                              'result': {'completion': False, 'state': 'PARTIAL'}}],
                         state='PARTIAL', session=PREVIOUS, target=TODAY)
    result = project_status(home_settings, now=now, journal=journal)
    assert nodes(result)['month_end']['session'] == PREVIOUS
    assert nodes(result)['month_end']['state'] == 'UNKNOWN'
    assert len(nodes(result)['month_end']['details']) == 3
    assert all(row['state'] != 'COMPLETE' for row in nodes(result)['month_end']['details'])


def test_pending_old_month_end_stays_visible_on_a_non_month_end_trading_day(home_settings):
    now = '2026-11-02T10:00:00+08:00'
    journal = journal_at(now, [{'phase': 'month_end', 'state': 'WAITING',
        'result': {'next_retry_at': '2026-11-02T10:15:00+08:00', 'reason': 'RETRY_COOLDOWN'}}],
        state='PARTIAL', session=PREVIOUS, target=TODAY)
    result = project_status(home_settings, now=now, journal=journal)
    month = nodes(result)['month_end']
    assert month['state'] == 'COOLDOWN'
    assert month['session'] == PREVIOUS and month['target_session'] == TODAY
    assert month['scheduled_at'] == PREVIOUS + 'T18:10:00+08:00'
    assert nodes(result)['data']['state'] == 'SCHEDULED'


def test_maintenance_overrides_header_without_erasing_durable_plan(home_settings):
    path = home_settings.root / 'data/state/dashboard-maintenance.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'active': True, 'private_reason': 'https://secret.example/token'}))
    result = project_status(home_settings)
    assert result['summary']['state'] == 'MAINTENANCE'
    assert nodes(result)['morning_plan']['state'] == 'COMPLETE'
    assert 'secret.example' not in json.dumps(result)
    path.write_text('{broken')
    before = path.read_bytes()
    assert project_status(home_settings)['summary']['state'] == 'UNKNOWN'
    assert path.read_bytes() == before


def test_disabled_inactive_and_empty_accounts_are_not_success(home_settings):
    assert project_status(home_settings, facts=account_facts(start=None))['summary']['state'] == 'DISABLED'
    assert project_status(home_settings, facts={'state': 'READY', 'accounts': []})['summary']['state'] == 'NOT_REQUIRED'
    home_settings.raw['market']['enabled'] = False
    assert nodes(project_status(home_settings))['data']['state'] == 'DISABLED'
    home_settings.raw['strategies']['s1']['enabled'] = False
    result = project_status(home_settings, facts=account_facts(frozen=False), now='2026-11-02T08:00:00+08:00')
    assert nodes(result)['morning_plan']['accounts'][0]['state'] == 'DISABLED'


def test_bounded_whitelist_omits_error_bodies_credentials_and_audit_objects(home_settings):
    now = '2026-11-02T20:00:00+08:00'
    journal = journal_at(now, [{'phase': 'data', 'state': 'FAILED', 'result': {
        'reason': 'https://token@private.example', 'input_bundle': 'SECRET' * 10000,
        'response': 'SECRET', 'traceback': '/Users/private/secret'}}], state='PARTIAL')
    facts = account_facts()
    facts['accounts'][0]['sessions'][TODAY]['display_name'] = '/Users/private/secret'
    result = project_status(home_settings, now=now, journal=journal, facts=facts)
    text = json.dumps(result, ensure_ascii=False)
    assert not any(value in text for value in ('SECRET', 'private.example', '/Users/', 'input_bundle', 'traceback'))
    assert len(text) < 30_000
    facts['accounts_truncated'] = True
    assert project_status(home_settings, facts=facts)['summary']['state'] == 'UNKNOWN'


def test_corrupt_journal_does_not_reset_or_claim_complete(home_settings):
    assert project_status(home_settings, journal={'runs': []})['summary']['state'] == 'UNKNOWN'
    assert project_status(home_settings, journal={'schema_version': 9, 'runs': {}})['summary']['state'] == 'UNKNOWN'
    result = project_status(home_settings, journal={'state': 'RUNNING', 'current_run': {'pid': 5}})
    assert result['current_run']['state'] == 'UNKNOWN'


def test_future_phase_cannot_fall_back_to_older_success(home_settings):
    now = '2026-11-02T20:00:00+08:00'
    journal = journal_at(now, [{'phase': 'data', 'state': 'COMPLETE'},
        {'phase': 'data', 'state': 'FAILED', 'recorded_at': '2026-11-02T21:00:00+08:00'}], state='PARTIAL')
    assert nodes(project_status(home_settings, now=now, journal=journal))['data']['state'] == 'UNKNOWN'


def test_global_phase_cannot_erase_explicit_peer_failure_in_same_attempt(home_settings):
    now = '2026-11-02T20:00:00+08:00'
    journal = journal_at(now, [{'phase': 'input_review', 'account_id': 'S1', 'state': 'FAILED'},
                              {'phase': 'input_review', 'state': 'COMPLETE'}], state='PARTIAL')
    result = project_status(home_settings, now=now, journal=journal)
    assert {r['account_id']: r['state'] for r in nodes(result)['input_review']['accounts']} == {
        'S1': 'FAILED', 'S1-LT': 'COMPLETE', 'LLM': 'COMPLETE'}


def test_same_clock_new_current_attempt_wins_over_lexical_run_id_order(home_settings):
    now = '2026-11-02T20:00:00+08:00'
    journal = journal_at(now, [{'phase': 'data', 'state': 'COMPLETE'}], state='COMPLETE')
    old = deepcopy(journal['runs']['run-1'])
    old['run_id'] = 'z-old-run'
    old['attempts'][0]['phases'][0]['state'] = 'FAILED'
    journal['runs']['z-old-run'] = old
    result = project_status(home_settings, now=now, journal=journal)
    assert nodes(result)['data']['state'] == 'COMPLETE'


def test_one_unknown_account_does_not_hide_other_frozen_plans(home_settings):
    facts = account_facts()
    facts['accounts'][0] = {'account_id': 'S1', 'strategy_id': 'S1', 'state': 'UNKNOWN', 'sessions': {}}
    result = project_status(home_settings, facts=facts)
    assert {r['account_id']: r['state'] for r in nodes(result)['morning_plan']['accounts']} == {
        'S1': 'UNKNOWN', 'S1-LT': 'COMPLETE', 'LLM': 'COMPLETE'}


def test_all_business_nodes_complete_requires_current_date_rows_and_both_reports(home_settings):
    now = '2026-11-02T20:30:00+08:00'
    facts = account_facts()
    final_statement(facts)
    for account in facts['accounts']:
        account['sessions'][NEXT]['plan'] = {'plan_id': 'next', 'target_session': NEXT,
            'status': 'EMPTY', 'frozen_at': '2026-11-02T20:00:00+08:00', 'order_count': 0}
    phases = [{'phase': name, 'state': 'COMPLETE'} for name in (
        'data', 'input_review', 'settlement', 'report_daily', 'plan', 'report_plan', 'backup')]
    journal = journal_at(now, phases, state='COMPLETE')
    result = project_status(home_settings, now=now, facts=facts, journal=journal)
    assert result['summary']['state'] == 'COMPLETE'
    assert all(row['state'] in {'COMPLETE', 'NOT_REQUIRED'} for row in result['nodes'])
    # Even a terminal COMPLETE run cannot certify a missing current financial result.
    facts['accounts'][0]['sessions'][TODAY]['statement'] = None
    assert project_status(home_settings, now=now, facts=facts, journal=journal)['summary']['state'] == 'UNKNOWN'


def test_two_scheduler_processes_are_independent_and_private_launchctl_fields_omitted(home_settings):
    value = build_status(home_settings, now=datetime.fromisoformat('2026-11-02T10:00:00+08:00'),
        calendar=CALENDAR, journal={'runs': {}}, ledger_facts=account_facts(),
        scheduler_info={'workflow': {'state': 'INSTALLED', 'loaded': True,
            'runtime': {'state': 'IDLE', 'pid': None, 'last_exit_code': 0},
            'plist_path': '/Users/private/secret', 'environment': {'TOKEN': 'SECRET'}},
            'monitor': {'state': 'INSTALLED', 'runtime': {'state': 'RUNNING', 'pid': 99, 'last_exit_code': 0}}})
    assert value['scheduler']['workflow']['runtime']['state'] == 'IDLE'
    assert value['scheduler']['monitor']['runtime']['state'] == 'RUNNING'
    assert 'SECRET' not in json.dumps(value) and '/Users/' not in json.dumps(value)


def _put(db, table, account, identity, data):
    db.execute(f'INSERT INTO {table}(account_id,id,data) VALUES (?,?,?)',
               (account, identity, json.dumps(data)))


def _inventory(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob('*') if path.is_file()}


@pytest.mark.parametrize(('persisted', 'phase_state', 'result_state', 'reason', 'at', 'expected'), [
    (True, 'SKIPPED', 'SKIPPED', 'FROZEN_PLAN_EXISTS', '08:30:00', 'COMPLETE'),
    (True, 'SKIPPED', 'SKIPPED', 'FROZEN_PLAN_EXISTS', '10:00:00', 'COMPLETE'),
    (False, 'SKIPPED', 'SKIPPED', 'FROZEN_PLAN_EXISTS', '08:30:00', 'UNKNOWN'),
    (True, 'FAILED', 'FAILED', 'FROZEN_PLAN_EXISTS', '08:30:00', 'UNKNOWN'),
    (False, 'FAILED', 'FAILED', 'FROZEN_PLAN_EXISTS', '08:30:00', 'FAILED'),
    (True, 'SKIPPED', 'INTEGRITY_ERROR', 'FROZEN_PLAN_EXISTS', '08:30:00', 'UNKNOWN'),
    (False, 'SKIPPED', 'INTEGRITY_ERROR', 'FROZEN_PLAN_EXISTS', '08:30:00', 'FAILED'),
    (True, 'SKIPPED', 'SKIPPED', 'UNEXPLAINED_SKIP', '08:30:00', 'UNKNOWN'),
])
def test_persisted_frozen_plan_and_real_journal_reuse_remain_consistent(
        home_settings, persisted, phase_state, result_state, reason, at, expected):
    """Read both durable sources through build_status, including the actual workflow reason."""
    initialize(home_settings)
    with sqlite3.connect(home_settings.database) as db:
        state = json.loads(db.execute('SELECT data FROM account_state WHERE account_id=?',
                                       ('S1',)).fetchone()[0])
        state.update(simulation_start_session=TODAY, account_revision=2,
                     report_revision=4, last_rendered_revision=4)
        db.execute('UPDATE account_state SET data=? WHERE account_id=?', (json.dumps(state), 'S1'))
        if persisted:
            _put(db, 'plans', 'S1', 'frozen-reuse', {'plan_id': 'frozen-reuse',
                'target_session': TODAY, 'status': 'FROZEN',
                'frozen_at': '2026-11-01T21:00:00+08:00', 'order_ids': ['one']})
    journal = WorkflowJournal(home_settings)
    started = datetime.fromisoformat(TODAY + 'T08:00:00+08:00')
    journal.start('frozen-reuse-check', now=started,
                  metadata={'business_session': PREVIOUS, 'target_session': TODAY})
    identity = {'session': PREVIOUS, 'target_session': TODAY, 'account_id': 'S1'}
    journal.phase('frozen-reuse-check', 'plan', now=started, state='RUNNING', **identity)
    journal.phase('frozen-reuse-check', 'plan', now=started + timedelta(seconds=1),
                  state=phase_state, result={'state': result_state, 'reason': reason}, **identity)
    journal.finish('frozen-reuse-check', now=started + timedelta(seconds=2), state='COMPLETE')
    before = _inventory(home_settings.root)
    result = build_status(home_settings, now=datetime.fromisoformat(TODAY + 'T' + at + '+08:00'),
                          calendar=CALENDAR, scheduler_info={})
    row = next(row for row in nodes(result)['morning_plan']['accounts'] if row['account_id'] == 'S1')
    assert row['state'] == expected
    assert _inventory(home_settings.root) == before


def test_monitoring_missing_database_never_creates_directories(home_settings):
    before = _inventory(home_settings.root)
    value = read_runtime_facts(home_settings, [TODAY])
    assert value['state'] == 'MISSING'
    assert _inventory(home_settings.root) == before
    assert not home_settings.database.exists()


def test_monitoring_reads_latest_scalar_facts_and_retains_restatement_boundary(home_settings):
    initialize(home_settings)
    with sqlite3.connect(home_settings.database) as db:
        state = json.loads(db.execute('SELECT data FROM account_state WHERE account_id=?', ('S1',)).fetchone()[0])
        state.update(simulation_start_session=TODAY, account_revision=2,
                     report_revision=4, last_rendered_revision=3, restate_from=TODAY)
        db.execute('UPDATE account_state SET data=? WHERE account_id=?', (json.dumps(state), 'S1'))
        _put(db, 'plans', 'S1', 'p', {'plan_id': 'p', 'target_session': TODAY, 'status': 'EMPTY',
            'frozen_at': '2026-11-01T21:00:00+08:00', 'order_ids': [],
            'input_bundle': {'secret': 'NEVER_RENDER' * 10000}})
        for revision in (1, 2):
            _put(db, 'daily_nav', 'S1', f'{TODAY}:{revision}', {'session': TODAY,
                'statement_revision': revision, 'accounting_status': 'COMPLETE', 'valuation_status': 'FINAL',
                'processed_at': TODAY + 'T18:30:00+08:00', 'data_evidence': ['NEVER_RENDER']})
        _put(db, 'orders', 'S1', 'o', {'target_session': TODAY, 'status': 'PENDING', 'code': 'SH600000'})
    before = _inventory(home_settings.root)
    value = read_runtime_facts(home_settings, [TODAY, TODAY])
    row = next(r for r in value['accounts'] if r['account_id'] == 'S1')
    assert value['state'] == 'READY'
    assert value['sessions'] == [TODAY]
    assert row['sessions'][TODAY]['plan']['status'] == 'EMPTY'
    assert row['sessions'][TODAY]['statement']['statement_revision'] == 2
    assert row['sessions'][TODAY]['statement']['requires_restatement'] is True
    assert row['sessions'][TODAY]['pending_order_count'] == 1
    assert 'NEVER_RENDER' not in json.dumps(value)
    assert _inventory(home_settings.root) == before


def test_monitoring_account_corruption_isolated_and_database_corruption_unknown(home_settings):
    initialize(home_settings)
    with sqlite3.connect(home_settings.database) as db:
        db.execute("UPDATE account_state SET data=json_set(data,'$.account_revision','bad') WHERE account_id='S1'")
    value = read_runtime_facts(home_settings, [TODAY])
    assert value['state'] == 'PARTIAL'
    assert {r['account_id']: r['state'] for r in value['accounts']} == {
        'LLM': 'READY', 'S1': 'UNKNOWN', 'S1-LT': 'READY'}
    home_settings.database.write_bytes(b'bad database bytes')
    before = home_settings.database.read_bytes()
    assert read_runtime_facts(home_settings, [TODAY])['state'] == 'UNKNOWN'
    assert home_settings.database.read_bytes() == before


def test_monitoring_does_not_wait_for_workspace_writer_and_reads_committed_sql_snapshot(home_settings):
    initialize(home_settings)
    with writer_lock(home_settings), sqlite3.connect(home_settings.database) as writer:
        writer.execute('BEGIN IMMEDIATE')
        writer.execute("UPDATE account_state SET data=json_set(data,'$.account_revision',99) WHERE account_id='S1'")
        value = read_runtime_facts(home_settings, [TODAY])
        assert value['state'] == 'READY'
        assert next(r for r in value['accounts'] if r['account_id'] == 'S1')['account_revision'] == 0
        writer.rollback()


def test_status_never_calls_full_business_or_audit_api(home_settings, monkeypatch):
    from china_stock_sim.application.workflow import WorkflowApplication
    from china_stock_sim.ledger import LedgerService
    from china_stock_sim.market.service import MarketDataService

    def forbidden(*args, **kwargs):
        raise AssertionError('business or heavy audit called')

    for name in ('inspect', 'run'):
        monkeypatch.setattr(WorkflowApplication, name, forbidden)
    for name in ('report_snapshot', 'get_strategy_decision', 'verify_backup', 'backup'):
        monkeypatch.setattr(LedgerService, name, forbidden)
    monkeypatch.setattr(MarketDataService, 'update', forbidden)
    initialize(home_settings)
    before = _inventory(home_settings.root)
    result = build_status(home_settings, now=datetime.fromisoformat('2026-11-02T10:00:00+08:00'),
                          journal={'runs': {}, 'tasks': {}}, calendar=CALENDAR, scheduler_info={})
    assert result['summary']['state'] == 'DISABLED'
    assert _inventory(home_settings.root) == before


def test_date_scope_limit_is_explicit_and_unknown_is_not_zero(home_settings):
    with pytest.raises(ValueError):
        read_runtime_facts(home_settings, [(datetime(2026, 11, 1) + timedelta(days=i)).date().isoformat()
                                          for i in range(13)])
    result = project_status(home_settings, facts={'state': 'UNKNOWN', 'accounts': []})
    assert result['summary']['state'] == 'UNKNOWN'
    assert result['accounts'] == []
    with pytest.raises(ValueError):
        build_status(home_settings, now=datetime(2026, 11, 2), calendar=CALENDAR, journal={}, ledger_facts={})
