"""CR02/03/05/06: daily obligations and semantic close, isolated ledgers only."""

from copy import deepcopy
from datetime import datetime

import pytest
from execution_fixtures import FIRST, advance, execution_book, set_coverage, set_quote, source
from test_ledger_cross_layer import put_event
from test_ledger_queries import CODE, DAY, NEXT, buy, close, ledger, revision, seed
from test_workflow_acceptance import buy_first, workflow_ledger

from china_stock_sim.application.workflow_ledger import LedgerWorkflow
from china_stock_sim.common.event_accounting_scope import event_accounting_scope
from china_stock_sim.market.contracts import DataResult
from china_stock_sim.market.events import normalize_baostock_events
from china_stock_sim.market.repository import MarketRepository

assert ledger and execution_book


def event(**changes):
    return {'event_id': 'pending', 'code': CODE, 'kind': 'DISTRIBUTION',
        'status': 'PROPOSED', 'proposal_date': '2026-09-01', 'record_date': None,
        'ex_date': None, 'payment_date': None, 'share_listing_date': None,
        'cash_per_share': '0.1', 'bonus_per_share': '0', 'capital_per_share': '0',
        'conflicts': [], **changes}


def prepare(book, **changes):
    buy(book)
    book.market.price_rows[DAY] = '10'
    book.market.event_rows = [event(**changes)]


def counts(book):
    return book._read('LLM', lambda tx: {
        'state': deepcopy(tx.state), **{table: tx.db.execute(
            f'SELECT COUNT(*) FROM {table} WHERE account_id=?', ('LLM',)).fetchone()[0]
            for table in ('daily_nav', 'daily_positions', 'operations', 'fills', 'cash_entries')}})


@pytest.mark.parametrize('status', ['PROPOSED', 'CANCELLED'])
@pytest.mark.parametrize('future', [False, True])
def test_unimplemented_ordinary_event_does_not_block_or_create_asset(ledger, status, future):
    prepare(ledger, status=status, record_date=NEXT if future else None)
    before = counts(ledger)
    statement = close(ledger)['statement']
    assert statement['accounting_status'] == 'COMPLETE'
    assert statement['valuation_status'] == 'FINAL'
    assert statement['cash_receivable_fen'] == 0
    assert statement['net_assets_fen'] == 4_999_490
    assert counts(ledger)['cash_entries'] == before['cash_entries']
    assert ledger._read('LLM', lambda tx: tx.list('corporate_action_cases')) == []


@pytest.mark.parametrize('change', [
    {'status': 'IMPLEMENTED'}, {'status': 'UNKNOWN'}, {'kind': 'COMPLEX'},
    {'conflicts': ['CONFLICTING_CASH']},
    *({field: DAY} for field in ('record_date', 'ex_date', 'payment_date', 'share_listing_date')),
])
def test_uncertain_or_due_event_still_blocks_final_statement(ledger, change):
    prepare(ledger, **change)
    assert event_accounting_scope(ledger.market.event_rows[0], DAY) == 'REQUIRED'
    statement = close(ledger)['statement']
    assert statement['accounting_status'] == 'PARTIAL'
    assert statement['net_assets_fen'] is None
    assert statement['cash_receivable_fen'] == 0


@pytest.mark.parametrize('binding', ['alias', 'proposal'])
@pytest.mark.parametrize('status', ['PROPOSED', 'CANCELLED'])
def test_withdrawal_cannot_hide_existing_implemented_case(ledger, binding, status):
    prepare(ledger, status=status)

    def registered(tx):
        tx.append('corporate_action_cases', 'old-case', {
            'case_id': 'old-case', 'code': CODE, 'status': 'REGISTERED',
            'record_date': DAY, 'proposal_date': '2026-09-01' if binding == 'proposal' else None,
            'event_ids': ['pending' if binding == 'alias' else 'old-alias'],
            'event': event(status='IMPLEMENTED', record_date=DAY),
            'actions': {'eligibility': {'state': 'APPLIED'}}, 'eligibility': []})
        return {'state': 'APPLIED'}

    seed(ledger, registered)
    before = ledger._read('LLM', lambda tx: tx.list('corporate_action_cases'))
    statement = close(ledger)['statement']
    assert statement['accounting_status'] == 'PARTIAL'
    assert ledger._read('LLM', lambda tx: tx.list('corporate_action_cases')) == before


