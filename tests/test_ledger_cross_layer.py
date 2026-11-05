"""Full LedgerService + real public market cache integration, never real accounts."""

from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest
from test_ledger_trading import CODE, DAY, call, execute_payload, plan_payload

from china_stock_sim.ledger import LedgerService
from china_stock_sim.market.events import normalize_baostock_events
from china_stock_sim.market.repository import MarketRepository
from china_stock_sim.market.service import MarketDataService


class RealMarket(MarketDataService):
    def snapshot(self, code, day, cutoff, *, execution_context=None):
        purposes = ('valuation', 'execution') if execution_context else ('valuation',)
        frozen = self.freeze([code], day, knowledge_cutoff=cutoff,
                             purposes=purposes, rule_context=execution_context)
        if execution_context:
            result = frozen['body']['content']['results'][code]['execution']
            assert result['state'] == 'READY', result
        return frozen['snapshot_id']


def raw(rows, fetched, provider='fixture'):
    return {'provider': provider, 'fetched_at': fetched, 'adapter_version': 'isolated-test',
            'rows': rows}


@pytest.fixture
def realbook(settings, monkeypatch):
    import socket

    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('no network in ledger integration'))
    repository = MarketRepository(settings.root)
    start = date(2026, 10, 20)
    calendar = [{'date': (start + timedelta(days=i)).isoformat(),
                 'is_trading_day': int((start + timedelta(days=i)).weekday() < 5)} for i in range(60)]
    repository.put('calendar', calendar, raw(calendar, '2026-10-20T18:10:00+08:00'))
    for row in calendar:
        if row['is_trading_day']:
            day = row['date']
            price = '10.30' if day > DAY else '10'
            previous = '10' if day <= '2026-11-02' else '10.30'
            bars = [{'code': CODE, 'date': day, 'open': price, 'high': price, 'low': price,
                     'close': price, 'preclose': previous, 'tradestatus': 1, 'isST': 0, 'adjustflag': 3,
                     'volume': 100000, 'amount': str(Decimal(price) * 100000)}]
            repository.put('bar', bars, raw(bars, day + 'T18:10:00+08:00'))
            # Each simulated session has its own post-close source check. A
            # check on October 29 cannot prove all of November in advance.
            coverage = [{'code': CODE, 'date': day, 'purpose': 'events', 'start': '2026-10-20',
                         'end': day, 'complete': True,
                         'scope': 'reported_cash_bonus_capital_distributions'}]
            repository.put('coverage', coverage, raw(coverage, day + 'T18:10:00+08:00'))
    security = [{'code': CODE, 'date': '2026-10-20', 'listed': '1999-11-10',
                 'name': '隔离测试证券', 'board': '沪市主板', 'source_status': 1, 'eligibility_known': True}]
    repository.put('security', security, raw(security, '2026-10-20T18:10:00+08:00'))
    clock = [datetime.fromisoformat('2026-10-29T20:00:00+08:00')]
    monkeypatch.setattr('china_stock_sim.market.service.timestamp', lambda: clock[0].isoformat())
    service = LedgerService(settings, market=RealMarket(settings.root), clock=lambda: clock[0], test_mode=True)
    service.initialize()
    service.test_now = clock
    assert call(service, 'activate_simulation', {'target_session': DAY})['state'] == 'APPLIED'
    assert call(service, 'activate_simulation', {'target_session': DAY}, account='S1')['state'] == 'APPLIED'
    return service


