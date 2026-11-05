"""Corporate accounting acceptance using isolated databases and deterministic facts."""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from china_stock_sim.ledger.corporate import CorporateActionsMixin
from china_stock_sim.ledger.kernel import LedgerKernel
from china_stock_sim.ledger.store import initialize
from china_stock_sim.ledger.tax import allocate_fen, dividend_basis, dividend_tax_rate
from china_stock_sim.market.contracts import DataResult
from china_stock_sim.market.event_operations import operation_readiness

CODE = 'SH600000'


class Facts:
    def __init__(self):
        self.event = {'code': CODE, 'event_id': 'ev1', 'proposal_date': '2026-03-01',
            'record_date': '2026-06-01', 'ex_date': '2026-06-02',
            'payment_date': '2026-06-03', 'share_listing_date': '2026-06-04',
            'kind': 'DISTRIBUTION', 'status': 'IMPLEMENTED', 'cash_per_share': '0.1',
            'bonus_per_share': '0', 'capital_per_share': '0',
            'available_at': '2026-05-01T00:00:00+00:00', 'conflicts': [],
            'tax_facts': {name: {'state': 'UNKNOWN', 'value': None} for name in
                          ('share_face_value', 'capital_source', 'tax_basis_note')}}

    def event_settlement(self, code, day, *, operation, **kwargs):
        event = deepcopy(self.event)
        check = operation_readiness(event, '2026-08-01T00:00:00+00:00', operation=operation)
        event.update(settlement_readiness=check, operation_date=check['operation_date'],
                     operation_applicable=check['applicable'])
        return DataResult(check['state'], [event], tuple({'kind': i} for i in check['issues']))

    def calendar(self, *, start, end, **kwargs):
        return DataResult('READY', [{'date': start, 'is_trading_day': True}])

    def calendar_operation(self, operation, day, **kwargs):
        following = date.fromisoformat(day) + timedelta(days=1)
        while following.weekday() >= 5:
            following += timedelta(days=1)
        return DataResult('READY', following.isoformat())


class Harness(CorporateActionsMixin, LedgerKernel):
    def _verify_evidence(self, payload, required=False):
        assert payload['fixture_evidence'] == 'isolated controlled facts'
        return payload


@pytest.fixture
def service(settings):
    initialize(settings)
    return Harness(settings, market=Facts(), test_mode=True,
                   clock=lambda: datetime(2026, 8, 1, tzinfo=timezone.utc))


def revision(service):
    return service._read('S1', lambda tx: tx.state['account_revision'])


def rows(service, table):
    return service._read('S1', lambda tx: [r for r in tx.list(table)
        if not (table == 'tax_items' and 'POTENTIAL_TAX' in r.get('tax_id', ''))])


def payload(**changes):
    return {'code': CODE, 'effective_date': '2026-06-05',
            'knowledge_cutoff': '2026-08-01T00:00:00+00:00',
            'fixture_evidence': 'isolated controlled facts', **changes}


def call(service, method, operation_id, **values):
    return getattr(service, method)('S1', payload(**values), operation_id=operation_id,
                                     expected_revision=revision(service))


def seed(service, quantity=1000, cost=100000, lot='lot1', acquired='2026-05-01'):
    def apply(tx):
        item = {'lot_id': lot, 'code': CODE, 'quantity': quantity, 'cost_fen': cost,
                'original_quantity': quantity, 'original_cost_fen': cost,
                'acquired_date': acquired, 'sellable_date': acquired,
                'tax_start_date': acquired, 'tax_start_status': 'KNOWN'}
        tx.append('position_lots', lot, item)
        tx.append('lot_movements', lot + ':buy', {'lot_id': lot, 'code': CODE,
            'effective_date': acquired, 'quantity_delta': quantity, 'cost_delta_fen': cost,
            'kind': 'BUY', 'reference_id': lot})
        tx.cash(lot + ':cash', 'BUY', -cost, acquired)
        return {'state': 'APPLIED'}
    result = service._mutate('S1', lot, {'lot': lot}, revision(service), acquired, apply)
    assert result['state'] == 'APPLIED', result


def sell(service, quantity, day, identity='sell1', lot='lot1'):
    def apply(tx):
        item = tx.get('position_lots', lot)
        cost = item['cost_fen'] * quantity // item['quantity']
        item['quantity'] -= quantity
        item['cost_fen'] -= cost
        tx.save('position_lots', lot, item)
        tx.append('lot_movements', identity, {'lot_id': lot, 'code': CODE,
            'effective_date': day, 'quantity_delta': -quantity, 'cost_delta_fen': -cost,
            'kind': 'SELL', 'reference_id': identity})
        fill = {'order_id': identity, 'code': CODE, 'side': 'SELL', 'effective_date': day,
                'lot_allocations': [{'lot_id': lot, 'quantity': quantity, 'cost_fen': cost}]}
        tx.append('fills', identity, fill)
        service._on_sale(tx, fill)
        return {'state': 'APPLIED'}
    result = service._mutate('S1', identity, {'quantity': quantity}, revision(service), day, apply)
    assert result['state'] == 'APPLIED', result


