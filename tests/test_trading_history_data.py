"""Historical display boundaries; no providers, ledger writes or business runs."""

from copy import deepcopy

import pytest
from test_trading_dashboard_data import (
    CODE,
    CUTOFF,
    DAY,
    NEXT,
    PREVIOUS,
    SavedMarket,
    build,
    position,
    report,
    statement,
)

from china_stock_sim.reporting.trading_history import build_trading_history

OLD = '2026-01-05'


def trade(identity='buy', *, side='BUY', day=OLD, quantity=100, code=CODE):
    amount = quantity * 1000
    return {'id': identity, 'fill_id': identity, 'order_id': identity, 'account_id': 'S1',
        'code': code, 'side': side, 'quantity': quantity, 'price': '10.00',
        'effective_date': day, 'target_session': day, 'processed_at': CUTOFF,
        'amount_fen': amount, 'commission_fen': 500, 'stamp_fen': 0, 'transfer_fen': 0,
        'fees_fen': 500, 'net_cash_fen': -(amount + 500) if side == 'BUY' else amount - 500,
        'cost_fen': 0 if side == 'BUY' else amount,
        'realized_pnl_fen': 0 if side == 'BUY' else -500, 'storage_sequence': 1}


def lot(identity='buy', *, quantity=100, cost=100500, day=OLD, code=CODE):
    return {'id': 'buy:' + identity, 'lot_id': 'buy:' + identity, 'code': code,
        'quantity': quantity, 'cost_fen': cost, 'original_quantity': 100,
        'original_cost_fen': 100500, 'acquired_date': day, 'source_order_id': identity}


def move(identity='buy', *, qty=100, cost=100500, day=OLD, code=CODE):
    return {'id': 'movement:' + identity + ':' + day, 'lot_id': 'buy:' + identity,
        'code': code, 'effective_date': day, 'kind': 'BUY' if qty > 0 else 'SELL',
        'quantity_delta': qty, 'cost_delta_fen': cost, 'reference_id': identity}


def attach(value, **arrays):
    value.update(snapshot_at=CUTOFF, account_revision=5, report_revision=7)
    source = {'schema_version': 1, 'state': 'READY', 'scope': 'all_time', 'complete': True,
        'account_id': 'S1', 'snapshot_at': CUTOFF, 'account_revision': 5, 'report_revision': 7,
        'as_of_session': DAY, 'target_session': NEXT}
    for key in ('fills', 'lots', 'lot_movements', 'cash_entries', 'corrections', 'cases',
                'entitlements', 'tax_items', 'statements', 'boundary_statements'):
        source[key] = deepcopy(arrays.get(key, value.get(key, [])))
    source['row_counts'] = {key: len(rows) for key, rows in source.items() if isinstance(rows, list)}
    value['trading_history_projection'] = source
    return value


def history_rows(value, key='summary_chunks'):
    return [row for chunk in build_trading_history(value)[key] for row in chunk]


def test_historical_pnl_dates_use_boundary_without_selecting_it_or_substituting_future():
    value = report()
    value['account']['simulation_start_session'] = OLD
    boundary = statement('2026-10-30', [position('2026-10-30', value=90000)], 0)
    value['statements'][0]['daily_pnl_fen'] = 10000
    attach(value, boundary_statements=[boundary])
    daily = build(value)['daily_pnl_by_session']
    assert set(daily) == {PREVIOUS, DAY}
    assert NEXT not in daily and '2026-10-30' not in daily
    assert daily[PREVIOUS]['rows'][0]['daily_pnl_fen'] == 10000
    assert daily[DAY]['rows'][0]['daily_pnl_fen'] == 10000


def test_noncontinuous_boundary_does_not_fabricate_prior_trading_day():
    value = report()
    value['account']['simulation_start_session'] = OLD
    attach(value, boundary_statements=[statement('2026-10-29', [], 0)])
    previous = build(value)['daily_pnl_by_session'][PREVIOUS]
    assert previous['rows'][0]['daily_pnl_fen'] is None
    assert '连续' in previous['reason']


def test_boundary_revision_and_missing_final_guards_remain_per_day():
    value = report()
    value['statements'].append({**deepcopy(value['statements'][0]), 'statement_revision': 2,
                                'requires_restatement': True})
    daily = build(attach(value))['daily_pnl_by_session']
    assert all(item['rows'][0]['daily_pnl_fen'] is None for item in daily.values())