def put_event(book, *, bonus='0', fetched='2026-10-29T18:10:00+08:00', cash='0.1', face=None):
    rawrow = {'code': 'sh.600000', 'dividPlanAnnounceDate': '2026-09-01',
        'dividPlanDate': '2026-10-29', 'dividRegistDate': DAY,
        'dividOperateDate': '2026-11-02', 'dividPayDate': '2026-11-03',
        'dividStockMarketDate': '2026-11-04' if bonus != '0' else '',
        'dividCashPsBeforeTax': cash, 'dividStocksPs': bonus,
        'dividReserveToStockPs': '0'}
    event = normalize_baostock_events([rawrow], CODE, fetched)[0]
    repository = MarketRepository(book.settings.root)
    dependencies = []
    if face is not None:
        proof = raw([{'code': CODE, 'event_id': event['event_id'],
                      'applicable_date': event['ex_date'], 'face_value': face}], fetched, 'issuer_fixture')
        reference = repository.save_raw(proof)
        dependencies.append(reference)
        event['tax_facts']['share_face_value'] = {'state': 'READY', 'value': face, 'sources': [{
            'provider': 'issuer_fixture', 'source_field': 'face_value', 'raw_reference': reference,
            'event_id': event['event_id'], 'applicable_date': event['ex_date'], 'row_index': 0, 'value': face}]}
    repository.put('event', [event], {**raw([event], fetched, 'baostock'), 'dependencies': dependencies})
    # Ex-date preclose is the source corporate-action reference; later bars follow actual prior close.
    reference = ((Decimal('10') - Decimal(cash)) / (1 + Decimal(bonus))).quantize(
        Decimal('0.01'), rounding=ROUND_HALF_UP)
    price = '9.50' if Decimal(bonus) else '10.30'
    start = date(2026, 11, 2)
    for offset in range(35):
        session = start + timedelta(days=offset)
        if session.weekday() >= 5:
            continue
        day = session.isoformat()
        bars = [{'code': CODE, 'date': day, 'open': price, 'high': price, 'low': price,
                 'close': price, 'preclose': str(reference) if day == '2026-11-02' else price,
                 'tradestatus': 1, 'isST': 0, 'adjustflag': 3, 'volume': 100000,
                 'amount': str(Decimal(price) * 100000)}]
        available = max(day + 'T18:10:00+08:00', fetched)
        repository.put('bar', bars, raw(bars, available))
    return event


def at(book, day):
    book.test_now[0] = datetime.fromisoformat(day + 'T20:00:00+08:00')


def corporate(book, method, event, day, operation, case=None):
    cutoff = book._now().isoformat()
    kwargs = {'code': CODE, 'session': day, 'knowledge_cutoff': cutoff,
              'event_ids': [event['event_id']], 'operation': operation}
    evidence = book.market.event_settlement(**kwargs)
    payload = {'code': CODE, 'effective_date': day, 'event_ids': [event['event_id']],
        'knowledge_cutoff': cutoff, 'operation': operation,
        'data_evidence': [{'method': 'event_settlement', 'kwargs': kwargs, 'result': evidence.as_dict()}]}
    if case:
        payload['case_id'] = case
    return call(book, method, payload)


def close(book, day, account='LLM'):
    result = call(book, 'close_session', {'session': day, 'knowledge_cutoff': book._now().isoformat()}, account=account)
    assert result['state'] == 'APPLIED', result
    return book.daily_statement(account, day)


def buy(book):
    request = plan_payload(book)
    assert call(book, 'freeze_plan', request)['state'] == 'APPLIED'
    plan = book.get_plan('LLM', request['plan_id'])
    at(book, DAY)
    applied = call(book, 'apply_execution_results', execute_payload(book, plan, plan['orders'][0]))
    assert applied['state'] == 'APPLIED', applied


def freeze_sell(book, day='2026-11-02', price='10.30'):
    request = plan_payload(book, day, [{'code': CODE, 'side': 'SELL', 'quantity': 1000,
                                     'limit_price': price, 'reason': 'isolated test'}])
    result = call(book, 'freeze_plan', request)
    assert result['state'] == 'APPLIED', result
    return book.get_plan('LLM', request['plan_id'])