def register(service):
    result = call(service, 'register_entitlements', 'register')
    assert result['state'] == 'APPLIED', result
    return rows(service, 'corporate_action_cases')[0]['case_id']


def test_cash_receivable_payment_sell_empty_and_tax_no_double_count(service):
    seed(service)
    case = register(service)
    call(service, 'recognize_entitlements', 'recognize', case_id=case)
    cash = service._read('S1', lambda tx: tx.balance())
    assert rows(service, 'entitlements')[0]['amount_fen'] == 10000
    assert call(service, 'settle_entitlements', 'pay', case_id=case)['state'] == 'APPLIED'
    assert service._read('S1', lambda tx: tx.balance()) == cash + 10000
    assert call(service, 'settle_entitlements', 'repeat-pay', case_id=case)['state'] == 'APPLIED'
    assert service._read('S1', lambda tx: tx.balance()) == cash + 10000
    sell(service, 1000, '2026-06-05')
    taxes = rows(service, 'tax_items')
    assert taxes[0]['assessed_fen'] == 1000
    assert taxes[0]['due_date'] == '2026-06-08'
    assert call(service, 'record_tax', 'tax-before', effective_date='2026-06-05')['state'] == 'WAITING_DATA'
    assert call(service, 'record_tax', 'tax-pay', effective_date='2026-06-08')['state'] == 'APPLIED'
    assert rows(service, 'tax_items')[0]['paid_fen'] == 1000
    assert rows(service, 'tax_items')[0]['assessed_fen'] == 1000
    assert service._read('S1', lambda tx: tx.balance()) == cash + 9000


def test_cash_does_not_wait_for_unknown_shares_listing_or_tax_face(service):
    seed(service)
    service.market.event.update(bonus_per_share='0.1', share_listing_date=None)
    case = register(service)
    assert call(service, 'recognize_entitlements', 'accrue', case_id=case)['state'] == 'APPLIED'
    outcome = call(service, 'settle_entitlements', 'cash', case_id=case)
    assert outcome['state'] == 'WAITING_DATA'
    assert next(e for e in rows(service, 'entitlements') if e['kind'] == 'CASH')['status'] == 'PAID'
    sell(service, 1000, '2026-06-05')
    assert rows(service, 'tax_items')[0]['assessed_fen'] is None
    assert rows(service, 'tax_items')[0]['status'] == 'WAITING_DATA'


def test_shares_cost_conserved_even_original_sold_before_delivery(service):
    seed(service, cost=100003)
    service.market.event.update(cash_per_share='0', capital_per_share='0.1')
    case = register(service)
    call(service, 'recognize_entitlements', 'accrue', case_id=case)
    entitlement = rows(service, 'entitlements')[0]
    lot = rows(service, 'position_lots')[0]
    assert lot['cost_fen'] + entitlement['remaining_cost_fen'] == 100003
    original_cost = lot['cost_fen']
    sell(service, 1000, '2026-06-03')
    call(service, 'settle_entitlements', 'shares', case_id=case)
    delivered = [lot for lot in rows(service, 'position_lots') if lot['quantity']]
    assert len(delivered) == 1 and delivered[0]['quantity'] == 100
    assert delivered[0]['sellable_date'] == '2026-06-04'
    assert delivered[0]['cost_fen'] + original_cost == 100003
    assert delivered[0]['tax_start_status'] == 'RULE_PENDING'
    assert rows(service, 'entitlements')[0]['remaining_cost_fen'] == 0


def test_fractional_account_shares_remain_receivable_not_round_into_positions(service):
    seed(service, quantity=101)
    service.market.event.update(cash_per_share='0', capital_per_share='0.1')
    case = register(service)
    call(service, 'recognize_entitlements', 'accrue', case_id=case)
    outcome = call(service, 'settle_entitlements', 'shares', case_id=case)
    assert outcome['state'] == 'WAITING_DATA'
    assert rows(service, 'entitlements')[0]['quantity'] == '10.1'
    assert rows(service, 'entitlements')[0]['status'] == 'WAITING_ALLOCATION'
    assert len(rows(service, 'position_lots')) == 1


def test_source_id_changed_same_proposal_does_not_duplicate_case(service):
    seed(service)
    case = register(service)
    service.market.event['event_id'] = 'ev2'
    call(service, 'register_entitlements', 'register-again')
    cases = rows(service, 'corporate_action_cases')
    assert len(cases) == 1 and cases[0]['case_id'] == case
    assert cases[0]['event_ids'] == ['ev1', 'ev2']
    service.market.event['cash_per_share'] = '0.2'
    call(service, 'register_entitlements', 'conflicting-revision')
    assert len(rows(service, 'corporate_action_cases')) == 1
    assert rows(service, 'corporate_action_cases')[0]['status'] == 'REVISION_PENDING'


