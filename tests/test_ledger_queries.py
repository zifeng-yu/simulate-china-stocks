"""Ledger consumer and day-close acceptance using isolated accounts and public data fixtures."""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from china_stock_sim.ledger.evidence import EvidenceMixin
from china_stock_sim.ledger.kernel import LedgerKernel
from china_stock_sim.ledger.queries import QueriesMixin
from china_stock_sim.ledger.statements import StatementsMixin
from china_stock_sim.market.contracts import DataResult

DAY = '2026-10-26'
NEXT = '2026-10-27'
CODE = 'SH600000'


class Market:
    def __init__(self):
        self.price_rows = {}
        self.event_rows = []
        self.coverage = True
        self.calendar_missing = False
        self.corrupt = False

    def calendar(self, *, start=None, end=None, knowledge_cutoff=None):
        if self.calendar_missing:
            return DataResult('MISSING', [])
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        return DataResult('READY', [{'date': (first + timedelta(days=i)).isoformat(),
            'is_trading_day': '1' if (first + timedelta(days=i)).weekday() < 5 else '0'}
            for i in range((last - first).days + 1)], observation_ids=('calendar-proof',))

    def calendar_operation(self, operation, day, *, knowledge_cutoff=None):
        if self.calendar_missing:
            return DataResult('WAITING_DATA', None)
        step = 1 if operation == 'next_session' else -1
        candidate = date.fromisoformat(day) + timedelta(days=step)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=step)
        return DataResult('READY', candidate.isoformat(), observation_ids=('calendar-proof',))

    def prices(self, code, start, end, *, knowledge_cutoff=None):
        if self.corrupt:
            return DataResult('CORRUPT', [])
        rows = [{'code': code, 'date': day, 'close': value}
                for day, value in sorted(self.price_rows.items()) if start <= day <= end]
        state = 'READY' if any(r['date'] == end for r in rows) else 'PARTIAL' if rows else 'MISSING'
        return DataResult(state, rows, observation_ids=('price-proof',) if rows else ())

    def events(self, code, *, knowledge_cutoff=None):
        return DataResult('READY', self.event_rows, observation_ids=('events-proof',))

    def event_coverage(self, code, start, end, *, knowledge_cutoff=None):
        return DataResult('READY' if self.coverage else 'WAITING_DATA', {'start': start, 'end': end})


class Ledger(QueriesMixin, StatementsMixin, EvidenceMixin, LedgerKernel):
    pass


@pytest.fixture
def ledger(settings):
    market = Market()
    service = Ledger(settings, market=market, clock=lambda: datetime.fromisoformat('2026-10-29T20:00:00+08:00'), test_mode=True)
    service.initialize()
    def activate(tx):
        tx.state['simulation_start_session'] = DAY
        return {'state': 'APPLIED'}
    assert service._mutate('LLM', 'fixture-start', {}, 0, DAY, activate)['state'] == 'APPLIED'
    return service


def revision(ledger):
    return ledger._read('LLM', lambda tx: tx.state['account_revision'])


def seed(ledger, fn):
    rev = revision(ledger)
    result = ledger._mutate('LLM', f'fixture:{rev}', {'fixture': rev}, rev, DAY, fn)
    assert result['state'] == 'APPLIED', result


def buy(ledger, *, day=DAY, quantity=1000, cost=1000510, price='10', sellable=NEXT):
    def put(tx):
        tx.cash('buy-cash', 'BUY', -cost, day, 'order-buy')
        tx.append('position_lots', 'lot-buy', {'lot_id': 'lot-buy', 'code': CODE, 'quantity': quantity,
            'cost_fen': cost, 'original_quantity': quantity, 'original_cost_fen': cost,
            'acquired_date': day, 'sellable_date': sellable, 'tax_start_date': day, 'tax_start_status': 'KNOWN'})
        tx.append('lot_movements', 'move-buy', {'lot_id': 'lot-buy', 'code': CODE, 'effective_date': day,
            'quantity_delta': quantity, 'cost_delta_fen': cost, 'kind': 'BUY'})
        tx.append('fills', 'fill-buy', {'order_id': 'order-buy', 'plan_id': 'plan-buy', 'target_session': day,
            'effective_date': day, 'code': CODE, 'side': 'BUY', 'quantity': quantity, 'price': price,
            'fees_fen': cost - int(Decimal(price) * quantity * 100), 'realized_pnl_fen': 0})
        return {'state': 'APPLIED'}
    seed(ledger, put)


