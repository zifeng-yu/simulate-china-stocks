"""Workflow data adaptation uses public services and isolated deterministic sources."""

from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pytest
from execution_fixtures import advance, execution_book, set_pool
from test_ledger_trading import call
from test_market_pending_event_ranges import EventSource
from test_market_update import Clock, fixture_days
from test_strategy_acceptance import strategy_world

from china_stock_sim.application import workflow_data
from china_stock_sim.application.workflow_data import WorkflowData
from china_stock_sim.market.providers import ProviderError
from china_stock_sim.market.repository import MarketRepository
from china_stock_sim.market.service import MarketDataService

DAY, NEXT = '2026-10-30', '2026-11-02'
CODES = ('SH600000', 'SZ000001', 'SH600001', 'SZ000002')
assert execution_book and strategy_world


class Accounts:
    def __init__(self):
        self.inputs, self.historical, self.windows, self.policy_reads = {}, {}, {}, []

    def required_market_inputs(self, account, day):
        return deepcopy(self.inputs.get(account, {'as_of': day, 'holdings': [],
            'pending_orders': [], 'pending_actions': [], 'pending_event_ranges': [], 'unknown_ranges': []}))

    def positions(self, account, day):
        return deepcopy(self.historical.get((account, day), []))

    def get_account_policy(self, account, day):
        self.policy_reads.append((account, day))
        return {'policy': {'parameters': {'lookback_sessions': self.windows.get((account, day), 121)}}}


class PublicMarket(MarketDataService):
    def __init__(self, root, sources):
        super().__init__(root)
        self.sources, self.updates = sources, []

    def update(self, request, **kwargs):
        self.updates.append((request, deepcopy(kwargs)))
        return super().update(request, providers=self.sources, **kwargs)


@pytest.fixture
def world(settings, monkeypatch):
    import socket

    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('no live network'))
    template = Path(__file__).resolve().parents[1] / 'configs/preparation.toml'
    (settings.root / 'configs/preparation.toml').write_bytes(template.read_bytes())
    clock, calls = Clock(), []
    clock.now = datetime.fromisoformat(DAY + 'T20:00:00+08:00')
    sources = {name: EventSource(name, clock, calls) for name in ('baostock', 'tencent', 'eastmoney')}
    market, accounts = PublicMarket(settings.root, sources), Accounts()
    adapter = WorkflowData(settings, market=market, ledger=accounts, clock=clock)
    return adapter, market, accounts, clock, calls, sources