def test_record_date_sale_excluded_but_later_sale_preserves_qualification(service):
    seed(service)
    sell(service, 300, '2026-06-01')
    sell(service, 200, '2026-06-02', 'sell2')
    case = register(service)
    assert rows(service, 'corporate_action_cases')[0]['eligibility'][0]['quantity'] == 700
    call(service, 'recognize_entitlements', 'recognize', case_id=case)
    assert rows(service, 'entitlements')[0]['amount_fen'] == 7000


def test_late_shares_recognition_records_sold_cost_adjustment(service):
    seed(service)
    sell(service, 1000, '2026-06-03')
    service.market.event.update(cash_per_share='0', capital_per_share='0.1')
    case = register(service)
    call(service, 'recognize_entitlements', 'late', case_id=case)
    assert rows(service, 'corrections')[0]['realized_cost_adjustment_fen'] == rows(service, 'entitlements')[0]['cost_fen']
    assert rows(service, 'position_lots')[0]['cost_fen'] == 0


@pytest.mark.parametrize('acquired,sold,rate', [
    ('2026-01-31','2026-02-28','0.20'), ('2026-01-31','2026-03-01','0.10'),
    ('2024-02-29','2025-02-28','0.10'), ('2024-02-29','2025-03-01','0'),
    ('2025-05-03','2025-06-03','0.20'), ('2025-05-03','2025-06-04','0.10'),
    ('2025-05-03','2026-05-03','0.10'), ('2025-05-03','2026-05-04','0'),
])
def test_natural_month_year_tax_boundaries(acquired,sold,rate):
    assert dividend_tax_rate(acquired,sold) == Decimal(rate)


def test_cumulative_tax_rounding_and_payment_id_no_repeat(service):
    seed(service, quantity=10)
    service.market.event['cash_per_share'] = '0.03'
    case = register(service)
    call(service, 'recognize_entitlements', 'accrue', case_id=case)
    call(service, 'settle_entitlements', 'pay', case_id=case)
    for i in range(5):
        sell(service, 1, '2026-06-05', f'sell{i}')
    tax = rows(service, 'tax_items')[0]
    assert tax['assessed_fen'] == 2
    assert sum(a['amount_fen'] for a in tax['assessments']) == 2
    call(service, 'record_tax', 'pay-tax', effective_date='2026-06-08', payment_id='same')
    before = service._read('S1', lambda tx: tx.balance())
    call(service, 'record_tax', 'pay-tax-other-op', effective_date='2026-06-08', payment_id='same')
    assert service._read('S1', lambda tx: tx.balance()) == before


def test_rounding_conserves_tiny_costs_and_unknown_not_zero():
    assert allocate_fen(1, [1,1,1]) == [0,0,1]
    event = Facts().event
    assert dividend_basis(event)[0] == Decimal('0.1')
    event['capital_per_share'] = '0.1'
    assert dividend_basis(event)[0] is None
    event['tax_facts']['capital_source'] = {'state':'READY','value':'STOCK_PREMIUM'}
    assert dividend_basis(event)[0] == Decimal('0.1')
    event['tax_facts']['capital_source']['value'] = 'OTHER'
    assert dividend_basis(event)[0] is None


def test_future_ready_action_not_posted_until_due(service):
    seed(service)
    result = call(service, 'register_entitlements', 'notdue', effective_date='2026-05-31')
    assert result['state'] == 'WAITING_DATA'
    assert not rows(service, 'entitlements')
    assert rows(service, 'corporate_action_cases')[0]['actions']['eligibility']['state'] == 'NOT_DUE'


def test_tax_partial_payment_does_not_subtract_its_own_payable_reserve(service):
    seed(service)
    case = register(service)
    call(service, 'recognize_entitlements', 'accrue', case_id=case)
    call(service, 'settle_entitlements', 'pay', case_id=case)
    sell(service, 1000, '2026-06-05')

    def reduce_cash(tx):
        tx.cash('expense', 'TEST_EXPENSE', 100 - tx.balance(), '2026-06-05')
        return {'state': 'APPLIED'}
    service._mutate('S1', 'reduce', {}, revision(service), '2026-06-05', reduce_cash)
    outcome = call(service, 'record_tax', 'tax1', effective_date='2026-06-08', payment_id='p1')
    assert outcome['state'] == 'WAITING_DATA'
    assert rows(service, 'tax_items')[0]['paid_fen'] == 100
    assert service._read('S1', lambda tx: tx.balance()) == 0

    def add_cash(tx):
        tx.cash('correct', 'TEST_ADJUSTMENT', 900, '2026-06-08')
        return {'state': 'APPLIED'}
    service._mutate('S1', 'add', {}, revision(service), '2026-06-08', add_cash)
    call(service, 'record_tax', 'tax2', effective_date='2026-06-08', payment_id='p2')
    assert rows(service, 'tax_items')[0]['paid_fen'] == 1000
    assert service._read('S1', lambda tx: tx.balance()) == 0


