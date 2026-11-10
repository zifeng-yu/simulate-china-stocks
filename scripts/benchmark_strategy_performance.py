"""Offline, fixed-input strategy performance comparison using acceptance fixtures.

Only newly created temporary projects are mutated. No production business run,
provider, model endpoint, or scheduled task is invoked. Optional reference source
is an ephemeral test baseline; the installed runtime is never changed.
"""
import argparse
import hashlib
import inspect
import json
import resource
import sys
import tempfile
from collections import Counter
from pathlib import Path
from time import perf_counter

REPOSITORY = Path(__file__).resolve().parents[1]


def run(args):
    if args.source:
        sys.path.insert(0, str(Path(args.source).resolve()))
    sys.path.insert(1, str(REPOSITORY / 'tests'))
    import conftest
    import execution_fixtures as fixtures
    import pytest

    from china_stock_sim.config import load_settings
    from china_stock_sim.ledger.decision_evidence import verification_scope

    patch = pytest.MonkeyPatch()
    codes = ('SH600000', 'SZ000001') + tuple(f'SH{600001 + i:06d}' for i in range(args.codes - 2))
    patch.setattr(fixtures, 'CODES', codes)
    from test_strategy_acceptance import strategy_world

    records = []
    with tempfile.TemporaryDirectory(prefix='strategy-perf-fixture-') as folder:
        root = conftest.project.__wrapped__(Path(folder))
        book = fixtures.execution_book.__wrapped__(load_settings(root), patch)
        book, make_app, model = strategy_world.__wrapped__(book)
        calls = Counter()
        for name in ('calendar_operation', 'prices', 'events', 'event_coverage', 'signal_prices'):
            original = getattr(book.market, name)
            def wrapped(*a, _name=name, _original=original, **kw):
                calls[_name] += 1
                return _original(*a, **kw)
            patch.setattr(book.market, name, wrapped)
        economic = {}
        def measure(account, kind, function):
            before = calls.copy()
            cpu = resource.getrusage(resource.RUSAGE_SELF)
            started = perf_counter()
            with verification_scope(root, persist=True) as metrics:
                result = function()
            elapsed = perf_counter() - started
            after = resource.getrusage(resource.RUSAGE_SELF)
            if result.get('state') != 'FROZEN':
                raise AssertionError((account, kind, result.get('state'), result.get('reasons')))
            record = {'account': account, 'kind': kind, 'elapsed_seconds': elapsed,
                'cpu_seconds': after.ru_utime + after.ru_stime - cpu.ru_utime - cpu.ru_stime,
                'verification': dict(metrics), 'market_calls': dict(calls - before),
                'result_serialized_bytes': len(json.dumps(result, ensure_ascii=False, default=str).encode())}
            records.append(record)
            print(json.dumps(record, ensure_ascii=False), flush=True)
            return result
        for account in ('S1', 'S1-LT', 'LLM'):
            app = make_app()
            options = {'summary': True} if 'summary' in inspect.signature(app.recover).parameters else {}
            measure(account, 'new_plan', lambda a=account, ap=app, o=options:
                    ap.recover(a, fixtures.FIRST, **o))
            plan = book.get_plan(account, target_session=fixtures.FIRST)
            economic[account] = {'target_session': plan['target_session'],
                'no_order_reason': plan.get('no_order_reason'),
                'orders': [{k: row.get(k) for k in ('code', 'side', 'quantity', 'limit_price',
                    'budget_fen', 'order_index', 'target_session')} for row in plan['orders']]}
            measure(account, 'frozen_recovery_first', lambda a=account, ap=app, o=options:
                    ap.recover(a, fixtures.FIRST, **o))
            measure(account, 'frozen_recovery_warm', lambda a=account, ap=app, o=options:
                    ap.recover(a, fixtures.FIRST, **o))
        digest = hashlib.sha256(json.dumps(economic, ensure_ascii=False,
            sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        result = {'scope': 'isolated_fixed_input_no_network', 'codes': len(codes),
            'history_sessions': 121, 'source': str(Path(args.source).resolve()) if args.source else 'current',
            'controlled_model_calls': len(model.calls), 'records': records,
            'economic_projection': economic, 'economic_sha256': digest,
            'financial_production_changes': False}
    patch.undo()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', help='Temporary reference src directory for a fixed comparison')
    parser.add_argument('--codes', type=int, default=100)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    if not 2 <= args.codes <= 100:
        parser.error('--codes must be between 2 and 100')
    run(args)