def test_real_full_buy_cash_sell_tax_close_report_and_account_isolation(realbook):
    book = realbook
    event = put_event(book)
    buy(book)
    result = corporate(book, 'register_entitlements', event, DAY, 'eligibility')
    assert result['state'] == 'APPLIED', result
    case = book.corporate_action_cases('LLM')[0]['case_id']
    first = close(book, DAY)
    assert first['net_assets_fen'] == 4999490
    untouched = close(book, DAY, account='S1')
    assert untouched['net_assets_fen'] == 5000000
    sale = freeze_sell(book)
    at(book, '2026-11-02')
    assert corporate(book, 'recognize_entitlements', event, '2026-11-02', 'cash_accrual', case)['state'] == 'APPLIED'
    applied = call(book, 'apply_execution_results', execute_payload(book, sale, sale['orders'][0], price='10.30'))
    assert applied['state'] == 'APPLIED', applied
    statement = close(book, '2026-11-02')
    assert statement['net_assets_fen'] == 5036465
    assert statement['cash_receivable_fen'] == 10000
    assert statement['tax_payable_fen'] == 2000
    assert book.required_market_inputs('LLM', '2026-11-02')['pending_event_ranges']
    at(book, '2026-11-03')
    assert corporate(book, 'settle_entitlements', event, '2026-11-03', 'cash_payment', case)['state'] == 'APPLIED'
    paid = close(book, '2026-11-03')
    assert paid['net_assets_fen'] == 5036465 and paid['daily_pnl_fen'] == 0
    assert paid['closing_cash_fen'] == 5038465
    at(book, '2026-11-04')
    assert call(book, 'record_tax', {'effective_date': '2026-11-04', 'payment_id': 'tax-payment'})['state'] == 'APPLIED'
    taxed = close(book, '2026-11-04')
    assert taxed['net_assets_fen'] == taxed['closing_cash_fen'] == 5036465
    assert taxed['tax_payable_fen'] == 0 and taxed['cumulative_tax_expense_fen'] == 2000
    assert taxed['daily_pnl_fen'] == 0
    report = book.report_snapshot('LLM', '2026-11-04')
    assert report['account']['cash_fen'] == 5036465
    assert report['statements'][-1]['net_assets_fen'] == 5036465
    assert sum(row['amount_fen'] for row in report['cash_entries']) == 5036465
    assert book.account_state('S1', '2026-11-05')['cash_fen'] == 5000000
    assert not book.positions('S1', '2026-11-04')
    pending = book.pending_work('LLM')
    assert pending['cases'] == [] and pending['tax_items'] == []
    assert book.required_market_inputs('LLM', '2026-11-04')['pending_event_ranges'] == []


def test_real_unknown_tax_allows_cash_payment_and_blocks_new_dependent_plan(realbook):
    book = realbook
    event = put_event(book, bonus='0.1')
    buy(book)
    assert corporate(book, 'register_entitlements', event, DAY, 'eligibility')['state'] == 'APPLIED'
    case = book.corporate_action_cases('LLM')[0]['case_id']
    close(book, DAY)
    sale = freeze_sell(book, price='9.50')
    at(book, '2026-11-02')
    for action in ('cash_accrual', 'share_accrual'):
        assert corporate(book, 'recognize_entitlements', event, '2026-11-02', action, case)['state'] == 'APPLIED'
    assert call(book, 'apply_execution_results', execute_payload(book, sale, sale['orders'][0], price='9.50'))['state'] == 'APPLIED'
    uncertain = close(book, '2026-11-02')
    assert uncertain['net_assets_fen'] is None
    assert uncertain['accounting_status'] == 'PARTIAL'
    assert any(t['assessed_fen'] is None for t in book.tax_items('LLM'))
    at(book, '2026-11-03')
    before = book.account_state('LLM', '2026-11-04')['cash_fen']
    assert corporate(book, 'settle_entitlements', event, '2026-11-03', 'cash_payment', case)['state'] == 'APPLIED'
    assert book.account_state('LLM', '2026-11-04')['cash_fen'] == before + 10000
    close(book, '2026-11-03')
    at(book, '2026-11-04')
    assert corporate(book, 'settle_entitlements', event, '2026-11-04', 'share_delivery', case)['state'] == 'APPLIED'
    assert book.positions('LLM', '2026-11-04')[0]['total_shares'] == 100
    close(book, '2026-11-04')
    state = book.account_state('LLM', '2026-11-05')
    assert not state['can_plan']
    assert any(r['kind'] == 'UNKNOWN_TAX_LIABILITY' for r in state['blocking_reasons'])
    assert book.required_market_inputs('LLM', '2026-11-04')['pending_event_ranges']