def test_tax_must_not_consume_reserved_buy_cash(service):
    seed(service)
    case = register(service)
    call(service, 'recognize_entitlements', 'accrue', case_id=case)
    call(service, 'settle_entitlements', 'pay', case_id=case)
    sell(service, 1000, '2026-06-05')

    def reserve(tx):
        tx.append('reservations', 'existing-buy', {'status':'ACTIVE', 'side':'BUY',
                                                  'cash_fen': tx.balance(), 'code': CODE})
        return {'state':'APPLIED'}
    service._mutate('S1', 'reserve', {}, revision(service), '2026-06-05', reserve)
    assert call(service, 'record_tax', 'tax', effective_date='2026-06-08')['state'] == 'WAITING_DATA'
    assert rows(service, 'tax_items')[0]['paid_fen'] == 0


def test_later_sale_must_not_delay_old_tax_or_accelerate_new_tax(service):
    seed(service)
    case = register(service)
    call(service, 'recognize_entitlements', 'accrue', case_id=case)
    call(service, 'settle_entitlements', 'pay', case_id=case)
    sell(service, 500, '2026-06-05', 'sell-first')
    sell(service, 500, '2026-06-08', 'sell-second')
    item = rows(service, 'tax_items')[0]
    assert item['assessed_fen'] == 1000
    assert [a['due_date'] for a in item['assessments']] == ['2026-06-08', '2026-06-09']
    call(service, 'record_tax', 'tax-day1', effective_date='2026-06-08')
    assert rows(service, 'tax_items')[0]['paid_fen'] == 500
    call(service, 'record_tax', 'tax-day2', effective_date='2026-06-09')
    assert rows(service, 'tax_items')[0]['paid_fen'] == 1000


def test_zero_cash_after_fen_rounding_has_no_zero_financial_entry(service):
    seed(service, quantity=1)
    service.market.event['cash_per_share'] = '0.00001'
    case = register(service)
    call(service, 'recognize_entitlements', 'accrue', case_id=case)
    assert rows(service, 'entitlements')[0]['amount_fen'] == 0
    assert call(service, 'settle_entitlements', 'pay', case_id=case)['state'] == 'APPLIED'
    assert not [r for r in rows(service, 'cash_entries') if r['kind'] == 'DIVIDEND']


def test_withdrawn_implemented_event_preserves_paid_cash_and_flags_revision(service):
    seed(service)
    case = register(service)
    call(service, 'recognize_entitlements', 'accrue', case_id=case)
    call(service, 'settle_entitlements', 'pay', case_id=case)
    balance = service._read('S1', lambda tx: tx.balance())
    service.market.event['status'] = 'CANCELLED'
    assert call(service, 'register_entitlements', 'withdrawn')['state'] == 'WAITING_DATA'
    assert rows(service, 'corporate_action_cases')[0]['status'] == 'REVISION_PENDING'
    assert rows(service, 'entitlements')[0]['status'] == 'PAID'
    assert service._read('S1', lambda tx: tx.balance()) == balance


@pytest.mark.parametrize('method', ['register_entitlements', 'recognize_entitlements',
                                   'settle_entitlements', 'record_tax'])
def test_future_processing_day_is_rejected(service, method):
    with pytest.raises(ValueError, match='未来'):
        call(service, method, 'future', effective_date='2026-08-02')


def test_irrelevant_valid_evidence_cannot_authorize_uncovered_event_facts(service, monkeypatch):
    original = service.market.event_settlement
    def evidenced(*args, **kwargs):
        result = original(*args, **kwargs)
        return DataResult(result.state, result.data, result.issues, ('event-observation',))
    monkeypatch.setattr(service.market, 'event_settlement', evidenced)
    with pytest.raises(ValueError, match='未包含'):
        call(service, 'register_entitlements', 'irrelevant')


def test_nontrading_payment_uses_following_session_but_keeps_source_date(service, monkeypatch):
    seed(service)
    service.market.event['payment_date'] = '2026-06-06'
    monkeypatch.setattr(service.market, 'calendar', lambda **kwargs: DataResult('READY', [
        {'date': kwargs['start'], 'is_trading_day': kwargs['start'] != '2026-06-06'}]))
    case = register(service)
    call(service, 'recognize_entitlements', 'accrue', case_id=case)
    assert call(service, 'settle_entitlements', 'weekend', case_id=case,
                effective_date='2026-06-06')['state'] == 'WAITING_DATA'
    call(service, 'settle_entitlements', 'monday', case_id=case, effective_date='2026-06-08')
    item = rows(service, 'entitlements')[0]
    assert item['payment_date'] == '2026-06-06' and item['paid_date'] == '2026-06-08'
    assert next(r for r in rows(service, 'cash_entries') if r['kind'] == 'DIVIDEND')['effective_date'] == '2026-06-08'


