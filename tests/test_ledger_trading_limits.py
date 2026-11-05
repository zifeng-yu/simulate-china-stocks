"""A03/A05/A06 boundary acceptance through public ledger commands."""

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

import pytest
from test_ledger_trading import (
    CODE,
    DAY,
    Facts,
    buy_and_close,
    call,
    execute_payload,
    plan_payload,
    revision,
)
from test_ledger_trading import book as book

from china_stock_sim.ledger import LedgerService


def order(code=CODE, *, side='BUY', quantity=100, price='10.00'):
    return {'code': code, 'side': side, 'quantity': quantity, 'limit_price': price, 'reason': 'isolated acceptance'}


def freeze(book, day, orders):
    saved = call(book, 'freeze_plan', plan_payload(book, day, orders))
    assert saved['state'] == 'APPLIED', saved
    return book.get_plan('LLM', saved['plan_id'])


def execute_and_close(book, plan, *, price='10.00'):
    day = plan['target_session']
    book.test_now[0] = datetime.fromisoformat(f'{day}T20:00:00+08:00')
    for item in plan['orders']:
        result = call(book, 'apply_execution_results', execute_payload(book, plan, item, price=price))
        assert result['state'] == 'APPLIED', result
    result = call(book, 'close_session', {'session': day, 'knowledge_cutoff': book._now().isoformat()})
    assert result['state'] == 'APPLIED', result


@pytest.mark.parametrize('count', [5, 6])
def test_five_symbols_allowed_six_rejected_atomically(book, count):
    orders = [order(f'SH60000{i}') for i in range(count)]
    before = book.settings.database.read_bytes()
    result = call(book, 'freeze_plan', plan_payload(book, DAY, orders))
    if count == 5:
        assert result['state'] == 'APPLIED', result
        assert len(book.orders('LLM')['rows']) == 5
        assert book.account_state('LLM', DAY)['reserved_cash_fen'] == 5 * 100501
    else:
        assert result['state'] == 'INVALID_INPUT', result
        assert book.orders('LLM')['rows'] == []
        assert book.settings.database.read_bytes() == before


def test_same_open_sale_committed_first_never_expands_frozen_buy_budget(book):
    first = freeze(book, DAY, [order(quantity=4000)])
    execute_and_close(book, first)
    assert book.account_state('LLM', '2026-11-02')['cash_fen'] == 998760
    # Even a full same-open sale cannot justify a buy that exceeds pre-open cash.
    excessive = plan_payload(book, '2026-11-02', [order(side='SELL', quantity=4000), order('SH600001', quantity=1000)])
    result = call(book, 'freeze_plan', excessive)
    assert result['state'] == 'INVALID_INPUT', result
    assert book.get_plan('LLM', target_session='2026-11-02') is None
    second = freeze(book, '2026-11-02', [order(side='SELL', quantity=4000), order('SH600001', quantity=900)])
    budget = second['buy_budget_fen']
    assert budget == 900509
    book.test_now[0] = datetime.fromisoformat('2026-11-02T20:00:00+08:00')
    sold = call(book, 'apply_execution_results', execute_payload(book, second, second['orders'][0]))
    assert sold['state'] == 'APPLIED', sold
    assert book.account_state('LLM', '2026-11-03')['cash_fen'] > budget
    attempted_rewrite = execute_payload(book, second, second['orders'][1])
    attempted_rewrite['quantity'] = 1000
    rejected = call(book, 'apply_execution_results', attempted_rewrite)
    assert rejected['state'] == 'INVALID_INPUT'
    assert book.get_plan('LLM', second['plan_id'])['buy_budget_fen'] == budget
    bought = call(book, 'apply_execution_results', execute_payload(book, second, second['orders'][1]))
    assert bought['state'] == 'APPLIED', bought
    fills = [f for f in book.fills('LLM')['rows'] if f['plan_id'] == second['plan_id'] and f['side'] == 'BUY']
    assert len(fills) == 1 and -fills[0]['net_cash_fen'] == budget
    assert fills[0]['quantity'] == 900