def test_real_late_share_facts_invalidate_and_restate_historical_days(realbook):
    book = realbook
    buy(book)
    close(book, DAY)
    sale = freeze_sell(book)
    at(book, '2026-11-02')
    assert call(book, 'apply_execution_results', execute_payload(book, sale, sale['orders'][0], price='10.30'))['state'] == 'APPLIED'
    original = close(book, '2026-11-02')
    assert original['net_assets_fen'] == 5028465
    at(book, '2026-11-03')
    close(book, '2026-11-03')
    at(book, '2026-11-04')
    event = put_event(book, bonus='0.1', cash='0', fetched=book._now().isoformat())
    assert corporate(book, 'register_entitlements', event, '2026-11-04', 'eligibility')['state'] == 'APPLIED'
    case = book.corporate_action_cases('LLM')[0]['case_id']
    accrued = corporate(book, 'recognize_entitlements', event, '2026-11-04', 'share_accrual', case)
    assert accrued['state'] == 'APPLIED', accrued
    marked = book.daily_statement('LLM', '2026-11-02')
    assert marked['requires_restatement'] is True
    assert marked['net_assets_fen'] is None
    assert marked['published_net_assets_fen'] == original['net_assets_fen']
    assert book.daily_statement('LLM', '2026-11-02', statement_revision=1)['net_assets_fen'] == 5028465
    assert corporate(book, 'settle_entitlements', event, '2026-11-04', 'share_delivery', case)['state'] == 'APPLIED'
    updated = close(book, '2026-11-02')
    assert updated['statement_revision'] == 2
    assert updated['share_receivable_value_fen'] == 95000
    close(book, '2026-11-03')
    delivered = close(book, '2026-11-04')
    assert delivered['positions'][0]['shares'] == 100
    # Unknown bonus tax stays explicit; absence of a final net value is the correct result.
    assert delivered['net_assets_fen'] is None
    assert delivered['known_assets_fen'] == 5123465
    assert delivered['pnl_decomposition_fen'] == 123465
    assert book.pending_work('LLM')['report_pending']
    assert book.account_state('S1', '2026-11-05')['cash_fen'] == 5000000


def test_real_late_cash_facts_restate_to_final_without_rewriting_original_fill(realbook):
    book = realbook
    buy(book)
    close(book, DAY)
    sale = freeze_sell(book)
    at(book, '2026-11-02')
    assert call(book, 'apply_execution_results', execute_payload(book, sale, sale['orders'][0], price='10.30'))['state'] == 'APPLIED'
    original = close(book, '2026-11-02')
    at(book, '2026-11-03')
    close(book, '2026-11-03')
    original_fills = book.fills('LLM')['rows']
    at(book, '2026-11-04')
    event = put_event(book, fetched=book._now().isoformat())
    assert corporate(book, 'register_entitlements', event, '2026-11-04', 'eligibility')['state'] == 'APPLIED'
    case = book.corporate_action_cases('LLM')[0]['case_id']
    assert corporate(book, 'recognize_entitlements', event, '2026-11-04', 'cash_accrual', case)['state'] == 'APPLIED'
    assert corporate(book, 'settle_entitlements', event, '2026-11-04', 'cash_payment', case)['state'] == 'APPLIED'
    assert call(book, 'record_tax', {'effective_date': '2026-11-04'})['state'] == 'APPLIED'
    for day in ('2026-11-02', '2026-11-03', '2026-11-04'):
        statement = close(book, day)
        assert statement['accounting_status'] == 'COMPLETE', statement['issues']
        assert statement['net_assets_fen'] == 5036465
        assert statement['pnl_decomposition_fen'] == 36465
    old = book.daily_statement('LLM', '2026-11-02', statement_revision=1)
    assert old['net_assets_fen'] == original['net_assets_fen'] == 5028465
    assert book.fills('LLM')['rows'] == original_fills
    assert not book.account_state('LLM', '2026-11-05').get('restate_from')


def test_real_known_bonus_tax_source_and_costs_settle_to_final(realbook):
    book = realbook
    event = put_event(book, bonus='0.1', face='1')
    buy(book)
    assert corporate(book, 'register_entitlements', event, DAY, 'eligibility')['state'] == 'APPLIED'
    case = book.corporate_action_cases('LLM')[0]['case_id']
    close(book, DAY)
    sale = freeze_sell(book, price='9.50')
    at(book, '2026-11-02')
    for action in ('cash_accrual', 'share_accrual'):
        assert corporate(book, 'recognize_entitlements', event, '2026-11-02', action, case)['state'] == 'APPLIED'
    assert call(book, 'apply_execution_results', execute_payload(book, sale, sale['orders'][0], price='9.50'))['state'] == 'APPLIED'
    first = close(book, '2026-11-02')
    assert first['net_assets_fen'] == 5049505
    assert first['tax_payable_fen'] == 4000
    at(book, '2026-11-03')
    assert corporate(book, 'settle_entitlements', event, '2026-11-03', 'cash_payment', case)['state'] == 'APPLIED'
    close(book, '2026-11-03')
    at(book, '2026-11-04')
    assert corporate(book, 'settle_entitlements', event, '2026-11-04', 'share_delivery', case)['state'] == 'APPLIED'
    shares = close(book, '2026-11-04')
    assert shares['net_assets_fen'] == 5049505
    at(book, '2026-11-05')
    assert call(book, 'record_tax', {'effective_date': '2026-11-05'})['state'] == 'APPLIED'
    statement = close(book, '2026-11-05')
    assert statement['tax_payable_fen'] == 0
    assert statement['net_assets_fen'] == 5049505
    assert statement['pnl_decomposition_fen'] == 49505
    assert sum(p['total_shares'] for p in book.positions('LLM', '2026-11-05')) == 100