def test_real_public_data_evidence_covers_cash_lifecycle_without_network(settings, monkeypatch):
    import socket

    from test_market_consumer_gaps import FETCHED, cash_event, seed_event, source

    from china_stock_sim.ledger.evidence import EvidenceMixin
    from china_stock_sim.market.repository import MarketRepository
    from china_stock_sim.market.service import MarketDataService

    class RealHarness(CorporateActionsMixin, EvidenceMixin, LedgerKernel):
        pass

    def no_network(*args, **kwargs):
        raise AssertionError('Ledger must not collect data or invoke a model')

    monkeypatch.setattr(socket.socket, 'connect', no_network)
    initialize(settings)
    repository = MarketRepository(settings.root)
    event = cash_event(complete=True)
    seed_event(repository, event)
    calendar = [{'date': '2026-07-16', 'is_trading_day': 1}]
    repository.put('calendar', calendar, source(calendar))
    market = MarketDataService(settings.root)
    service = RealHarness(settings, market=market, test_mode=True,
                         clock=lambda: datetime(2026, 8, 1, tzinfo=timezone.utc))
    seed(service)
    common = {'code': CODE, 'effective_date': '2026-07-16',
              'knowledge_cutoff': FETCHED, 'event_ids': [event['event_id']]}
    for operation, method in [('eligibility', 'register_entitlements'),
                              ('cash_accrual', 'recognize_entitlements'),
                              ('cash_payment', 'settle_entitlements')]:
        kwargs = {'code': CODE, 'session': '2026-07-16', 'knowledge_cutoff': FETCHED,
                  'event_ids': [event['event_id']], 'operation': operation}
        data = market.event_settlement(**kwargs).as_dict()
        request = {**common, 'operation': operation,
                   'data_evidence': [{'method': 'event_settlement', 'kwargs': kwargs, 'result': data}]}
        if operation != 'eligibility':
            request['case_id'] = rows(service, 'corporate_action_cases')[0]['case_id']
        result = getattr(service, method)('S1', request, operation_id=operation,
                                         expected_revision=revision(service))
        assert result['state'] == 'APPLIED', result
    assert rows(service, 'entitlements')[0]['amount_fen'] == 42000
    assert rows(service, 'entitlements')[0]['paid_fen'] == 42000
    assert service._read('S1', lambda tx: tx.balance()) == 4942000


def test_late_share_cost_adjustments_reconstruct_both_old_and_current_lots(service):
    from china_stock_sim.ledger.queries import historical_lots

    seed(service, cost=100003)
    sell(service, 400, '2026-06-03', 'first')
    sell(service, 600, '2026-06-05', 'last')
    service.market.event.update(cash_per_share='0', capital_per_share='0.1')
    case = register(service)
    call(service, 'recognize_entitlements', 'late', case_id=case)
    total_transfer = rows(service, 'entitlements')[0]['cost_fen']
    assert sum(c['realized_cost_adjustment_fen'] for c in rows(service, 'corrections')) == total_transfer
    moves = rows(service, 'lot_movements')
    assert sum(m['cost_delta_fen'] for m in moves) == 0
    assert sum(m['cost_delta_fen'] for m in moves if m['effective_date'] <= '2026-06-02') == 100003 - total_transfer
    assert service._read('S1', lambda tx: historical_lots(tx, '2026-06-02'))[0]['cost_fen'] == 100003 - total_transfer


def test_registration_waits_for_record_day_unresolved_order_then_recovers(service):
    seed(service)
    def pending(tx):
        tx.append('orders', 'unknown-sell', {'code': CODE, 'state': 'WAITING_DATA',
                                           'target_session': '2026-06-01'})
        return {'state': 'APPLIED'}
    service._mutate('S1', 'pending', {}, revision(service), '2026-06-01', pending)
    assert call(service, 'register_entitlements', 'registration-wait')['state'] == 'WAITING_DATA'
    assert rows(service, 'corporate_action_cases')[0]['eligibility'] == []
    def confirmed(tx):
        order = tx.get('orders', 'unknown-sell')
        order['state'] = 'NOT_FILLED'
        tx.save('orders', 'unknown-sell', order)
        return {'state': 'APPLIED'}
    service._mutate('S1', 'confirmed', {}, revision(service), '2026-06-01', confirmed)
    assert call(service, 'register_entitlements', 'registration-new-attempt')['state'] == 'APPLIED'
    assert rows(service, 'corporate_action_cases')[0]['eligibility'][0]['quantity'] == 1000


