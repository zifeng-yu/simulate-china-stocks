"""Offline three-account statement review comparison; only temporary fixture data.

Measures the workflow shape of three input refreshes plus nine due scans. Source,
clock, visible market facts and statement evidence are fixed between versions.
No provider/model, production data, task install or actual workflow run is used.
"""

import argparse
import hashlib
import json
import resource
import sqlite3
import sys
import tempfile
from collections import Counter
from contextlib import nullcontext
from datetime import date, datetime, timedelta
from pathlib import Path
from time import perf_counter

REPOSITORY = Path(__file__).resolve().parents[1]


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def run(args):
    if args.source:
        sys.path.insert(0, str(Path(args.source).resolve()))
    sys.path.insert(1, str(REPOSITORY / 'tests'))
    import conftest
    import pytest

    from china_stock_sim.application.workflow import WorkflowApplication
    from china_stock_sim.config import load_settings
    from china_stock_sim.ledger import LedgerService, statement_inputs
    from china_stock_sim.ledger.decision_evidence import verification_scope
    from china_stock_sim.ledger.kernel import canonical
    from china_stock_sim.market.repository import MarketRepository
    from china_stock_sim.market.service import MarketDataService

    patch = pytest.MonkeyPatch()
    import socket
    patch.setattr(socket.socket, 'connect', lambda *a: (_ for _ in ()).throw(
        AssertionError('benchmark cannot access network')))
    end = date(2026, 11, 3)
    calendar = [{'date': (end - timedelta(days=i)).isoformat(),
                 'is_trading_day': int((end - timedelta(days=i)).weekday() < 5)}
                for i in reversed(range(180))]
    history = [r['date'] for r in calendar if r['is_trading_day']][-121:]
    days = history[-args.sessions:]
    now = [datetime.fromisoformat('2026-11-03T21:00:00+08:00')]
    cutoff = '2026-11-03T19:00:00+08:00'
    codes = [f'SH{600000 + i:06d}' for i in range(args.codes)]
    accounts = ('S1', 'S1-LT', 'LLM')
    records = []

    def source(rows):
        return {'provider': 'isolated', 'adapter_version': 'fixed-statement-review',
                'fetched_at': '2026-11-03T18:10:00+08:00', 'rows': rows}

    with tempfile.TemporaryDirectory(prefix='statement-review-perf-') as folder:
        root = conftest.project.__wrapped__(Path(folder))
        settings = load_settings(root)
        repo = MarketRepository(root)
        repo.put('calendar', calendar, source(calendar))
        for code in codes:
            bars = [{'code': code, 'date': day, 'open': '10.00', 'high': '10.00',
                'low': '10.00', 'close': '10.00', 'preclose': '10.00', 'tradestatus': 1,
                'isST': 0, 'adjustflag': 3, 'volume': 100000, 'amount': '1000000.00'}
                for day in history]
            repo.put('bar', bars, source(bars))
            coverage = [{'code': code, 'date': days[-1], 'purpose': 'events',
                'start': history[0], 'end': days[-1], 'complete': True}]
            repo.put('coverage', coverage, source(coverage))
        market = MarketDataService(root)
        book = LedgerService(settings, market=market, clock=lambda: now[0], test_mode=True)
        book.initialize()
        statements = []
        for day in days:
            queries = [('calendar', {'end': day})]
            for code in codes:
                queries.extend([('prices', {'code': code, 'start': day, 'end': day}),
                                ('events', {'code': code}),
                                ('event_coverage', {'code': code, 'start': day, 'end': day})])
            evidence = []
            for method, kwargs in queries:
                kwargs = {**kwargs, 'knowledge_cutoff': cutoff}
                result = getattr(market, method)(**kwargs).as_dict()
                if result['state'] != 'READY':
                    raise AssertionError(('fixture not final', method, result))
                evidence.append({'method': method, 'kwargs': kwargs, 'result': result})
            statements.append({'session': day, 'statement_revision': 1, 'knowledge_cutoff': cutoff,
                'accounting_status': 'COMPLETE', 'valuation_status': 'FINAL', 'account_revision': 0,
                'net_assets_fen': 5_000_000, 'data_evidence': evidence})
        with sqlite3.connect(settings.database) as db:
            for account in accounts:
                state = json.loads(db.execute('SELECT data FROM account_state WHERE account_id=?',
                                              (account,)).fetchone()[0])
                state['simulation_start_session'] = days[0]
                db.execute('UPDATE account_state SET data=? WHERE account_id=?', (canonical(state), account))
                db.executemany('INSERT INTO daily_nav VALUES (?,?,?)',
                    [(f'nav:{row["session"]}', account, canonical(row)) for row in statements])
        db_before = settings.database.read_bytes()
        evidence_sha = sha(statements)
        app = WorkflowApplication(settings, ledger=book, market=market,
                                  clock=lambda: now[0], test_mode=True)
        schedule = {'completed_session': days[-1], 'trading_sessions': days}
        counters = Counter()
        original = statement_inputs._projection
        def project(*a, **kw):
            counters['semantic_projection_calls'] += 1
            return original(*a, **kw)
        patch.setattr(statement_inputs, '_projection', project)
        for method in ('statement_input_fingerprint', 'calendar', 'prices', 'events', 'event_coverage'):
            actual = getattr(market, method)
            def counted(*a, _name=method, _actual=actual, **kw):
                counters[_name + '_calls'] += 1
                return _actual(*a, **kw)
            patch.setattr(market, method, counted)
        original_loads = json.loads
        def loads(value, *a, **kw):
            counters['json_decode_calls'] += 1
            counters['json_decode_characters'] += len(value)
            return original_loads(value, *a, **kw)
        patch.setattr(json, 'loads', loads)
        for name in ('cold_run', 'persisted_receipt_run'):
            counters.clear()
            outputs = []
            scope = getattr(book, 'statement_input_review_scope', nullcontext)
            cpu = resource.getrusage(resource.RUSAGE_SELF)
            started = perf_counter()
            with verification_scope(root, persist=True) as verification, scope() as review_metrics:
                for account in accounts:
                    if args.advancing_clock:
                        now[0] += timedelta(seconds=1)
                    outputs.append(book.refresh_statement_inputs(account,
                        {'session': days[-1], 'knowledge_cutoff': now[0].isoformat()},
                        operation_id='review:' + account + ':' + name, expected_revision=0))
                    outputs.append(app._due(account, schedule))
                for _ in range(2):
                    for account in accounts:
                        if args.advancing_clock:
                            now[0] += timedelta(seconds=1)
                        outputs.append(app._due(account, schedule))
            elapsed = perf_counter() - started
            after = resource.getrusage(resource.RUSAGE_SELF)
            measured = dict(counters)
            if settings.database.read_bytes() != db_before:
                raise AssertionError('review changed fixture financial database')
            if any(item != [] if isinstance(item, list) else
                   item.get('state') != 'ALREADY_APPLIED' or item.get('changed') for item in outputs):
                raise AssertionError(('unexpected review output', outputs))
            records.append({'kind': name, 'elapsed_seconds': elapsed,
                'process_cpu_seconds': after.ru_utime + after.ru_stime - cpu.ru_utime - cpu.ru_stime,
                'counts': measured, 'review_metrics': review_metrics,
                'verification_metrics': dict(verification), 'outputs_sha256': sha(outputs),
                'database_unchanged': True})
        patch.undo()
        record = {'scope': 'isolated_three_account_3_refresh_9_due_no_network',
            'source': str(Path(args.source).resolve()) if args.source else 'current',
            'codes': len(codes), 'history_sessions': len(history), 'review_sessions': len(days),
            'advancing_clock': args.advancing_clock, 'statement_evidence_sha256': evidence_sha,
            'records': records, 'provider_calls': 0, 'model_calls': 0,
            'production_read_or_write': False}
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
    return record


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source')
    parser.add_argument('--codes', type=int, default=10)
    parser.add_argument('--sessions', type=int, default=5)
    parser.add_argument('--advancing-clock', action='store_true')
    parser.add_argument('--output', required=True)
    arguments = parser.parse_args()
    if not 1 <= arguments.codes <= 100 or not 1 <= arguments.sessions <= 60:
        parser.error('codes must be 1..100; sessions must be 1..60')
    print(json.dumps(run(arguments), ensure_ascii=False))