def close(ledger, day=DAY, op=None):
    return ledger.close_session('LLM', {'session': day}, operation_id=op or f'close:{day}:{revision(ledger)}', expected_revision=revision(ledger))


def test_empty_account_day_is_final_without_fake_trades(ledger):
    result = close(ledger)
    assert result['state'] == 'APPLIED', result
    stmt = result['statement']
    assert stmt['accounting_status'] == 'COMPLETE'
    assert stmt['valuation_status'] == 'FINAL'
    assert stmt['net_assets_fen'] == 5_000_000
    assert stmt['daily_pnl_fen'] == stmt['cumulative_pnl_fen'] == 0
    assert stmt['orders'] == stmt['fills'] == []
    assert stmt['plan_status'] == 'NO_PLAN'
    assert ledger.pending_work('LLM')['settled_through'] == DAY
    assert ledger.history('LLM')['total'] == 1
    assert ledger.history('S1')['total'] == 0


@pytest.mark.parametrize(('day', 'expected'), [('2026-10-25', 'INVALID_INPUT'), ('2026-10-31', 'NOT_DUE')])
def test_no_prestart_or_future_daily_statement(ledger, day, expected):
    result = close(ledger, day)
    assert result['state'] == expected
    assert ledger.history('LLM')['rows'] == []


def test_missing_calendar_waits_without_creating_statement(ledger):
    ledger.market.calendar_missing = True
    assert close(ledger)['state'] == 'WAITING_DATA'
    assert ledger.history('LLM')['total'] == 0


def test_current_price_conserves_assets_fees_and_costs(ledger):
    buy(ledger)
    ledger.market.price_rows[DAY] = '10.30'
    result = close(ledger)
    assert result['state'] == 'APPLIED', result
    stmt = result['statement']
    assert stmt['closing_cash_fen'] == 3_999_490
    assert stmt['net_assets_fen'] == 5_029_490
    assert stmt['cumulative_transaction_fees_fen'] == 510
    assert stmt['pnl_decomposition_fen'] == stmt['cumulative_pnl_fen'] == 29490
    assert stmt['positions'][0]['cost_fen'] == 1000510


def test_previous_price_provisional_and_corrected_statement_append(ledger):
    buy(ledger)
    ledger.market.price_rows['2026-10-23'] = '10'
    first = close(ledger)['statement']
    assert first['accounting_status'] == 'COMPLETE'
    assert first['valuation_status'] == 'PROVISIONAL'
    assert first['net_assets_fen'] is None
    assert first['provisional_assets_fen'] == 4999490
    assert first['positions'][0]['price_date'] == '2026-10-23'
    assert ledger.pending_work('LLM')['settled_through'] == DAY
    assert ledger.pending_work('LLM')['valued_through'] is None
    ledger.market.price_rows[DAY] = '10.20'
    second = close(ledger)['statement']
    assert second['statement_revision'] == 2
    assert second['supersedes_revision'] == 1
    assert second['net_assets_fen'] == 5019490
    assert ledger.daily_statement('LLM', DAY, statement_revision=1) == first
    assert ledger.history('LLM')['total'] == 1
    assert ledger.history('LLM', include_revisions=True)['total'] == 2


@pytest.mark.parametrize('problem', ['none', 'corrupt', 'ex_date', 'undated', 'coverage'])
def test_untrusted_or_incomparable_old_price_never_becomes_zero(ledger, problem):
    buy(ledger)
    if problem != 'none':
        ledger.market.price_rows['2026-10-23'] = '10'
    if problem == 'corrupt':
        ledger.market.corrupt = True
    elif problem == 'ex_date':
        ledger.market.event_rows = [{'ex_date': DAY}]
    elif problem == 'undated':
        ledger.market.event_rows = [{'ex_date': None}]
    elif problem == 'coverage':
        ledger.market.coverage = False
    stmt = close(ledger)['statement']
    assert stmt['valuation_status'] == 'UNAVAILABLE'
    assert stmt['net_assets_fen'] is stmt['market_value_fen'] is stmt['daily_pnl_fen'] is None
    assert stmt['positions'][0]['market_value_fen'] is None