def test_two_ambiguous_same_proposal_cases_are_held_not_created_a_third(service):
    seed(service)
    case_id = register(service)
    def add_ambiguous(tx):
        old = tx.get('corporate_action_cases', case_id)
        old.pop('id')
        old['case_id'] = 'legacy-similar'
        old['event_ids'] = ['legacy-other']
        tx.append('corporate_action_cases', 'legacy-similar', old)
        return {'state': 'APPLIED'}
    service._mutate('S1', 'legacy-fixture', {}, revision(service), '2026-06-01', add_ambiguous)
    service.market.event['event_id'] = 'new-ambiguous'
    assert call(service, 'register_entitlements', 'ambiguous')['state'] == 'WAITING_DATA'
    cases = rows(service, 'corporate_action_cases')
    assert len(cases) == 2 and all(c['status'] == 'IDENTITY_PENDING' for c in cases)


def test_whole_account_integral_shares_from_fractional_lots_conserve_total_cost(service):
    seed(service, quantity=5, cost=501, lot='a')
    seed(service, quantity=5, cost=502, lot='b')
    service.market.event.update(cash_per_share='0', capital_per_share='0.1')
    case = register(service)
    call(service, 'recognize_entitlements', 'accrue', case_id=case)
    assert rows(service, 'entitlements')[0]['quantity'] == '1.0'
    call(service, 'settle_entitlements', 'deliver', case_id=case)
    lots = rows(service, 'position_lots')
    assert sum(lot['quantity'] for lot in lots) == 11
    assert sum(lot['cost_fen'] for lot in lots) == 1003
    assert rows(service, 'entitlements')[0]['status'] == 'DELIVERED'


def test_lost_previously_known_record_date_does_not_erase_locked_eligibility(service):
    seed(service)
    case = register(service)
    service.market.event['record_date'] = None
    assert call(service, 'register_entitlements', 'lost-date')['state'] == 'WAITING_DATA'
    saved = rows(service, 'corporate_action_cases')[0]
    assert saved['case_id'] == case and saved['record_date'] == '2026-06-01'
    assert saved['status'] == 'REVISION_PENDING'
    assert saved['eligibility'][0]['quantity'] == 1000


def test_unchanged_unknown_tax_does_not_request_infinite_restatement(service):
    seed(service)
    service.market.event.update(bonus_per_share='0.1')
    case = register(service)
    call(service, 'recognize_entitlements', 'accrue', case_id=case)
    sell(service, 1000, '2026-06-05')
    def closed(tx):
        tx.state['settled_through'] = '2026-06-05'
        tx.state['restate_from'] = None
        tx.state['restate_through'] = None
        return {'state': 'APPLIED'}
    service._mutate('S1', 'closed-fixture', {}, revision(service), '2026-06-05', closed)
    call(service, 'record_tax', 'repeat-unknown', effective_date='2026-06-08')
    assert service._read('S1', lambda tx: tx.state['restate_from']) is None


def configure_holding_rule(service, basis='ORIGINAL_ACQUISITION', *, effective_from='2026-01-01',
                           url='https://www.chinatax.gov.cn/fixture'):
    # A controlled test policy, not a claim that this URL establishes the chosen basis.
    text = f'''schema_version = 1
default_state = "UNKNOWN"
[[rules]]
rule_id = "fixture-holding"
effective_from = "{effective_from}"
start_basis = "{basis}"
[[rules.authorities]]
title = "Isolated acceptance fixture"
reference = "Explicit fixture basis, not a production policy"
url = "{url}"
'''
    (service.root / 'configs/tax_holding_rules.toml').write_text(text)


def pending_share_tax(service):
    seed(service)
    service.market.event.update(cash_per_share='0', capital_per_share='0.1')
    service.market.event['tax_facts']['capital_source'] = {'state': 'READY', 'value': 'STOCK_PREMIUM'}
    first = register(service)
    call(service, 'recognize_entitlements', 'first-accrue', case_id=first)
    call(service, 'settle_entitlements', 'first-deliver', case_id=first)
    lot = next(row for row in rows(service, 'position_lots') if row.get('source_case_id'))
    service.market.event.update(event_id='second-event', proposal_date='2026-04-01',
        record_date='2026-06-05', ex_date='2026-06-08', payment_date='2026-06-09',
        cash_per_share='1', capital_per_share='0')
    call(service, 'register_entitlements', 'second-register', effective_date='2026-06-10')
    second = next(row['case_id'] for row in rows(service, 'corporate_action_cases') if row['case_id'] != first)
    call(service, 'recognize_entitlements', 'second-accrue', case_id=second, effective_date='2026-06-10')
    call(service, 'settle_entitlements', 'second-pay', case_id=second, effective_date='2026-06-10')
    sell(service, 100, '2026-06-10', 'new-shares-sale', lot=lot['lot_id'])
    assert next(row for row in rows(service, 'tax_items') if row.get('status') == 'WAITING_DATA')['assessed_fen'] is None
    return lot['lot_id'], second