@pytest.mark.parametrize('incomplete', [False, True])
def test_same_economics_new_operation_and_cutoff_are_zero_write(ledger, monkeypatch, incomplete):
    prepare(ledger, status='UNKNOWN' if incomplete else 'PROPOSED')
    first = close(ledger)['statement']
    before, state = ledger.settings.database.read_bytes(), counts(ledger)
    ledger._clock = lambda: datetime.fromisoformat('2026-10-29T21:00:00+08:00')
    monkeypatch.setattr('china_stock_sim.ledger.statements.statement_record',
                        lambda *a, **k: pytest.fail('equal close must not write CAS snapshots'))
    result = ledger.close_session('LLM', {'session': DAY,
        'knowledge_cutoff': '2026-10-29T20:30:00+08:00'},
        operation_id='new-close-operation', expected_revision=revision(ledger))
    assert result['state'] == 'ALREADY_APPLIED', result
    assert result['statement'] == first
    assert counts(ledger) == state
    assert ledger.settings.database.read_bytes() == before


@pytest.mark.parametrize('change', ['price', 'event', 'correction', 'new_day', 'coverage', 'corrupt'])
def test_real_economic_or_trust_changes_are_not_deduplicated(ledger, change):
    prepare(ledger)
    first = close(ledger)['statement']
    day = DAY
    if change == 'price':
        ledger.market.price_rows[DAY] = '10.1'
    elif change == 'event':
        ledger.market.event_rows = [event(status='IMPLEMENTED', record_date=DAY)]
    elif change == 'correction':
        def corrected(tx):
            tx.cash('correction', 'CASH_ADJUSTMENT', 100, DAY, 'correction')
            tx.append('corrections', 'correction', {'effective_date': DAY, 'delta_fen': 100})
            return {'state': 'APPLIED'}
        seed(ledger, corrected)
    elif change == 'new_day':
        day = NEXT
        ledger.market.price_rows[NEXT] = '10'
    elif change == 'coverage':
        ledger.market.coverage = False
    else:
        ledger.market.corrupt = True
    result = close(ledger, day, op='changed-facts')
    assert result['state'] == 'APPLIED', result
    assert result['statement']['statement_revision'] == (1 if day != DAY else 2)
    assert ledger.daily_statement('LLM', DAY, statement_revision=1) == first
    if change in {'event', 'coverage', 'corrupt'}:
        assert result['statement']['valuation_status'] != 'FINAL'


def test_restatement_equal_nav_advances_once_without_duplicating_statement(ledger):
    first = close(ledger)['statement']
    close(ledger, NEXT)

    def invalidated(tx):
        tx.state.update(restate_from=DAY, restate_through=NEXT)
        for day in (DAY, NEXT):
            tx.append('job_runs', 'restate:' + day, {'kind': 'RESTATE_SESSION',
                'session': day, 'status': 'WAITING_DATA'})
        return {'state': 'APPLIED'}

    seed(ledger, invalidated)
    before = counts(ledger)
    result = close(ledger, op='reconcile-cursor')
    assert result['state'] == 'APPLIED' and result['recovery_progressed']
    assert result['statement'] == first and not result['statement_changed']
    after = counts(ledger)
    assert after['daily_nav'] == before['daily_nav'] == 2
    assert after['daily_positions'] == before['daily_positions'] == 2
    assert after['operations'] == before['operations'] + 1
    assert after['state']['restate_from'] == NEXT
    result = close(ledger, NEXT, op='reconcile-end')
    assert result['recovery_progressed'] and counts(ledger)['state']['restate_from'] is None
    before = ledger.settings.database.read_bytes()
    assert close(ledger, NEXT, op='repeat-end')['state'] == 'ALREADY_APPLIED'
    assert ledger.settings.database.read_bytes() == before