@pytest.mark.parametrize('bonus,reference', [('0', '9.90'), ('0.1', '9.00')])
def test_real_ex_date_execution_snapshot_has_matching_identity_reference_and_limits(realbook, bonus, reference):
    book = realbook
    put_event(book, bonus=bonus)
    at(book, '2026-11-02')
    context = {'rule_id': 'fixture-main-board-v1', 'effective_from': '2026-01-01',
               'price_tick': '0.01', 'limit_rate': '0.1'}
    identity = book.market.snapshot(CODE, '2026-11-02', book._now().isoformat(),
                                    execution_context=context)
    snapshot = book.market.read_snapshot(identity)
    facts = snapshot['body']['content']['results'][CODE]['execution']
    assert facts['state'] == 'READY'
    assert Decimal(facts['data']['reference_price']) == Decimal(reference)
    assert facts['data']['tradestatus'] == 1
    assert facts['data']['isST'] == 0
    assert facts['data']['security']['eligibility_known'] is True
    assert facts['data']['rule_id'] == context['rule_id']


def test_real_public_rule_and_identity_recovery_preserve_backup_evidence(realbook):
    from test_ledger_corporate import configure_holding_rule

    book = realbook
    event = put_event(book, bonus='0.1', face='1')
    buy(book)
    assert corporate(book, 'register_entitlements', event, DAY, 'eligibility')['state'] == 'APPLIED'
    case = book.corporate_action_cases('LLM')[0]['case_id']
    close(book, DAY)
    at(book, '2026-11-04')
    for action in ('cash_accrual', 'share_accrual'):
        assert corporate(book, 'recognize_entitlements', event, '2026-11-04', action, case)['state'] == 'APPLIED'
    for action in ('cash_payment', 'share_delivery'):
        assert corporate(book, 'settle_entitlements', event, '2026-11-04', action, case)['state'] == 'APPLIED'
    lot = next(row for row in book._read('LLM', lambda tx: tx.list('position_lots')) if row.get('source_case_id'))
    configure_holding_rule(book, 'SHARE_DELIVERY')
    before_cash = book.account_state('LLM', '2026-11-05')['cash_fen']
    resolved = call(book, 'resolve_tax_holding_rule', {'effective_date': '2026-11-04',
                    'rule_id': 'fixture-holding', 'lot_ids': [lot['lot_id']]})
    assert resolved['state'] == 'APPLIED', resolved
    updated = book._read('LLM', lambda tx: tx.get('position_lots', lot['lot_id']))
    assert updated['tax_start_date'] == '2026-11-04' and updated['tax_start_status'] == 'KNOWN'
    invalid = call(book, 'resolve_event_identity', {'effective_date': '2026-11-04', 'code': CODE,
                    'case_id': case, 'event_ids': [event['event_id']], 'knowledge_cutoff': book._now().isoformat()})
    assert invalid['state'] == 'WAITING_DATA'
    verified = corporate(book, 'resolve_event_identity', event, '2026-11-04', 'eligibility', case)
    assert verified['state'] == 'APPLIED', verified
    assert book.account_state('LLM', '2026-11-05')['cash_fen'] == before_cash
    assert book.account_state('S1', '2026-11-05')['cash_fen'] == 5000000
    backup = book.backup()
    assert backup['state'] == 'COMPLETE', backup['evidence']['issues']
    files = {row['path'] for row in backup['evidence']['files']}
    assert resolved['document']['path'] in files
    assert verified['document']['path'] in files