def test_unknown_holding_rule_requires_config_and_rejects_caller_start(service):
    lot, _ = pending_share_tax(service)
    outcome = call(service, 'resolve_tax_holding_rule', 'no-rule', rule_id='missing', lot_ids=[lot])
    assert outcome['state'] == 'WAITING_DATA'
    assert next(row for row in rows(service, 'position_lots') if row['lot_id'] == lot)['tax_start_date'] is None
    outcome = call(service, 'resolve_tax_holding_rule', 'no-date-shortcut', rule_id='missing',
                   lot_ids=[lot], tax_start_date='2000-01-01')
    assert outcome['state'] == 'INVALID_INPUT'


@pytest.mark.parametrize(('basis', 'start', 'assessed'), [
    ('ORIGINAL_ACQUISITION', '2026-05-01', 1000), ('SHARE_DELIVERY', '2026-06-04', 2000)])
def test_rule_recovery_recalculates_unknown_tax_preserves_document_and_marks_history(service, basis, start, assessed):
    import json

    lot, case_id = pending_share_tax(service)
    configure_holding_rule(service, basis)
    def closed(tx):
        tx.state.update(settled_through='2026-06-10', restate_from=None, restate_through=None)
        return {'state': 'APPLIED'}
    service._mutate('S1', 'closed-before-recovery', {}, revision(service), '2026-06-10', closed)
    before_cash = service._read('S1', lambda tx: tx.balance())
    outcome = call(service, 'resolve_tax_holding_rule', 'resolve-start', rule_id='fixture-holding',
                   lot_ids=[lot], effective_date='2026-06-11')
    assert outcome['state'] == 'APPLIED', outcome
    updated = next(row for row in rows(service, 'position_lots') if row['lot_id'] == lot)
    assert updated['quantity'] == 0 and updated['tax_start_date'] == start
    document = updated['tax_holding_rule_document']
    rule_file = service.root / document['path']
    assert json.loads(rule_file.read_text())['start_basis'] == basis
    assert updated['tax_holding_rule_hash'] == document['content_hash']
    eligibility = next(row for row in rows(service, 'corporate_action_cases') if row['case_id'] == case_id)['eligibility']
    assert next(row for row in eligibility if row['lot_id'] == lot)['tax_start_date'] == start
    tax = next(row for row in rows(service, 'tax_items') if row.get('rate') is not None)
    assert tax['assessed_fen'] == assessed and tax['due_date'] == '2026-06-11'
    assert not any(row['status'] == 'WAITING_DATA' for row in rows(service, 'tax_items'))
    assert service._read('S1', lambda tx: tx.balance()) == before_cash
    assert service._read('S1', lambda tx: tx.state['restate_from']) == '2026-06-04'
    assert service._read('S1', lambda tx: tx.state['restate_through']) == '2026-06-10'
    assert call(service, 'record_tax', 'pay-resolved', effective_date='2026-06-11')['state'] == 'APPLIED'
    assert service._read('S1', lambda tx: tx.balance()) == before_cash - assessed
    assert service._read('LLM', lambda tx: tx.balance()) == 5000000


def test_confirmed_holding_rule_change_conflicts_and_same_operation_is_idempotent(service):
    lot, _ = pending_share_tax(service)
    configure_holding_rule(service)
    values = payload(rule_id='fixture-holding', lot_ids=[lot], effective_date='2026-06-11')
    prior_revision = revision(service)
    applied = service.resolve_tax_holding_rule('S1', values, operation_id='resolve', expected_revision=prior_revision)
    assert applied['state'] == 'APPLIED'
    saved_lots, saved_tax = rows(service, 'position_lots'), rows(service, 'tax_items')
    configure_holding_rule(service, 'SHARE_DELIVERY')
    repeated = service.resolve_tax_holding_rule('S1', values, operation_id='resolve', expected_revision=prior_revision)
    assert repeated['state'] == 'ALREADY_APPLIED' and repeated['commit_id'] == applied['commit_id']
    changed = call(service, 'resolve_tax_holding_rule', 'change-confirmed-rule', rule_id='fixture-holding', lot_ids=[lot])
    assert changed['state'] == 'CONFLICT'
    assert rows(service, 'position_lots') == saved_lots and rows(service, 'tax_items') == saved_tax
    assert len([row for row in rows(service, 'corrections') if row['kind'] == 'TAX_HOLDING_RULE_RESOLUTION']) == 1


@pytest.mark.parametrize(('config', 'state'), [
    ({'url': 'https://example.com/unverified'}, 'INVALID_INPUT'),
    ({'effective_from': '2026-07-01'}, 'CONFLICT')])