def test_missing_earlier_day_blocks_final_until_ordered_recovery(ledger):
    result = close(ledger, NEXT)['statement']
    assert result['accounting_status'] == 'PARTIAL'
    assert ledger.pending_work('LLM')['settled_through'] is None
    close(ledger, DAY)
    repaired = close(ledger, NEXT)['statement']
    assert repaired['accounting_status'] == 'COMPLETE'
    assert repaired['daily_pnl_fen'] == 0
    assert ledger.pending_work('LLM')['settled_through'] == NEXT


def test_waiting_order_keeps_cash_and_does_not_create_fill(ledger):
    def put(tx):
        tx.append('orders', 'order', {'order_id': 'order', 'code': CODE, 'plan_id': 'p', 'target_session': DAY,
                                    'status': 'WAITING_DATA'})
        tx.append('reservations', 'order', {'order_id': 'order', 'code': CODE, 'side': 'BUY',
                                           'cash_fen': 1000500, 'status': 'ACTIVE'})
        return {'state': 'APPLIED'}
    seed(ledger, put)
    result = close(ledger)['statement']
    assert result['accounting_status'] == 'PARTIAL'
    assert result['closing_cash_fen'] == 5000000
    assert result['fills'] == []
    view = ledger.account_state('LLM', '2026-10-30')
    assert view['reserved_cash_fen'] == 1000500
    assert not view['can_plan']


def test_sellable_own_reservation_and_historical_lots(ledger):
    buy(ledger)
    def put(tx):
        tx.append('reservations', 'sell', {'order_id': 'sell', 'code': CODE, 'side': 'SELL',
                                         'shares': 1000, 'status': 'ACTIVE'})
        return {'state': 'APPLIED'}
    seed(ledger, put)
    assert ledger.positions('LLM', DAY)[0]['sellable_shares'] == 0
    assert ledger.positions('LLM', NEXT)[0]['sellable_shares'] == 0
    assert ledger.positions('LLM', NEXT, own_order_id='sell')[0]['sellable_shares'] == 1000
    assert ledger.lots('LLM', DAY)[0]['cost_fen'] == 1000510


def test_known_future_cash_receivable_does_not_block_accounting(ledger):
    def put(tx):
        tx.append('entitlements', 'cash', {'case_id': 'case', 'code': CODE, 'kind': 'CASH',
            'recognition_date': DAY, 'payment_date': '2026-11-02', 'amount_fen': 1000, 'paid_fen': 0,
            'status': 'RECEIVABLE'})
        return {'state': 'APPLIED'}
    seed(ledger, put)
    stmt = close(ledger)['statement']
    assert stmt['accounting_status'] == 'COMPLETE'
    assert stmt['cash_receivable_fen'] == 1000
    assert stmt['net_assets_fen'] == 5001000
    assert stmt['cumulative_dividend_income_fen'] == 1000


def test_unknown_tax_blocks_final_nav_without_swallowing_dividend(ledger):
    def put(tx):
        tx.append('entitlements', 'cash', {'case_id': 'case', 'code': CODE, 'kind': 'CASH',
            'recognition_date': DAY, 'payment_date': NEXT, 'amount_fen': 1000, 'paid_fen': 0,
            'status': 'RECEIVABLE'})
        tx.append('tax_items', 'unknown', {'case_id': 'case', 'code': CODE, 'trigger_date': DAY,
            'assessed_fen': None, 'paid_fen': 0, 'status': 'WAITING_DATA'})
        return {'state': 'APPLIED'}
    seed(ledger, put)
    stmt = close(ledger)['statement']
    assert stmt['net_assets_fen'] is None
    assert stmt['known_assets_fen'] == 5001000
    assert stmt['cash_receivable_fen'] == 1000
    assert not ledger.account_state('LLM', '2026-10-30')['can_plan']


def test_paid_dividend_and_tax_history_preserves_prior_receivable(ledger):
    def put(tx):
        tx.append('entitlements', 'cash', {'case_id': 'case', 'code': CODE, 'kind': 'CASH',
            'recognition_date': DAY, 'payment_date': NEXT, 'amount_fen': 1000, 'paid_fen': 1000,
            'paid_date': NEXT, 'payments': [{'effective_date': NEXT, 'amount_fen': 1000}], 'status': 'PAID'})
        tx.append('tax_items', 'tax', {'case_id': 'case', 'code': CODE, 'trigger_date': DAY,
            'assessed_fen': 200, 'paid_fen': 200, 'status': 'PAID',
            'assessments': [{'effective_date': DAY, 'amount_fen': 200}],
            'payments': [{'effective_date': NEXT, 'amount_fen': 200}]})
        tx.cash('div', 'DIVIDEND', 1000, NEXT, 'case')
        tx.cash('tax', 'DIVIDEND_TAX', -200, NEXT, 'tax')
        return {'state': 'APPLIED'}
    seed(ledger, put)
    before = close(ledger, DAY)['statement']
    after = close(ledger, NEXT)['statement']
    assert before['cash_receivable_fen'] == 1000
    assert before['tax_payable_fen'] == 200
    assert after['cash_receivable_fen'] == after['tax_payable_fen'] == 0
    assert before['net_assets_fen'] == after['net_assets_fen'] == 5000800
    assert before['cumulative_tax_expense_fen'] == after['cumulative_tax_expense_fen'] == 200
    assert after['daily_pnl_fen'] == 0