def test_full_history_never_expands_recent_executions_or_market_queries():
    value = attach(report(), fills=[trade()], lots=[lot()], lot_movements=[move()])
    market = SavedMarket()
    original = deepcopy(value)
    view = build(value, market)
    history = build_trading_history(value)
    assert view['executions'] == [] and history['row_counts']['trades'] == 1
    assert not any(call[0] == 'prices' and call[2] == OLD for call in market.calls)
    assert value == original


def test_holding_cost_average_and_old_original_buy_are_distinct():
    value = attach(report(), fills=[trade()], lots=[lot()], lot_movements=[move()])
    holding = build(value)['holdings'][0]
    assert holding['cost_price'] == '10.0500'
    batch = holding['buy_lots'][0]
    assert batch['price'] == '10.00' and batch['acquired_date'] == OLD
    assert batch['original_cost_fen'] == batch['cost_fen'] == 100500
    assert holding['buy_lots_state'] == batch['state'] == 'READY'


def test_lots_use_valuation_day_not_later_live_sale_and_keep_original_prices():
    value = attach(report(), fills=[trade(), trade('sell', side='SELL', day=NEXT)],
        lots=[lot(quantity=0, cost=0)],
        lot_movements=[move(), move(qty=-100, cost=-100500, day=NEXT)])
    value['account']['positions'][0]['total_shares'] = 0
    holding = build(value)['holdings'][0]
    assert holding['buy_lots'][0]['quantity'] == 100
    assert holding['buy_lots'][0]['cost_fen'] == 100500
    assert holding['buy_lots_state'] == 'READY'
    assert history_rows(value)[0]['status'] == 'CLOSED'


def test_partial_sale_and_cost_adjustment_use_saved_movements_not_recomputed_fifo():
    value = report()
    value['statements'][-1]['positions'] = [position(DAY, shares=60, cost=60123, value=66000)]
    value['account']['positions'][0]['total_shares'] = 60
    value = attach(value, fills=[trade()], lots=[lot(quantity=60, cost=60123)],
        lot_movements=[move(), move(qty=-40, cost=-40377, day=DAY)])
    holding = build(value)['holdings'][0]
    assert holding['cost_price'] == '10.0205'
    assert holding['buy_lots'][0]['price'] == '10.00'
    assert holding['buy_lots'][0]['cost_fen'] == 60123
    assert holding['buy_lots'][0]['original_cost_fen'] == 100500


@pytest.mark.parametrize('defect', ['missing_moves', 'missing_fill', 'quantity_mismatch', 'cost_mismatch', 'restatement'])
def test_incomplete_or_inconsistent_batch_evidence_is_explicit(defect):
    value = report()
    if defect == 'restatement':
        value['statements'][-1]['requires_restatement'] = True
    attach(value, fills=[] if defect == 'missing_fill' else [trade()], lots=[lot()],
        lot_movements=[] if defect == 'missing_moves' else [move(
            qty=90 if defect == 'quantity_mismatch' else 100,
            cost=100000 if defect == 'cost_mismatch' else 100500)])
    holding = build(value)['holdings'][0]
    assert holding['buy_lots_state'] == 'UNAVAILABLE' and holding['buy_lots_reason']
    if defect == 'missing_fill':
        assert holding['buy_lots'][0]['price'] is None


def test_equity_batch_does_not_invent_buy_price_and_zero_shares_do_not_divide():
    item = lot()
    item.update(source_order_id=None, source_case_id='equity')
    value = attach(report(), lots=[item], lot_movements=[move()])
    holding = build(value)['holdings'][0]
    assert holding['buy_lots'][0]['source_kind'] == 'EQUITY'
    assert holding['buy_lots'][0]['price'] is None
    value['statements'][-1]['positions'][0]['shares'] = 0
    assert build(value)['holdings'][0]['cost_price'] is None


def test_closed_then_rebought_is_currently_held_with_all_old_trades_retained():
    fills = [trade(), trade('sell', side='SELL', day=PREVIOUS), trade('buy2', day=DAY)]
    value = attach(report(), fills=fills, lots=[lot(quantity=0, cost=0), lot('buy2', day=DAY)])
    history = build_trading_history(value)
    summary = history['summary_chunks'][0][0]
    assert history['row_counts'] == {'trades': 3, 'summaries': 1}
    assert summary['status'] == 'HELD' and summary['current_quantity'] == 100
    assert (summary['buy_quantity'], summary['sell_quantity']) == (200, 100)
    assert summary['original_realized_pnl_fen'] == summary['realized_pnl_fen'] == -500
    assert [r['order_id'] for r in history['trade_chunks'][0]] == ['buy', 'sell', 'buy2']