def test_holding_rule_needs_official_reference_and_applicable_dates(service, config, state):
    lot, _ = pending_share_tax(service)
    configure_holding_rule(service, **config)
    result = call(service, 'resolve_tax_holding_rule', 'bad-config', rule_id='fixture-holding', lot_ids=[lot])
    assert result['state'] == state
    assert next(row for row in rows(service, 'position_lots') if row['lot_id'] == lot)['tax_start_status'] == 'RULE_PENDING'


def test_unique_restored_source_clears_pending_identity_without_posting_twice(service):
    seed(service)
    case = register(service)
    call(service, 'recognize_entitlements', 'accrue', case_id=case)
    call(service, 'settle_entitlements', 'pay', case_id=case)
    cash, entitlements = service._read('S1', lambda tx: tx.balance()), rows(service, 'entitlements')
    service.market.event['record_date'] = None
    assert call(service, 'register_entitlements', 'uncertain')['state'] == 'WAITING_DATA'
    service.market.event['record_date'] = '2026-06-01'
    assert call(service, 'register_entitlements', 'restore-source')['state'] == 'APPLIED'
    assert rows(service, 'corporate_action_cases')[0]['status'] == 'REGISTERED'
    call(service, 'recognize_entitlements', 'repeat-accrue', case_id=case)
    call(service, 'settle_entitlements', 'repeat-pay', case_id=case)
    assert service._read('S1', lambda tx: tx.balance()) == cash
    assert rows(service, 'entitlements') == entitlements


def add_identity_alias(service, case_id, *, economics=False):
    def add(tx):
        row = deepcopy(tx.get('corporate_action_cases', case_id))
        row.pop('id')
        row.update(case_id='suspected-alias', event_ids=['old-alias'], status='IDENTITY_PENDING')
        tx.append('corporate_action_cases', 'suspected-alias', row)
        if economics:
            tx.append('entitlements', 'alias-cash', {'case_id': 'suspected-alias', 'kind': 'CASH',
                'code': CODE, 'amount_fen': 10000, 'paid_fen': 0, 'status': 'RECEIVABLE'})
        return {'state': 'APPLIED'}
    service._mutate('S1', 'add-alias', {}, revision(service), '2026-06-01', add)


def test_identity_resolution_merges_only_empty_alias_preserves_case_and_retries(service):
    seed(service)
    case = register(service)
    call(service, 'recognize_entitlements', 'accrue', case_id=case)
    add_identity_alias(service, case)
    service.market.event['event_id'] = 'new-canonical'
    assert call(service, 'register_entitlements', 'ambiguous')['state'] == 'WAITING_DATA'
    values = payload(case_id=case, merge_case_ids=['suspected-alias'], event_ids=['new-canonical'])
    rev = revision(service)
    applied = service.resolve_event_identity('S1', values, operation_id='resolve-alias', expected_revision=rev)
    assert applied['state'] == 'APPLIED', applied
    assert applied['case_id'] == case
    cases = rows(service, 'corporate_action_cases')
    assert next(row for row in cases if row['case_id'] == 'suspected-alias')['status'] == 'MERGED'
    assert next(row for row in cases if row['case_id'] == case)['event_ids'] == ['ev1', 'new-canonical', 'old-alias']
    assert service.resolve_event_identity('S1', values, operation_id='resolve-alias', expected_revision=rev)['state'] == 'ALREADY_APPLIED'
    assert call(service, 'register_entitlements', 'after-resolution')['state'] == 'APPLIED'
    assert len(rows(service, 'corporate_action_cases')) == 2 and len(rows(service, 'entitlements')) == 1
    call(service, 'settle_entitlements', 'final-pay', case_id=case)
    assert service._read('S1', lambda tx: tx.balance()) == 4910000


def test_identity_resolution_refuses_two_economic_cases(service):
    seed(service)
    case = register(service)
    call(service, 'recognize_entitlements', 'accrue', case_id=case)
    add_identity_alias(service, case, economics=True)
    before = rows(service, 'corporate_action_cases')
    result = call(service, 'resolve_event_identity', 'two-economic', case_id=case,
                  merge_case_ids=['suspected-alias'], event_ids=['ev1'])
    assert result['state'] == 'CONFLICT'
    assert rows(service, 'corporate_action_cases') == before and len(rows(service, 'entitlements')) == 2


def test_identity_resolution_refuses_unrelated_source_or_omitted_ambiguous_case(service):
    seed(service)
    case = register(service)
    add_identity_alias(service, case)
    assert call(service, 'resolve_event_identity', 'omitted', case_id=case, event_ids=['ev1'])['state'] == 'CONFLICT'
    service.market.event.update(event_id='unrelated', proposal_date='2026-05-02')
    result = call(service, 'resolve_event_identity', 'unrelated', case_id=case,
                  merge_case_ids=['suspected-alias'], event_ids=['unrelated'])
    assert result['state'] == 'CONFLICT'
