"""Isolated public-cache inputs for the real execution/ledger acceptance chain."""

from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from test_ledger_trading import call

from china_stock_sim.domain import ExecutionIntent, StrategyProposal
from china_stock_sim.ledger import LedgerService
from china_stock_sim.market.events import normalize_baostock_events
from china_stock_sim.market.repository import MarketRepository
from china_stock_sim.market.service import MarketDataService

FIRST = '2026-10-30'
PREVIOUS = '2026-10-29'
CODES = ('SH600000', 'SZ000001')


def source(rows, fetched, provider='execution_acceptance_fixture'):
    return {'provider': provider, 'adapter_version': 'isolated-current',
            'fetched_at': fetched, 'rows': rows}


def set_quote(book, code, day, price='10.00', *, preclose=None, fetched=None,
              volume=100000, status=1, is_st=0, **changes):
    price = str(price)
    row = {'code': code, 'date': day, 'open': price, 'high': price,
           'low': price, 'close': price, 'preclose': str(preclose or price),
           'tradestatus': status, 'isST': is_st, 'adjustflag': 3,
           'volume': volume, 'amount': str(Decimal(price) * volume), **changes}
    repo = MarketRepository(book.root)
    return repo.put('bar', [row], source([row], fetched or day + 'T18:10:00+08:00'))


def set_coverage(book, code, day, *, fetched=None):
    rows = [{'code': code, 'date': day, 'purpose': 'events', 'start': '2026-02-01',
             'end': day, 'complete': True,
             'scope': 'reported_cash_bonus_capital_distributions'}]
    return MarketRepository(book.root).put('coverage', rows,
        source(rows, fetched or day + 'T18:10:00+08:00'))


def set_pool(book, day, effective_from, codes, *, at='18:40:00'):
    frozen_at = day + 'T' + at + '+08:00'
    row = {'code': '', 'date': day, 'effective_from': effective_from,
           'selected': [{'code': code, 'name': '隔离验收证券'} for code in codes],
           'excluded': [], 'frozen_at': frozen_at}
    return MarketRepository(book.root).put('pool', [row], source([row], frozen_at))


def set_event_settlement_dates(book, event, *, payment_date, share_listing_date):
    """Publish a normalized isolated issuer revision, retaining bound tax proof."""
    rawrow = {**event['source_evidence'][0]['source_row'],
              'dividPayDate': payment_date, 'dividStockMarketDate': share_listing_date}
    fetched = PREVIOUS + 'T18:20:00+08:00'
    revised = normalize_baostock_events([rawrow], event['code'], fetched)[0]
    revised['tax_facts'] = deepcopy(event['tax_facts'])
    dependencies = sorted({evidence['raw_reference'] for fact in revised['tax_facts'].values()
                           for evidence in fact.get('sources', [])})
    MarketRepository(book.root).put('event', [revised], {
        **source([revised], fetched, 'baostock'), 'dependencies': dependencies})
    return revised


def advance(book, day, at='20:00:00'):
    book.test_now[0] = datetime.fromisoformat(day + 'T' + at + '+08:00')


def proposal(book, *, target=FIRST, account='LLM', intents=None, no_order_reason=None):
    state = book.account_state(account, target)
    previous = book.market.calendar_operation('previous_session', target,
                                              knowledge_cutoff=book._now().isoformat())
    assert previous.ready, previous
    if intents is None:
        intents = [ExecutionIntent(CODES[0], 'BUY', 'isolated acceptance', 0,
                                   '10.00', quantity=1000)]
    intents = tuple(ExecutionIntent(**i) if isinstance(i, dict) else i for i in intents)
    return StrategyProposal(proposal_id=f'proposal:{account}:{target}', account_id=account,
        strategy_id=state['strategy_id'], strategy_version='isolated-current',
        target_session=target, market_as_of=previous.data,
        generated_at=book._now().isoformat(), input_cutoff=book._now().isoformat(),
        account_snapshot_id=state['state_id'], account_revision=state['account_revision'],
        ordered_intents=intents, no_order_reason=no_order_reason)


@pytest.fixture
def execution_book(settings, monkeypatch):
    import socket

    monkeypatch.setattr(socket.socket, 'connect',
                        lambda *a, **k: pytest.fail('execution acceptance must stay offline'))
    repo = MarketRepository(settings.root)
    first = date(2026, 2, 1)
    calendar = [{'date': (first + timedelta(days=i)).isoformat(),
                 'is_trading_day': int((first + timedelta(days=i)).weekday() < 5)}
                for i in range(305)]
    repo.put('calendar', calendar, source(calendar, '2026-02-01T18:10:00+08:00'))
    for code in CODES:
        rows = [{'code': code, 'date': row['date'], 'open': '10.00', 'high': '10.00',
                 'low': '10.00', 'close': '10.00', 'preclose': '10.00',
                 'tradestatus': 1, 'isST': 0, 'adjustflag': 3,
                 'volume': 100000, 'amount': '1000000.00'}
                for row in calendar if row['is_trading_day'] and row['date'] <= PREVIOUS]
        repo.put('bar', rows, source(rows, PREVIOUS + 'T18:10:00+08:00'))
        security = [{'code': code, 'date': '2026-02-01', 'listed': '2000-01-01',
                     'name': '隔离验收证券', 'board': '沪市主板' if code.startswith('SH') else '深市主板',
                     'source_status': 1, 'eligibility_known': True}]
        repo.put('security', security, source(security, '2026-02-01T18:10:00+08:00'))
    pool = {'code': '', 'date': PREVIOUS, 'effective_from': FIRST,
            'selected': [{'code': code, 'name': '隔离验收证券'} for code in CODES],
            'excluded': [], 'frozen_at': PREVIOUS + 'T18:30:00+08:00'}
    repo.put('pool', [pool], source([pool], pool['frozen_at']))
    clock = [datetime.fromisoformat(PREVIOUS + 'T20:00:00+08:00')]
    monkeypatch.setattr('china_stock_sim.market.service.timestamp', lambda: clock[0].isoformat())
    service = LedgerService(settings, market=MarketDataService(settings.root),
                            clock=lambda: clock[0], test_mode=True)
    service.initialize()
    service.test_now = clock
    service.test_monkeypatch = monkeypatch
    for code in CODES:
        set_coverage(service, code, PREVIOUS)
        for day in (FIRST, '2026-11-02', '2026-11-03', '2026-11-04'):
            set_quote(service, code, day)
            set_coverage(service, code, day)
    for account in ('LLM', 'S1'):
        assert call(service, 'activate_simulation', {'target_session': FIRST},
                    account=account)['state'] == 'APPLIED'
    return service