def test_fifo_two_batches_partial_consumption_and_final_remainder_conserve_every_fen(book):
    first = freeze(book, DAY, [order(quantity=300, price='10.01')])
    execute_and_close(book, first, price='10.01')
    first_lot = book.lots('LLM')[0]
    second = freeze(book, '2026-11-02', [order(quantity=300, price='10.03')])
    execute_and_close(book, second, price='10.03')
    lots = book.lots('LLM')
    second_lot = next(lot for lot in lots if lot['lot_id'] != first_lot['lot_id'])
    original_cost = sum(lot['cost_fen'] for lot in lots)
    partial_cost = int((Decimal(second_lot['cost_fen']) / 3).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    sell = freeze(book, '2026-11-03', [order(side='SELL', quantity=400)])
    execute_and_close(book, sell)
    fill = next(f for f in book.fills('LLM')['rows'] if f['plan_id'] == sell['plan_id'])
    assert fill['lot_allocations'] == [
        {'lot_id': first_lot['lot_id'], 'quantity': 300, 'cost_fen': first_lot['cost_fen'],
         'acquired_date': DAY, 'tax_start_date': DAY, 'tax_start_status': 'KNOWN'},
        {'lot_id': second_lot['lot_id'], 'quantity': 100, 'cost_fen': partial_cost,
         'acquired_date': '2026-11-02', 'tax_start_date': '2026-11-02', 'tax_start_status': 'KNOWN'},
    ]
    remaining = book.positions('LLM', '2026-11-03')[0]
    assert remaining['total_shares'] == 200
    assert fill['cost_fen'] + remaining['cost_fen'] == original_cost
    final = freeze(book, '2026-11-04', [order(side='SELL', quantity=200)])
    execute_and_close(book, final)
    sold = [f for f in book.fills('LLM')['rows'] if f['side'] == 'SELL']
    assert sum(f['cost_fen'] for f in sold) == original_cost
    assert book.positions('LLM', '2026-11-04') == []
    assert all(lot['quantity'] == 0 and lot['cost_fen'] == 0 for lot in book.lots('LLM'))


def test_odd_lot_remainder_can_be_sold_with_whole_lots_without_liquidating_entire_position(book):
    buy_and_close(book)
    # An already delivered, integral corporate-action batch in this isolated fixture.
    def shares(tx):
        lot = {'lot_id': 'fixture-bonus', 'code': CODE, 'quantity': 150, 'cost_fen': 0,
            'original_quantity': 150, 'original_cost_fen': 0, 'acquired_date': DAY,
            'sellable_date': DAY, 'tax_start_date': DAY, 'tax_start_status': 'KNOWN',
            'source_case_id': 'fixture-legal-bonus'}
        tx.append('position_lots', lot['lot_id'], lot)
        tx.append('lot_movements', 'fixture-bonus', {'lot_id': lot['lot_id'], 'code': CODE,
            'effective_date': DAY, 'quantity_delta': 150, 'cost_delta_fen': 0,
            'kind': 'SHARE_DELIVERY', 'reference_id': 'fixture-legal-bonus'})
        return {'state': 'APPLIED'}
    seeded = book._mutate('LLM', 'fixture-shares', {}, revision(book), DAY, shares)
    assert seeded['state'] == 'APPLIED'
    for bad_quantity in (25, 125, 175):
        invalid = call(book, 'freeze_plan', plan_payload(book, '2026-11-02', [order(side='SELL', quantity=bad_quantity)]))
        assert invalid['state'] == 'INVALID_INPUT', invalid
    accepted = freeze(book, '2026-11-02', [order(side='SELL', quantity=150)])
    execute_and_close(book, accepted)
    assert book.positions('LLM', '2026-11-02')[0]['total_shares'] == 1000


def test_t_plus_one_uses_explicit_holiday_calendar_not_next_weekday(settings):
    market = Facts()
    # Explicit exchange calendar fixture: the next session after Sep 30 is Oct 8.
    market.calendar_rows = [{'date': day, 'is_trading_day': status} for day, status in [
        ('2027-09-29', '1'), ('2027-09-30', '1'), ('2027-10-01', '0'),
        ('2027-10-02', '0'), ('2027-10-03', '0'), ('2027-10-04', '0'),
        ('2027-10-05', '0'), ('2027-10-06', '0'), ('2027-10-07', '0'), ('2027-10-08', '1')]]
    market.calendar_rows = [{'date': f'2027-09-{d}', 'is_trading_day': '1'}
                            for d in range(20, 29)] + market.calendar_rows
    current = [datetime.fromisoformat('2027-09-29T20:00:00+08:00')]
    service = LedgerService(settings, market=market, clock=lambda: current[0], test_mode=True)
    service.test_now = current
    service.initialize()
    assert call(service, 'activate_simulation', {'target_session': '2027-09-30'})['state'] == 'APPLIED'
    plan = freeze(service, '2027-09-30', [order(quantity=1000)])
    execute_and_close(service, plan)
    lot = service.lots('LLM')[0]
    assert lot['sellable_date'] == '2027-10-08'
    assert service.positions('LLM', '2027-10-07')[0]['sellable_shares'] == 0
    assert service.positions('LLM', '2027-10-08')[0]['sellable_shares'] == 1000