def test_old_pending_event_remains_required_after_all_stock_sold(ledger):
    def put(tx):
        tx.append('corporate_action_cases', 'case', {'case_id': 'case', 'code': CODE,
            'record_date': '2020-01-02', 'event_ids': ['old'], 'status': 'REGISTERED'})
        tx.append('entitlements', 'cash', {'case_id': 'case', 'code': CODE, 'kind': 'CASH',
            'recognition_date': '2020-01-03', 'amount_fen': 1000, 'paid_fen': 0, 'status': 'RECEIVABLE'})
        return {'state': 'APPLIED'}
    seed(ledger, put)
    result = ledger.required_market_inputs('LLM', DAY)
    assert result['holdings'] == []
    assert result['pending_actions'] == [CODE]
    assert result['pending_event_ranges'] == [{'code': CODE, 'start': '2020-01-02', 'end': DAY, 'event_ids': ['old']}]


def test_reports_exports_and_queries_are_readonly_consistent_and_bounded(ledger):
    close(ledger, DAY)
    close(ledger, NEXT)
    before = ledger.settings.database.read_bytes()
    snap = ledger.report_snapshot('LLM', NEXT)
    export = ledger.export_rows('LLM', 'statements', offset=1, limit=1)
    assert export['report_revision'] == snap['report_revision']
    assert export['total'] == 2 and export['rows'][0]['session'] == NEXT
    assert ledger.fills('LLM')['rows'] == ledger.orders('LLM')['rows'] == []
    assert ledger.settings.database.read_bytes() == before
    with pytest.raises(ValueError):
        ledger.export_rows('LLM', limit=10001)
    with pytest.raises(ValueError):
        ledger.history('LLM', start=NEXT, end=DAY)


def test_known_due_unpaid_cash_is_partial_but_not_zero(ledger):
    def put(tx):
        tx.append('entitlements', 'cash', {'case_id': 'case', 'code': CODE, 'kind': 'CASH',
            'recognition_date': DAY, 'payment_date': DAY, 'amount_fen': 1000, 'paid_fen': 0,
            'status': 'RECEIVABLE'})
        return {'state': 'APPLIED'}
    seed(ledger, put)
    stmt = close(ledger)['statement']
    assert stmt['accounting_status'] == 'PARTIAL'
    assert stmt['cash_receivable_fen'] == 1000
    assert stmt['net_assets_fen'] is None
    assert stmt['provisional_assets_fen'] == 5001000


def test_historical_tax_group_uses_each_assessment_date(ledger):
    def put(tx):
        tx.append('tax_items', 'tax', {'case_id': 'case', 'code': CODE, 'trigger_date': NEXT,
            'assessed_fen': 300, 'paid_fen': 0, 'status': 'PAYABLE', 'due_date': '2026-11-03',
            'assessments': [{'effective_date': DAY, 'amount_fen': 200},
                            {'effective_date': NEXT, 'amount_fen': 100}], 'payments': []})
        return {'state': 'APPLIED'}
    seed(ledger, put)
    first = close(ledger, DAY)['statement']
    later = close(ledger, NEXT)['statement']
    assert first['tax_payable_fen'] == 200
    assert later['tax_payable_fen'] == 300
    assert first['net_assets_fen'] == 4999800
    assert later['daily_pnl_fen'] == -100


def test_known_dividend_unregistered_prevents_false_final_statement(ledger):
    buy(ledger)
    ledger.market.price_rows[DAY] = '10'
    ledger.market.event_rows = [{'event_id': 'unregistered', 'record_date': DAY,
                                'ex_date': NEXT, 'cash_per_share': '0.1'}]
    stmt = close(ledger)['statement']
    assert stmt['accounting_status'] == 'PARTIAL'
    assert stmt['net_assets_fen'] is None
    assert any(i['kind'] == 'UNREGISTERED_OR_AMBIGUOUS_EVENT' for i in stmt['issues'])