@pytest.mark.parametrize('collection,status,expected', [
    ('entitlements', 'ACCRUED', 'CLOSED_PENDING'), ('entitlements', 'PAID', 'CLOSED'),
    ('entitlements', 'DELIVERED', 'CLOSED'), ('tax_items', 'PAYABLE', 'CLOSED_PENDING'),
    ('tax_items', 'PAID', 'CLOSED'), ('tax_items', 'RESOLVED', 'CLOSED'),
    ('tax_items', 'NOT_TRIGGERED', 'CLOSED'),
])
def test_zero_shares_pending_rights_and_tax_are_distinct_from_settled(collection, status, expected):
    value = attach(report(), fills=[trade(), trade('sell', side='SELL')],
        lots=[lot(quantity=0, cost=0)], **{collection: [{'code': CODE, 'status': status}]})
    assert history_rows(value)[0]['status'] == expected


def test_unimplemented_proposal_does_not_create_debt_and_missing_lot_is_unknown():
    case = {'case_id': 'proposal', 'code': CODE, 'status': 'RULE_PENDING',
            'event': {'code': CODE, 'kind': 'DISTRIBUTION', 'status': 'PROPOSED'}}
    value = attach(report(), fills=[trade()], lots=[lot(quantity=0, cost=0)], cases=[case])
    assert history_rows(value)[0]['status'] == 'CLOSED'
    value = attach(report(), fills=[trade()])
    assert history_rows(value)[0]['status'] == 'UNKNOWN'


@pytest.mark.parametrize('kind,expected', [('sell_fee', -400), ('fifo', -375), ('late_equity', -300)])
def test_recognized_persisted_corrections_adjust_summary_only_once(kind, expected):
    correction = {'status': 'APPLIED', 'kind': 'FEE_ADJUSTMENT', 'reference_id': 'fee',
                  'realized_pnl_adjustment_fen': 100}
    if kind == 'fifo':
        correction = {'kind': 'FIFO_FEE_COST', 'reference_id': 'sell', 'realized_cost_adjustment_fen': 125}
    if kind == 'late_equity':
        correction = {'reason': 'LATE_SHARE_COST_ALLOCATION', 'reference_fill_id': 'sell',
            'case_id': 'case', 'lot_id': 'buy:buy', 'realized_cost_adjustment_fen': 200,
            'cash_adjustment_fen': 0}
    value = attach(report(), fills=[trade(), trade('sell', side='SELL')], lots=[lot(quantity=0, cost=0)],
        corrections=[correction], cash_entries=[{'id': 'fee', 'reference_id': 'sell', 'kind': 'COMMISSION_FEN'}])
    summary = history_rows(value)[0]
    assert summary['realized_pnl_fen'] == expected
    assert summary['original_realized_pnl_fen'] == -500
    assert history_rows(value, 'trade_chunks')[1]['realized_pnl_fen'] == -500


def test_unattributed_correction_does_not_smear_or_silently_ignore_result():
    value = attach(report(), fills=[trade(), trade('sell', side='SELL')], lots=[lot(quantity=0, cost=0)],
                   corrections=[{'kind': 'SOMETHING_UNKNOWN', 'pnl_adjustment_fen': 1000}])
    summary = history_rows(value)[0]
    assert summary['realized_pnl_fen'] is None and summary['original_realized_pnl_fen'] == -500
    assert '更正' in summary['reason']


@pytest.mark.parametrize('defect', ['missing', 'count', 'account', 'revision', 'duplicate', 'incomplete'])
def test_bad_projection_is_not_presented_as_confirmed_zero_trades(defect):
    value = attach(report(), fills=[trade()])
    source = value['trading_history_projection']
    if defect == 'missing':
        del value['trading_history_projection']
    elif defect == 'count':
        source['row_counts']['fills'] = 3
    elif defect == 'account':
        source['account_id'] = 'OTHER'
    elif defect == 'revision':
        source['account_revision'] = -1
    elif defect == 'duplicate':
        source['fills'].append(deepcopy(source['fills'][0]))
        source['row_counts']['fills'] = 2
    else:
        source['complete'] = False
    history = build_trading_history(value)
    assert history['state'] == 'UNAVAILABLE' and history['complete'] is False and history['reason']