def test_equal_partial_statement_does_not_advance_restatement_cursor(ledger):
    prepare(ledger, status='UNKNOWN')
    close(ledger)
    def invalidated(tx):
        tx.state.update(restate_from=DAY, restate_through=DAY)
        return {'state': 'APPLIED'}
    seed(ledger, invalidated)
    before = ledger.settings.database.read_bytes()
    result = close(ledger, op='repeat-partial')
    assert result['state'] == 'ALREADY_APPLIED'
    assert counts(ledger)['state']['restate_from'] == DAY
    assert ledger.settings.database.read_bytes() == before


@pytest.mark.parametrize('change', [{'status': 'CANCELLED'}, {'status': 'PROPOSED'},
    {'status': 'UNKNOWN'}, {'kind': 'COMPLEX'}, {'conflicts': ['REVISED_AMOUNT']},
    {'cash_per_share': '0.2'}])
def test_close_alone_never_accepts_withdrawal_of_already_accounted_past_event(ledger, change):
    prepare(ledger, status='IMPLEMENTED', record_date=DAY, ex_date=DAY, payment_date=DAY,
            **{key: value for key, value in change.items() if key != 'status'})
    ledger.market.event_rows[0].update(change)
    def posted(tx):
        tx.append('corporate_action_cases', 'paid', {'case_id': 'paid', 'code': CODE,
            'record_date': DAY, 'proposal_date': '2026-09-01', 'event_ids': ['pending'],
            'status': 'REGISTERED', 'event': event(status='IMPLEMENTED', record_date=DAY),
            'actions': {action: {'state': 'APPLIED'} for action in (
                'eligibility', 'cash_accrual', 'cash_payment', 'share_accrual', 'share_delivery')}})
        tx.cash('paid-dividend', 'DIVIDEND', 100, DAY, 'paid')
        return {'state': 'APPLIED'}
    seed(ledger, posted)
    old_cash = ledger._read('LLM', lambda tx: tx.list('cash_entries'))
    stmt = close(ledger)['statement']
    assert stmt['accounting_status'] == 'PARTIAL'
    assert any(issue['kind'] == 'UNCONFIRMED_EVENT_TERMS' for issue in stmt['issues'])
    assert ledger._read('LLM', lambda tx: tx.list('cash_entries')) == old_cash


@pytest.mark.parametrize('status, expected', [('PROPOSED', 'PROVISIONAL'), ('UNKNOWN', 'UNAVAILABLE')])
def test_old_price_proposal_is_observation_but_unknown_action_still_blocks(ledger, status, expected):
    prepare(ledger, status=status)
    stmt = close(ledger, NEXT)['statement']
    assert stmt['valuation_status'] == expected
    assert stmt['positions'][0]['price_date'] == DAY


def test_registered_implemented_action_without_ex_date_cannot_be_final(ledger):
    prepare(ledger, status='IMPLEMENTED', record_date=DAY)
    def registered(tx):
        tx.append('corporate_action_cases', 'registered', {'case_id': 'registered', 'code': CODE,
            'record_date': DAY, 'event_ids': ['pending'], 'status': 'REGISTERED',
            'event': deepcopy(ledger.market.event_rows[0]),
            'actions': {'eligibility': {'state': 'APPLIED'}}})
        return {'state': 'APPLIED'}
    seed(ledger, registered)
    stmt = close(ledger)['statement']
    assert stmt['accounting_status'] == 'PARTIAL'
    assert any(issue['kind'] == 'UNKNOWN_EVENT_SCHEDULE' for issue in stmt['issues'])


def proposal_in_market(book):
    fetched = book._now().isoformat()
    raw = [{'code': 'sh.600000', 'dividPlanAnnounceDate': '2026-09-01',
        'dividCashPsBeforeTax': '0.1', 'dividStocksPs': '0', 'dividReserveToStockPs': '0'}]
    events = normalize_baostock_events(raw, 'SH600000', fetched)
    MarketRepository(book.root).put('event', events, source(raw, fetched, 'baostock'))
    return events[0]