def test_old_event_when_account_had_no_stock_does_not_block(ledger):
    buy(ledger)
    ledger.market.price_rows[DAY] = '10'
    ledger.market.event_rows = [{'event_id': 'old', 'record_date': '2020-01-02',
                                'ex_date': '2020-01-03', 'cash_per_share': '0.1'}]
    assert close(ledger)['statement']['accounting_status'] == 'COMPLETE'


def test_known_due_share_delivery_cannot_be_skipped(ledger):
    buy(ledger)
    ledger.market.price_rows[DAY] = '10'
    event = {'event_id': 'shares', 'record_date': DAY, 'ex_date': DAY, 'share_listing_date': DAY,
             'cash_per_share': '0', 'bonus_per_share': '0', 'capital_per_share': '0.1'}
    ledger.market.event_rows = [event]
    def put(tx):
        tx.append('corporate_action_cases', 'case', {'case_id': 'case', 'code': CODE,
            'record_date': DAY, 'event_ids': ['shares'], 'status': 'REGISTERED',
            'actions': {'eligibility': {'state': 'APPLIED'}, 'share_accrual': {'state': 'APPLIED'}}})
        return {'state': 'APPLIED'}
    seed(ledger, put)
    stmt = close(ledger)['statement']
    assert any(i.get('operation') == 'share_delivery' for i in stmt['issues'])
    assert stmt['accounting_status'] == 'PARTIAL'


def test_declared_model_failure_is_not_accounting_failure(ledger):
    def put(tx):
        tx.append('job_runs', 'model-failed', {'kind': 'NO_PLAN', 'target_session': DAY,
            'status': 'MODEL_FAILED', 'reason': 'MODEL_ATTEMPTS_EXHAUSTED'})
        return {'state': 'APPLIED'}
    seed(ledger, put)
    stmt = close(ledger)['statement']
    assert stmt['plan_status'] == 'MODEL_FAILED'
    assert stmt['accounting_status'] == 'COMPLETE'


def test_repeated_close_same_operation_id_has_no_new_revision(ledger):
    first = close(ledger, op='same-close')
    second = ledger.close_session('LLM', {'session': DAY}, operation_id='same-close', expected_revision=1)
    assert second['state'] == 'ALREADY_APPLIED'
    assert second['commit_id'] == first['commit_id']
    assert ledger.history('LLM', include_revisions=True)['total'] == 1


def test_late_correction_masks_old_final_and_restates_in_order(ledger):
    old_first = close(ledger, DAY)['statement']
    close(ledger, NEXT)
    def late(tx):
        tx.cash('late-cash', 'CASH_ADJUSTMENT', 1000, DAY, 'late')
        tx.append('corrections', 'late', {'effective_date': DAY, 'delta_fen': 1000})
        tx.state['restate_from'], tx.state['restate_through'] = DAY, NEXT
        return {'state': 'APPLIED'}
    seed(ledger, late)
    stale = ledger.daily_statement('LLM', DAY)
    assert stale['net_assets_fen'] is None
    assert stale['requires_restatement'] is True
    assert stale['published_net_assets_fen'] == 5000000
    assert ledger.daily_statement('LLM', DAY, statement_revision=1) == old_first
    assert not ledger.account_state('LLM', '2026-10-30')['can_plan']
    first = close(ledger, DAY)['statement']
    assert first['net_assets_fen'] == 5001000
    assert ledger.pending_work('LLM')['restate_from'] == NEXT
    assert ledger.daily_statement('LLM', NEXT)['net_assets_fen'] is None
    second = close(ledger, NEXT)['statement']
    assert second['net_assets_fen'] == 5001000
    assert ledger.pending_work('LLM')['restate_from'] is None
    assert ledger.pending_work('LLM')['valued_through'] == NEXT


def test_rebalance_window_expires_without_extending_during_offline(ledger):
    def put(tx):
        tx.append('rebalance_tasks', 'expired', {'task_id': 'expired', 'status': 'ACTIVE',
            'start_session': '2026-10-19', 'end_session': '2026-10-23',
            'targets': [{'code': CODE, 'side': 'BUY', 'quantity': 1000}],
            'completed_quantities': {f'{CODE}:BUY': 300}})
        return {'state': 'APPLIED'}
    seed(ledger, put)
    pending = ledger.pending_work('LLM')
    assert pending['rebalance_tasks'] == []
    assert pending['expired_rebalance_tasks'][0]['remaining_targets'][0]['quantity'] == 700
    assert ledger.report_snapshot('LLM')['rebalance_tasks'][0]['status'] == 'EXPIRED'