def test_complete_empty_history_and_chunk_boundary_preserve_all_rows_and_ids():
    empty = build_trading_history(attach(report()))
    assert empty['complete'] is True and empty['row_counts'] == {'trades': 0, 'summaries': 0}
    fills = [trade(str(i), code=f'SH{i:06d}') for i in range(1001)]
    lots = [lot(str(i), code=f'SH{i:06d}') for i in range(1001)]
    value = attach(report(), fills=fills, lots=lots)
    history = build_trading_history(value)
    assert [len(c) for c in history['trade_chunks']] == [1000, 1]
    assert [len(c) for c in history['summary_chunks']] == [1000, 1]
    assert {r['id'] for chunk in history['trade_chunks'] for r in chunk} == {str(i) for i in range(1001)}
    assert all(isinstance(cell, str) for chunk in history['trade_chunks'] for r in chunk for cell in r['cells'])


def test_missing_economic_fields_stay_null_and_raw_nested_evidence_is_never_exported():
    fill = trade('sell', side='SELL')
    del fill['realized_pnl_fen']
    fill['input_bundle'] = {'secret': 'never-copy'}
    value = attach(report(), fills=[fill], lots=[lot(quantity=0, cost=0)])
    summary, detail = history_rows(value)[0], history_rows(value, 'trade_chunks')[0]
    assert summary['realized_pnl_fen'] is None and '缺失' in summary['reason']
    assert 'input_bundle' not in detail and detail['cells'][12] == '—'


@pytest.mark.parametrize('defect', ['lot_code', 'fill_code', 'account', 'positions'])
def test_invalid_identifiers_and_null_positions_fail_display_without_page_exception(defect):
    value = attach(report(), fills=[trade()], lots=[lot()], lot_movements=[move()])
    source = value['trading_history_projection']
    if defect == 'lot_code':
        source['lots'][0]['code'] = None
    elif defect == 'fill_code':
        source['fills'][0]['code'] = ['not-an-identifier']
    elif defect == 'account':
        source['account_id'] = None
    else:
        source['statements'][-1]['positions'] = None
        value['statements'][-1]['positions'] = None
    assert build_trading_history(value)['state'] == 'UNAVAILABLE'
    view = build(value)
    assert view['holdings'][0]['buy_lots_state'] == 'UNAVAILABLE'
    if defect == 'positions':
        assert view['stock_daily_pnl']['rows'][0]['daily_pnl_fen'] is None


def test_missing_original_buy_price_marks_batch_evidence_unknown():
    fill = trade()
    del fill['price']
    value = attach(report(), fills=[fill], lots=[lot()], lot_movements=[move()])
    holding = build(value)['holdings'][0]
    assert holding['buy_lots_state'] == 'UNAVAILABLE'
    assert holding['buy_lots'][0]['price'] is None


def test_waiting_implemented_event_remains_pending_but_proposal_is_only_observation():
    for status, expected in [('IMPLEMENTED', 'CLOSED_PENDING'), ('PROPOSED', 'CLOSED')]:
        case = {'case_id': 'waiting', 'code': CODE, 'status': 'WAITING_DATA',
                'event': {'code': CODE, 'kind': 'DISTRIBUTION', 'status': status}}
        value = attach(report(), fills=[trade(), trade('sell', side='SELL')],
                       lots=[lot(quantity=0, cost=0)], cases=[case])
        assert history_rows(value)[0]['status'] == expected


def test_known_metadata_resolutions_without_economic_adjustments_leave_recorded_pnl():
    for kind in ('EVENT_IDENTITY_RESOLUTION', 'TAX_HOLDING_RULE_RESOLUTION'):
        for fields in ({}, {'pnl_adjustment_fen': 0, 'cash_adjustment_fen': None}):
            value = attach(report(), fills=[trade(), trade('sell', side='SELL')],
                lots=[lot(quantity=0, cost=0)], corrections=[{'kind': kind, **fields}])
            assert history_rows(value)[0]['realized_pnl_fen'] == -500


def test_metadata_label_never_hides_economic_or_malformed_adjustment():
    for kind in ('EVENT_IDENTITY_RESOLUTION', 'TAX_HOLDING_RULE_RESOLUTION'):
        for amount in (1, '0', False, {}):
            value = attach(report(), fills=[trade(), trade('sell', side='SELL')],
                lots=[lot(quantity=0, cost=0)],
                corrections=[{'kind': kind, 'realized_pnl_adjustment_fen': amount}])
            summary = history_rows(value)[0]
            assert summary['realized_pnl_fen'] is None
            assert summary['original_realized_pnl_fen'] == -500