def files(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def seed_calendar(adapter, *, end='2027-02-01'):
    rows = [{'date': row['calendar_date'], 'is_trading_day': row['is_trading_day']}
            for row in fixture_days('2025-01-01', end)]
    MarketRepository(adapter.root).put('calendar', rows, {
        'provider': 'isolated_calendar', 'adapter_version': '1',
        'fetched_at': '2025-01-01T18:10:00+08:00', 'rows': rows})


def seed_pool(adapter, day='2026-10-28', target='2026-10-29', codes=CODES[:1]):
    rows = [{'code': '', 'date': day, 'effective_from': target,
             'selected': [{'code': code, 'name': '隔离流程证券'} for code in codes],
             'frozen_at': day + 'T18:30:00+08:00'}]
    MarketRepository(adapter.root).put('pool', rows, {
        'provider': 'isolated_pool', 'adapter_version': '1',
        'fetched_at': day + 'T18:30:00+08:00', 'rows': rows})


def fake_prepare(adapter, monkeypatch, *, count=2, complete=True):
    seen = []

    def prepare(root, day, **kwargs):
        seen.append((root, day, kwargs))
        seed_pool(adapter, day, NEXT, CODES[1:3])
        return {'state': 'POOL_PREPARED', 'effective_from': NEXT,
                'preparation': {'run_id': 'isolated-frozen-month-end', 'price_ready': True,
                    'state': 'PREPARATION_COMPLETE' if complete else 'PARTIAL',
                    'clues_downloaded': complete, 'requests_this_run': count,
                    'requests': {} if complete else {'adjustments': {
                        'status': 'FAILED', 'next_retry_at': '2026-10-30T20:02:00+08:00'}}}}

    monkeypatch.setattr(workflow_data, 'prepare_pool', prepare)
    return seen


def test_inspect_and_calendar_cache_checks_never_create_files_or_call_updater(world):
    adapter, market, _, _, calls, _ = world
    before = files(adapter.root)
    assert adapter.ensure_calendar(DAY)['state'] == 'WAITING_CALENDAR'
    result = adapter.inspect(DAY, [], include_indices=False)
    assert result['state'] == 'WAITING_DATA' and not result['completion']
    assert result['collection_checked'] is False
    assert files(adapter.root) == before and not calls and not market.updates


def test_calendar_bootstrap_resolves_weekend_and_before_1810_without_future_prices(world):
    adapter, market, _, clock, calls, _ = world
    clock.now = datetime.fromisoformat('2026-11-01T08:00:00+08:00')
    result = adapter.ensure_calendar('2026-11-01', download=True)
    assert result['state'] == 'READY' and result['completion']
    assert result['calendar_as_of'] == '2026-10-31'
    assert result['completed_session'] == DAY and result['next_session'] == NEXT
    assert result['is_month_end'] is True
    request = market.updates[0][0]
    assert request.price_codes == () and not request.include_events and request.include_indices
    assert all(q in {'calendar', 'prices'} for _, q, _ in calls)
    assert all(p['end_date'] <= DAY for _, q, p in calls if q == 'prices')
    assert all(p['code'] in {'SH000001', 'SZ399001', 'SH000300'}
               for _, q, p in calls if q == 'prices')
    before = len(calls)
    assert adapter.ensure_calendar('2026-11-01', download=True)['changed'] is False
    assert len(calls) == before


def test_calendar_recovery_does_not_depend_on_index_download_success(world):
    adapter, _, _, _, _, sources = world
    sources['tencent'].failures['prices'] = [ProviderError('index unavailable', retryable=True)] * 3
    result = adapter.ensure_calendar(DAY, download=True)
    assert result['completion'] and result['state'] == 'READY'
    assert result['collection']['state'] == 'PARTIAL'
    assert result['next_session'] == NEXT


def test_scope_unions_current_historical_pending_and_future_policy_window(world):
    adapter, _, accounts, _, _, _ = world
    seed_calendar(adapter)
    seed_pool(adapter)
    old = {'code': CODES[3], 'start': '2017-06-01', 'end': DAY, 'event_ids': ['unpaid']}
    accounts.inputs['A'] = {'holdings': [CODES[0]], 'pending_orders': [CODES[1]],
        'pending_actions': [CODES[3]], 'pending_event_ranges': [old], 'unknown_ranges': []}
    accounts.inputs['B'] = {'holdings': [CODES[1]], 'pending_orders': [],
        'pending_actions': [CODES[3]], 'pending_event_ranges': [old], 'unknown_ranges': []}
    accounts.historical[('A', DAY)] = [{'code': CODES[2], 'total_shares': 100}]
    accounts.windows[('A', DAY)] = 130
    accounts.windows[('B', NEXT)] = 150
    before = deepcopy(accounts.inputs)
    scope = adapter.build_request(DAY, ['A', 'B'], history_window=121)
    request = scope['request']
    assert request.holdings == tuple(sorted(CODES[:3]))
    assert request.pending_orders == (CODES[1],) and request.pending_actions == (CODES[3],)
    assert request.history_sessions == 150
    assert len(request.pending_event_ranges) == 1 and request.pending_event_ranges[0].start == old['start']
    assert request.ordered_codes[0] in request.holdings and set(request.ordered_codes) == set(CODES)
    assert scope['target_session'] == NEXT and ('B', NEXT) in accounts.policy_reads
    assert accounts.inputs == before


def test_missing_pool_does_not_drop_held_or_pending_scope(world):
    adapter, _, accounts, _, _, _ = world
    seed_calendar(adapter)
    accounts.inputs['A'] = {'holdings': [CODES[0]], 'pending_orders': [CODES[1]],
        'pending_actions': [], 'pending_event_ranges': [], 'unknown_ranges': []}
    scope = adapter.build_request(DAY, ['A'])
    assert scope['request'].price_codes == CODES[:2]
    assert scope['reasons'][0]['kind'] == 'POOL_UNAVAILABLE'


def test_real_collection_reuses_prices_and_keeps_custom_window_without_reset(world):
    adapter, market, _, _, calls, _ = world
    first = adapter.collect(DAY, candidates=CODES[:1], history_window=150,
                            include_indices=False, download=True)
    assert first['state'] == 'COMPLETE', first
    assert first['completion'] and first['changed']
    history = [p for _, q, p in calls if q == 'history']
    assert len(history) == 1 and history[0]['end_date'] == DAY
    before = len(calls)
    second = adapter.collect(DAY, candidates=CODES[:1], history_window=150,
                             include_indices=False, download=True)
    assert second['completion'] and second['requests_this_run'] == 0
    assert len(calls) == before
    assert [r.history_sessions for r, _ in market.updates] == [150, 150]
    assert len(market.prices(CODES[0], history[0]['start_date'], DAY,
                            knowledge_cutoff=adapter.clock()).data) == 150


@pytest.mark.parametrize('server_delay', [None, 90])
def test_failed_collection_deadline_is_returned_and_only_failed_source_resumes(world, server_delay):
    adapter, _, _, clock, calls, sources = world
    failure = ProviderError('temporary', retryable=True)
    if server_delay:
        failure.retry_after_seconds = server_delay
    sources['baostock'].failures['dividends'] = [failure]
    args = {'candidates': CODES[:1], 'include_indices': False, 'download': True}
    failed = adapter.collect(DAY, **args)
    assert not failed['completion'] and failed['next_retry_at']
    stored = [row for row in MarketRepository(adapter.root).states('requests').values()
              if row['state'] == 'FAILED'][0]
    assert failed['next_retry_at'] == stored['next_retry_at']
    if server_delay:
        issue = next(row for row in failed['collection']['issues'] if row['kind'] == 'NETWORK')
        assert issue['server_retry_at'] == stored['server_retry_at'] == failed['next_retry_at']
    before = len(calls)
    cooling = adapter.collect(DAY, **args)
    assert not cooling['completion'] and len(calls) == before
    clock.now = datetime.fromisoformat(failed['next_retry_at'])
    recovered = adapter.collect(DAY, **args)
    assert recovered['completion'], recovered
    assert [q for _, q, _ in calls[before:]] == ['dividends']


def test_unknown_pending_range_remains_visible_even_if_collected_facts_complete(world, monkeypatch):
    adapter, market, accounts, _, _, _ = world
    accounts.inputs['A'] = {'holdings': [], 'pending_orders': [], 'pending_actions': [CODES[0]],
        'pending_event_ranges': [], 'unknown_ranges': [{'code': CODES[0], 'case_id': 'case',
                                                       'reason': 'UNKNOWN_EVENT_RANGE'}]}
    monkeypatch.setattr(market, 'update', lambda *a, **k: {'state': 'COMPLETE', 'requests_this_run': 0})
    result = adapter.collect(DAY, ['A'], candidates=(), download=False)
    assert result['state'] == 'PARTIAL' and not result['completion']
    assert result['collection']['state'] == 'COMPLETE'
    assert result['unknown_ranges'][0]['account_id'] == 'A'


def test_month_end_uses_explicit_successor_codes_at_old_date_with_shared_budget(world, monkeypatch):
    adapter, market, accounts, _, _, _ = world
    seed_calendar(adapter)
    seed_pool(adapter)
    accounts.windows[('A', NEXT)] = 140
    old = market.pool(DAY, knowledge_cutoff=adapter.clock())
    seen = fake_prepare(adapter, monkeypatch)
    result = adapter.refresh_month_end(DAY, accounts=['A'], download=True,
                                      max_requests=20, budget_seconds=60)
    assert result['completion'], result
    assert result['target_session'] == NEXT and result['request']['as_of'] == DAY
    request, options = market.updates[-1]
    assert request.candidates == CODES[1:3] and request.history_sessions == 140
    assert options['max_requests'] == 18 and 5 <= options['budget_seconds'] <= 60
    assert seen[0][1] == DAY and not seen[0][2]['force'] and not seen[0][2]['repair']
    assert market.pool(DAY, knowledge_cutoff=adapter.clock()) == old
    assert result['requests_this_run'] == 2 + result['collection']['requests_this_run']


def test_outer_pool_prepared_never_hides_incomplete_clues(world, monkeypatch):
    adapter, market, _, _, _, _ = world
    seed_calendar(adapter)
    seed_pool(adapter)
    fake_prepare(adapter, monkeypatch, complete=False)
    result = adapter.refresh_month_end(DAY, download=True)
    assert result['state'] == 'PARTIAL' and not result['completion']
    assert result['preparation']['state'] == 'POOL_PREPARED'
    assert result['next_retry_at'] == '2026-10-30T20:02:00+08:00'
    assert not market.updates


@pytest.mark.parametrize('budget_exhausted', ['requests', 'time'])
def test_month_end_budget_cannot_reset_between_preparation_and_new_pool_collection(
        world, monkeypatch, budget_exhausted):
    adapter, market, _, _, _, _ = world
    seed_calendar(adapter)
    seed_pool(adapter)
    fake_prepare(adapter, monkeypatch, count=5 if budget_exhausted == 'requests' else 1)
    ticks = iter([0, 18 if budget_exhausted == 'time' else 1])
    monkeypatch.setattr(workflow_data, 'monotonic', lambda: next(ticks))
    result = adapter.refresh_month_end(DAY, download=True, max_requests=5, budget_seconds=20)
    assert result['state'] == 'BUDGET_PAUSED' and not result['completion']
    assert not market.updates


def test_newer_known_pool_prevents_historical_month_from_regressing_latest(world, monkeypatch):
    adapter, market, _, clock, _, _ = world
    seed_calendar(adapter)
    seed_pool(adapter)
    clock.now = datetime.fromisoformat('2026-12-02T20:00:00+08:00')
    seed_pool(adapter, '2026-11-30', '2026-12-01', CODES[2:])
    monkeypatch.setattr(workflow_data, 'prepare_pool', lambda *a, **k: pytest.fail('pool regression'))
    before = files(adapter.root)
    result = adapter.refresh_month_end(DAY, download=True)
    assert result['state'] == 'UNCHANGED' and result['completion'] and not result['changed']
    assert result['reasons'][0]['kind'] == 'NEWER_POOL_PRESENT'
    assert not market.updates and files(adapter.root) == before


def test_regular_day_keeps_existing_pool_and_does_not_prepare(world, monkeypatch):
    adapter, market, _, _, _, _ = world
    seed_calendar(adapter)
    seed_pool(adapter)
    monkeypatch.setattr(workflow_data, 'prepare_pool', lambda *a, **k: pytest.fail('not month-end'))
    result = adapter.refresh_month_end('2026-10-29', download=True)
    assert result['completion'] and not result['changed'] and not market.updates


def test_corrupt_active_pool_is_not_replaced_or_automatically_repaired(world, monkeypatch):
    adapter, market, _, _, _, _ = world
    seed_calendar(adapter)
    seed_pool(adapter)
    active = market.pool(DAY, knowledge_cutoff=adapter.clock())
    path = adapter.root / active.data['_provenance']['raw_ref']
    path.write_text('isolated damage')
    monkeypatch.setattr(workflow_data, 'prepare_pool', lambda *a, **k: pytest.fail('no repair'))
    result = adapter.refresh_month_end(DAY, download=True)
    assert result['state'] == 'NEEDS_DATA_REVIEW' and not result['completion']
    assert not market.updates and path.read_text() == 'isolated damage'


@pytest.mark.parametrize('day', ['2026-10-31', '2026-11-02'])
def test_collection_rejects_unfinished_or_future_days_before_any_write(world, day):
    adapter, market, _, _, calls, _ = world
    before = files(adapter.root)
    with pytest.raises(ValueError):
        adapter.collect(day, download=True)
    assert not calls and not market.updates and files(adapter.root) == before


def test_old_entitlement_scope_refreshes_years_without_extending_price_window(world):
    adapter, market, accounts, _, calls, _ = world
    seed_calendar(adapter)
    accounts.inputs['A'] = {'holdings': [], 'pending_orders': [], 'pending_actions': [CODES[3]],
        'pending_event_ranges': [{'code': CODES[3], 'start': '2017-06-01',
                                 'end': DAY, 'event_ids': ['old-unpaid']}], 'unknown_ranges': []}
    result = adapter.collect(DAY, ['A'], candidates=(), include_indices=False, download=True)
    assert result['completion'], result
    request = market.updates[0][0]
    assert request.history_sessions == 121 and not request.candidates
    history = [p for _, q, p in calls if q == 'history']
    assert len(history) == 1 and history[0]['start_date'] > '2026-01-01'
    years = [p['year'] for _, q, p in calls if q == 'dividends']
    assert set(years) == set(range(2017, 2027)) and len(years) == 10
    ranges = result['collection']['securities'][0]['pending_event_ranges']
    assert ranges[0]['state'] == 'READY' and ranges[0]['event_ids'] == ('old-unpaid',)


@pytest.mark.parametrize('phase', ['inputs', 'policy'])
def test_broken_one_account_scope_does_not_discard_other_accounts(world, monkeypatch, phase):
    adapter, market, accounts, _, _, _ = world
    seed_calendar(adapter)
    accounts.inputs['B'] = {'holdings': [CODES[1]], 'pending_orders': [],
                           'pending_actions': [], 'pending_event_ranges': [], 'unknown_ranges': []}
    method = 'required_market_inputs' if phase == 'inputs' else 'get_account_policy'
    original = getattr(accounts, method)

    def broken(account, day):
        if account == 'A':
            raise ValueError('isolated account original unavailable')
        return original(account, day)

    monkeypatch.setattr(accounts, method, broken)
    result = adapter.collect(DAY, ['A', 'B'], candidates=(), include_indices=False, download=True)
    assert result['state'] == 'PARTIAL' and not result['completion']
    assert result['collection']['state'] == 'COMPLETE'
    assert CODES[1] in market.updates[0][0].holdings
    assert any(row.get('account_id') == 'A' for row in result['reasons'])


@pytest.mark.parametrize('method', ['ensure_calendar', 'collect', 'refresh_month_end'])
def test_less_than_minimum_remaining_budget_never_starts_collection(world, monkeypatch, method):
    adapter, market, _, _, calls, _ = world
    if method == 'refresh_month_end':
        seed_calendar(adapter)
        seed_pool(adapter)
    monkeypatch.setattr(workflow_data, 'prepare_pool', lambda *a, **k: pytest.fail('no time'))
    before = files(adapter.root)
    result = getattr(adapter, method)(DAY, download=True, budget_seconds=4.99)
    assert result['state'] == 'BUDGET_PAUSED' and not result['changed']
    assert not calls and not market.updates and files(adapter.root) == before


def test_fractional_remaining_budget_is_floored_before_public_data_api(world, monkeypatch):
    adapter, market, _, _, _, _ = world
    received = []

    def update(request, **options):
        received.append(options)
        return {'state': 'COMPLETE', 'requests_this_run': 0}

    monkeypatch.setattr(market, 'update', update)
    result = adapter.collect(DAY, candidates=(), budget_seconds=7.99)
    assert result['completion'] and received[0]['budget_seconds'] == 7


@pytest.mark.parametrize('change,expected_window', [('future_policy', 150), ('new_account', 170)])
def test_completed_month_task_cannot_hide_next_pool_or_changed_account_window(
        strategy_world, monkeypatch, change, expected_window):
    from china_stock_sim.application.workflow import WorkflowApplication

    book, make_strategy, model = strategy_world
    advance(book, DAY)
    template = Path(__file__).resolve().parents[1] / 'configs/preparation.toml'
    (book.root / 'configs/preparation.toml').write_bytes(template.read_bytes())
    # D's old pool and N's successor differ; both are already frozen public facts.
    set_pool(book, '2026-10-29', DAY, [CODES[0]])
    set_pool(book, DAY, NEXT, [CODES[1]])
    book.settings.raw['market']['enabled'] = True
    book.settings.raw['execution']['enabled'] = False
    for value in book.settings.raw['strategies'].values():
        value['enabled'] = False
    observed = []
    original_update = book.market.update

    def observed_update(request, **options):
        observed.append((request, dict(options)))
        return original_update(request, **options)

    monkeypatch.setattr(book.market, 'update', observed_update)
    monkeypatch.setattr(book, 'backup', lambda **kwargs: {'state': 'COMPLETE', 'backup_id': 'isolated'})
    app = WorkflowApplication(book.settings, market=book.market, ledger=book,
        strategy=make_strategy(), clock=lambda: book.test_now[0], test_mode=True,
        renderer=lambda *a, **k: {'published': []})
    app.journal.save_task('month-end:' + DAY, now=book._now(), state='COMPLETE',
                          result={'state': 'COMPLETE', 'completion': True})
    monkeypatch.setattr(app.data, 'refresh_month_end',
                        lambda *a, **k: pytest.fail('completed month must not be reselected'))
    app.run(download=False)
    assert len(observed) == 1
    assert set(observed[0][0].candidates) == set(CODES[:2])
    assert observed[0][0].history_sessions == 121
    initial_keys = {key for key in app.journal.read()['tasks'] if key.startswith('data:' + DAY + ':')}
    assert len(initial_keys) == 1

    if change == 'future_policy':
        result = book.schedule_account_policy('S1', {'effective_session': NEXT,
            'strategy_parameters': {'lookback_sessions': expected_window}, 'reason': 'isolated new window'},
            operation_id='isolated-longer-window',
            expected_revision=book.account_state('S1', NEXT)['account_revision'])
        assert result['state'] == 'APPLIED', result
    else:
        created = book.create_account({'account_id': 'EXTRA', 'strategy_id': 'S1',
            'initial_cash_fen': 777777, 'strategy_parameters': {'lookback_sessions': expected_window}},
            operation_id='isolated-extra-account')
        assert created['state'] == 'APPLIED'
        assert call(book, 'activate_simulation', {'target_session': NEXT},
                    account='EXTRA')['state'] == 'APPLIED'
    app.run(download=False)
    assert len(observed) == 2, 'new scope must not be hidden by old-scope cooldown'
    request, options = observed[-1]
    assert request.as_of == DAY and set(request.candidates) == set(CODES[:2])
    assert request.history_sessions == expected_window
    assert type(options['budget_seconds']) is int and 5 <= options['budget_seconds'] <= 1800
    keys = {key for key in app.journal.read()['tasks'] if key.startswith('data:' + DAY + ':')}
    assert len(keys) == 2 and initial_keys < keys
    assert app.journal.get_task('month-end:' + DAY)['state'] == 'COMPLETE'
    assert model.calls == [] and book.fills('S1')['total'] == 0
    monkeypatch.setattr(app, '_remaining', lambda: 7.99)
    assert app._data_budget() == 7 and type(app._data_budget()) is int



