"""Independent late-input recovery boundaries through real isolated services."""

from execution_fixtures import CODES, FIRST, advance, execution_book, set_quote
from test_strategy_acceptance import response, strategy_world, tree_state
from test_workflow_application import application

assert execution_book and strategy_world

ACCOUNTS = ('S1', 'S1-LT', 'LLM')
NEXT = '2026-11-02'


def completed_three_accounts(world):
    book, app, model = application(world)
    assert app.run()['state'] == 'COMPLETE'
    assert len(model.calls) == 1
    advance(book, FIRST)
    for strategy in book.settings.raw['strategies'].values():
        strategy['enabled'] = False
    settled = app.run()
    assert settled['state'] == 'COMPLETE', settled
    for account in ACCOUNTS:
        statement = book.daily_statement(account, FIRST)
        assert statement['accounting_status'] == 'COMPLETE'
        assert statement['valuation_status'] == 'FINAL'
        assert book.fills(account)['total'] > 0
    return book, app, model


def late_price(book):
    advance(book, FIRST, at='21:00:00')
    set_quote(book, CODES[0], FIRST, price='11.00', preclose='10.00',
              fetched=book._now().isoformat())


def test_failed_input_refresh_blocks_only_its_account_restatement_and_planning(
        strategy_world, monkeypatch):
    book, app, model = completed_three_accounts(strategy_world)
    original_statements = {a: book.daily_statement(a, FIRST) for a in ACCOUNTS}
    original_fills = {a: book.fills(a)['rows'] for a in ACCOUNTS}
    failed_revision = book.account_state('S1', FIRST)['account_revision']
    late_price(book)
    for strategy in book.settings.raw['strategies'].values():
        strategy['enabled'] = True
    model.responses.append(response(NEXT, intents=[]))
    refresh = book.refresh_statement_inputs
    refreshed = []

    def fail_one(account, payload, **kwargs):
        refreshed.append(account)
        if account == 'S1':
            return {'state': 'INTEGRITY_ERROR', 'reason': 'isolated input audit failure'}
        return refresh(account, payload, **kwargs)

    monkeypatch.setattr(book, 'refresh_statement_inputs', fail_one)
    result = app.run()
    assert result['state'] == 'PARTIAL', result
    assert set(refreshed) == set(ACCOUNTS)
    assert book.daily_statement('S1', FIRST) == original_statements['S1']
    assert book.account_state('S1', FIRST)['account_revision'] == failed_revision
    assert book.get_plan('S1', target_session=NEXT) is None
    assert book.get_strategy_decision('S1', target_session=NEXT) is None
    assert any(r.get('account_id') == 'S1' and r['kind'] == 'ACCOUNT_UNAVAILABLE'
               for r in result['reasons'])
    for account in ('S1-LT', 'LLM'):
        current = book.daily_statement(account, FIRST)
        assert current['statement_revision'] > original_statements[account]['statement_revision']
        assert current['valuation_status'] == 'FINAL'
        assert current['market_value_fen'] > original_statements[account]['market_value_fen']
        assert book.get_plan(account, target_session=NEXT) is not None
        assert book.pending_work(account)['restate_from'] is None
    assert {a: book.fills(a)['rows'] for a in ACCOUNTS} == original_fills
    assert len(model.calls) == 2


def test_execution_disabled_inspection_detects_late_input_without_financial_writes(
        strategy_world, monkeypatch):
    book, app, model = completed_three_accounts(strategy_world)
    late_price(book)
    book.settings.raw['execution']['enabled'] = False
    for strategy in book.settings.raw['strategies'].values():
        strategy['enabled'] = True
    original = {a: {'revision': book.account_state(a, FIRST)['account_revision'],
                    'statement': book.daily_statement(a, FIRST),
                    'fills': book.fills(a)['rows']}
                for a in ACCOUNTS}
    before = tree_state(book.root)
    inspection = app.inspect()
    assert tree_state(book.root) == before
    accounts = {r['account_id']: r for r in inspection['accounts']}
    assert all(accounts[a]['due_sessions'] == [FIRST] for a in ACCOUNTS)

    def must_not_refresh(*args, **kwargs):
        raise AssertionError('execution-disabled workflow must not invalidate statements')

    monkeypatch.setattr(book, 'refresh_statement_inputs', must_not_refresh)
    result = app.run()
    assert result['state'] == 'PARTIAL', result
    for account in ACCOUNTS:
        assert book.account_state(account, FIRST)['account_revision'] == original[account]['revision']
        assert book.daily_statement(account, FIRST) == original[account]['statement']
        assert book.fills(account)['rows'] == original[account]['fills']
        assert book.get_plan(account, target_session=NEXT) is None
        assert book.get_strategy_decision(account, target_session=NEXT) is None
        assert book.pending_work(account)['restate_from'] is None
    assert len(model.calls) == 1