def test_real_market_proposal_is_observed_then_implemented_and_paid(execution_book):
    book = execution_book
    buy_first(book)
    proposal_in_market(book)
    flow = workflow_ledger(book)
    raw_result = book.market.event_settlement('SH600000', FIRST, operation='eligibility',
                                            knowledge_cutoff=book._now().isoformat())
    assert raw_result.state == 'WAITING_DATA'
    assert 'EVENT_NOT_IMPLEMENTED' in raw_result.data[0]['settlement_readiness']['issues']
    after = flow.after_execution('LLM', FIRST)
    assert after['registrations'][0]['accounting_scope'] == 'OBSERVE'
    assert all(r['state'] != 'WAITING_DATA' for r in after['registrations'])
    assert book.corporate_action_cases('LLM') == []
    assert flow.close('LLM', FIRST)['statement']['valuation_status'] == 'FINAL'
    cash = book.account_state('LLM', FIRST)['cash_fen']
    advance(book, '2026-11-03')
    put_event(book, fetched=book._now().isoformat())
    flow.after_execution('LLM', FIRST)
    assert len(book.corporate_action_cases('LLM')) == 1
    flow.after_execution('LLM', '2026-11-03')
    assert book.account_state('LLM', '2026-11-03')['cash_fen'] == cash + 10000


@pytest.mark.parametrize('state, issues', [
    ('WAITING_DATA', ({'kind': 'EVENT_COVERAGE_MISSING'},)),
    ('CONFLICT', ({'kind': 'SOURCE_CONFLICT', 'event_id': 'pending'},)),
])
def test_workflow_proposal_never_hides_global_coverage_failure(ledger, state, issues):
    prepare(ledger)
    ledger.market.event_settlement = lambda **kwargs: DataResult(state, ledger.market.event_rows, issues)
    flow = LedgerWorkflow(ledger.settings, ledger=ledger, market=ledger.market, clock=ledger._now)
    result = flow.before_execution('LLM', DAY)
    assert any(row['state'] == 'WAITING_DATA' for row in result['registrations'])


def test_new_same_value_market_evidence_and_cutoff_do_not_write_ledger(execution_book, monkeypatch):
    book = execution_book
    buy_first(book)
    flow = workflow_ledger(book)
    first = flow.close('LLM', FIRST)['statement']
    before = book.settings.database.read_bytes()
    advance(book, FIRST, at='21:00:00')
    set_quote(book, 'SH600000', FIRST, '10.0000', fetched=book._now().isoformat())
    set_coverage(book, 'SH600000', FIRST, fetched=book._now().isoformat())
    monkeypatch.setattr('china_stock_sim.ledger.statements.statement_record',
                        lambda *a, **k: pytest.fail('no new snapshot for equal facts'))
    result = flow.close('LLM', FIRST)
    assert result['receipt']['state'] == 'ALREADY_APPLIED', result
    assert result['statement'] == first
    assert book.settings.database.read_bytes() == before


def test_missing_plan_uses_only_target_receipt_and_header(execution_book, monkeypatch):
    book = execution_book
    flow = workflow_ledger(book)
    advance(book, '2026-11-02')
    monkeypatch.setattr(book, 'report_snapshot', lambda *a, **k: pytest.fail('no audit read'))
    monkeypatch.setattr(book, 'get_plan', lambda *a, **k: pytest.fail('no full plan read'))
    assert flow.missing_plan('LLM', '2026-11-02')['state'] == 'APPLIED'
    before = book.settings.database.read_bytes()
    result = flow.missing_plan('LLM', '2026-11-02')
    assert result['state'] == 'ALREADY_APPLIED'
    assert result['no_plan']['target_session'] == '2026-11-02'
    assert book.settings.database.read_bytes() == before