def test_real_public_data_daily_valuation_is_offline_and_preserves_market_index(ledger, monkeypatch):
    import socket

    from test_market_service import bar, calendar_days, source

    from china_stock_sim.market.repository import MarketRepository
    from china_stock_sim.market.service import MarketDataService

    def no_network(*args, **kwargs):
        raise AssertionError('Daily ledger must use the public cached data service')

    monkeypatch.setattr(socket.socket, 'connect', no_network)
    repository = MarketRepository(ledger.root)
    rows = calendar_days(DAY, '2026-10-29', {DAY, NEXT, '2026-10-28', '2026-10-29'})
    repository.put('calendar', rows, source(rows, fetched='2026-10-25T18:10:00+08:00'))
    bars = [bar(day=DAY, close='10.30')]
    repository.put('bar', bars, source(bars, fetched='2026-10-26T18:10:00+08:00'))
    coverage = [{'code': CODE, 'purpose': 'events', 'start': DAY, 'end': DAY, 'complete': True}]
    repository.put('coverage', coverage, source(coverage, fetched='2026-10-26T18:10:00+08:00'))
    ledger.market = MarketDataService(ledger.root)
    buy(ledger)
    before = repository.path.read_bytes()
    result = close(ledger)
    assert result['state'] == 'APPLIED', result
    assert result['statement']['net_assets_fen'] == 5029490
    assert result['statement']['data_evidence']
    assert all(item['result']['observation_ids'] for item in result['statement']['data_evidence'])
    assert repository.path.read_bytes() == before


def test_received_model_response_is_pending_validation_not_another_call(ledger):
    def put(tx):
        tx.append('model_attempts', 'attempt', {'attempt_id': 'attempt', 'target_session': NEXT,
            'status': 'RECEIVED', 'attempt_number': 1, 'response_document': {'path': 'fixture.json'}})
        return {'state': 'APPLIED'}
    seed(ledger, put)
    assert ledger.pending_work('LLM')['model_attempts'][0]['status'] == 'RECEIVED'


def test_provisional_valuation_cannot_authorize_new_nav_dependent_plan(ledger):
    buy(ledger)
    ledger.market.price_rows['2026-10-23'] = '10'
    close(ledger)
    ledger._clock = lambda: datetime.fromisoformat('2026-10-26T20:00:00+08:00')
    state = ledger.account_state('LLM', NEXT)
    assert state['settled_through'] == DAY
    assert state['valuation_status'] == 'PROVISIONAL'
    assert not state['can_plan']
    assert any(reason['kind'] == 'VALUATION_NOT_FINAL' for reason in state['blocking_reasons'])


def test_settled_cases_and_resolved_potential_taxes_leave_pending_queue(ledger):
    def put(tx):
        tx.append('corporate_action_cases', 'case', {'case_id': 'case', 'code': CODE,
            'record_date': DAY, 'event_ids': ['event'], 'status': 'REGISTERED',
            'actions': {'cash_accrual': {'state': 'APPLIED'}, 'share_accrual': {'state': 'NOT_APPLICABLE'}}})
        tx.append('entitlements', 'cash', {'case_id': 'case', 'code': CODE, 'kind': 'CASH',
            'recognition_date': DAY, 'payment_date': NEXT, 'amount_fen': 1000, 'paid_fen': 1000,
            'paid_date': NEXT, 'status': 'PAID'})
        tx.append('tax_items', 'tax', {'case_id': 'case', 'code': CODE, 'status': 'PAID',
                                     'assessed_fen': 200, 'paid_fen': 200})
        tx.append('tax_items', 'potential', {'case_id': 'case', 'code': CODE, 'status': 'RESOLVED',
                                           'assessed_fen': 0, 'paid_fen': 0})
        return {'state': 'APPLIED'}
    seed(ledger, put)
    pending = ledger.pending_work('LLM')
    assert pending['cases'] == pending['tax_items'] == pending['entitlements'] == []
    assert ledger.required_market_inputs('LLM', NEXT)['pending_event_ranges'] == []
