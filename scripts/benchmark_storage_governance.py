"""SG14 isolated capacity measurement, not a financial/backtest acceptance.

Default mode builds synthetic compact SQLite facts under TemporaryDirectory,
with one cold read and one unchanged warm read at each size. --append-day adds
one real isolated write/render/backup workload only at five years. Neither mode
uses collection, models, workflow.run, network, or a production database.
"""

import argparse
import hashlib
import json
import resource
import socket
import sqlite3
import sys
import tempfile
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

from china_stock_sim.application.execution import ExecutionApplication
from china_stock_sim.application.workflow import WorkflowApplication
from china_stock_sim.config import load_settings
from china_stock_sim.domain import ExecutionIntent, StrategyProposal
from china_stock_sim.ledger import LedgerService
from china_stock_sim.ledger.decision_evidence import verification_identity, verification_scope
from china_stock_sim.ledger.evidence import fee_breakdown
from china_stock_sim.ledger.kernel import canonical
from china_stock_sim.ledger.queries import latest_statements
from china_stock_sim.ledger.store import initialize, read_snapshot
from china_stock_sim.market.repository import MarketRepository
from china_stock_sim.market.service import MarketDataService
from china_stock_sim.reporting.ledger import write_ledger_report

REPOSITORY = Path(__file__).resolve().parents[1]
NOW = '2026-11-03T12:00:00+08:00'
KNOWN = '2026-11-02T20:00:00+08:00'
FETCHED = '2026-11-02T18:11:00+08:00'
END = '2026-11-02'
ACCOUNTS = {'S1': 10, 'S1-LT': 4, 'LLM': 2}


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()


def source(rows, fetched=FETCHED):
    return {'provider': 'capacity_fixture', 'adapter_version': 'isolated-1',
            'fetched_at': fetched, 'rows': rows,
            'fields': sorted({key for row in rows for key in row})}


def price(code, day, close='10.00'):
    return {'code': code, 'date': day, 'open': '10.00', 'high': '11.00',
            'low': '9.00', 'close': close, 'preclose': '10.00', 'volume': 10000,
            'amount': '100000.00', 'tradestatus': 1, 'isST': 0, 'adjustflag': 3}


def fixture(root, years):
    """Direct fixture inserts are deliberately not claimed to be real trades."""
    (root / 'configs').mkdir(parents=True)
    for name in ('default.toml', 'execution_rules.toml'):
        text = (REPOSITORY / 'configs' / name).read_text()
        (root / 'configs' / name).write_text(text.replace('enabled = true', 'enabled = false'))
    settings = load_settings(root)
    initialize(settings, recorded_at=NOW)
    cursor, sessions = date.fromisoformat(END), []
    while len(sessions) < 252 * years:
        if cursor.weekday() < 5:
            sessions.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    sessions.reverse()
    codes = [f'SH{600000 + i:06d}' for i in range(100 + 20 * (years - 1))]
    repository = MarketRepository(root)
    with repository.connection(write=True) as db:
        cursor, calendar = date.fromisoformat(sessions[0]), []
        while cursor <= date(2026, 11, 4):
            calendar.append({'date': cursor.isoformat(), 'is_trading_day': str(int(cursor.weekday() < 5))})
            cursor += timedelta(days=1)
        for offset in range(0, len(calendar), 366):
            rows = calendar[offset:offset + 366]
            repository.put('calendar', rows, source(rows), connection=db)
        # Monthly turnover is represented by cumulative code growth each year;
        # old code history remains, while only the latest 121 sessions are read.
        for year in range(years):
            days = sessions[year * 252:(year + 1) * 252]
            for code in codes[:100 + 20 * year]:
                rows = [price(code, day) for day in days]
                previous = [price(code, day, '9.90') for day in days[::20]]
                repository.put('bar', previous, source(previous,
                    '2026-11-02T18:10:00+08:00'), connection=db)
                repository.put('bar', rows, source(rows), connection=db)
    counts = Counter()
    correction_day = sessions[-31]
    with sqlite3.connect(settings.database) as db:
        def insert(table, account, identity, body):
            body = {**body, 'id': identity, 'account_id': account}
            db.execute(f'INSERT INTO {table} VALUES (?,?,?)', (identity, account, canonical(body)))
            counts[table] += 1

        for account, order_count in ACCOUNTS.items():
            state = json.loads(db.execute('SELECT data FROM account_state WHERE account_id=?',
                                          (account,)).fetchone()[0])
            state.update(simulation_start_session=sessions[0], settled_through=END,
                         valued_through=END, account_revision=len(sessions), report_revision=len(sessions))
            db.execute('UPDATE account_state SET data=? WHERE account_id=?', (canonical(state), account))
            for index, day in enumerate(sessions):
                evidence = [{'method': 'prices', 'kwargs': {'code': codes[0], 'start': day,
                    'end': day, 'knowledge_cutoff': KNOWN}, 'result': {'state': 'READY',
                    'data': [price(codes[0], day, '9.90' if day == correction_day else '10.00')],
                    'issues': [], 'observation_ids': []}}]
                body = {'session': day, 'statement_revision': 1, 'knowledge_cutoff': KNOWN,
                    'account_revision': index, 'accounting_status': 'COMPLETE',
                    'valuation_status': 'FINAL', 'net_assets_fen': 5_000_000,
                    'daily_pnl_fen': 0, 'data_evidence': evidence, 'orders': [], 'fills': []}
                insert('daily_nav', account, f'nav:{day}:1', body)
                if index % 20 == 0:
                    insert('daily_nav', account, f'nav:{day}:2', {**body, 'statement_revision': 2})
                for number in range(order_count):
                    identity = f'{day}:{number}'
                    values = {'order_id': identity, 'plan_id': f'plan:{day}',
                        'target_session': day, 'code': codes[number], 'side': 'BUY',
                        'status': 'FILLED', 'quantity': 100, 'price': '10.00',
                        'amount_fen': 100000, 'fees_fen': 500, 'realized_pnl_fen': 0}
                    insert('orders', account, identity, values)
                    insert('fills', account, identity, {**values, 'fill_id': identity})
            for number in range(20 * years):
                case = f'future:{number}'
                insert('corporate_action_cases', account, case, {'case_id': case,
                    'code': codes[number % len(codes)], 'status': 'WAITING_DATA',
                    'actions': {'cash_payment': {'state': 'NOT_DUE', 'operation_date': '2027-12-01'}}})
                insert('entitlements', account, case, {'case_id': case, 'kind': 'CASH',
                    'status': 'WAITING_PAYMENT', 'amount_fen': 1000, 'paid_fen': 0,
                    'code': codes[number % len(codes)], 'payment_date': '2027-12-01'})
    return settings, repository, sessions, codes, counts, correction_day


@contextmanager
def instrument_sql():
    """Count values crossing sqlite/Python, not disk I/O or internal rows visited."""
    stats = {'selects': 0, 'returned_rows': 0, 'returned_value_bytes': 0}
    plans = {}
    original = sqlite3.connect

    class Cursor(sqlite3.Cursor):
        def execute(self, sql, parameters=()):
            self.measured = sql.lstrip().upper().startswith('SELECT')
            if self.measured:
                stats['selects'] += 1
                if sql not in plans and len(plans) < 64:
                    explained = sqlite3.Connection.execute(self.connection,
                        'EXPLAIN QUERY PLAN ' + sql, parameters).fetchall()
                    plans[sql] = [tuple(row) for row in explained]
            return super().execute(sql, parameters)

        def count(self, rows):
            if self.measured:
                stats['returned_rows'] += len(rows)
                stats['returned_value_bytes'] += sum(sum(len(value.encode())
                    if isinstance(value, str) else len(value) if isinstance(value, bytes)
                    else len(str(value).encode()) for value in row) for row in rows)
            return rows

        def fetchone(self):
            row = super().fetchone()
            self.count([] if row is None else [row])
            return row

        def fetchall(self):
            return self.count(super().fetchall())

        def __next__(self):
            row = super().__next__()
            self.count([row])
            return row

    class Connection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            return self.cursor(factory=Cursor).execute(sql, parameters)

    def connect(*args, **kwargs):
        return original(*args, **{**kwargs, 'factory': Connection})

    with patch.object(sqlite3, 'connect', connect):
        yield stats, plans


def measure(function):
    with instrument_sql() as (stats, plans):
        started = perf_counter()
        result = function()
        elapsed = perf_counter() - started
    return result, {'seconds': elapsed, 'sql': stats,
                    'result_json_bytes': len(encoded(result)),
                    'query_plans': [{'sql': sql, 'plan': plan} for sql, plan in plans.items()]}


def run_scale(years):
    with tempfile.TemporaryDirectory(prefix=f'ashare-capacity-{years}y-') as directory:
        root = Path(directory).resolve()
        setup = perf_counter()
        settings, repository, sessions, codes, counts, correction = fixture(root, years)
        setup_seconds = perf_counter() - setup
        market = MarketDataService(root)
        def clock():
            return datetime.fromisoformat(NOW)
        book = LedgerService(settings, market=market, clock=clock, test_mode=True)
        app = WorkflowApplication(settings, ledger=book, market=market, clock=clock, test_mode=True)
        before = {str(p.relative_to(root)): sha(p) for p in (settings.database, repository.path)}
        result = {'years': years, 'trading_sessions': len(sessions), 'cumulative_codes': len(codes),
            'fixture_setup_seconds': setup_seconds, 'ledger_table_counts': dict(counts),
            'fixture_order_counts_per_day': ACCOUNTS, 'historical_correction_session': correction,
            'fixture_semantics': 'Synthetic read workload, not execution/accounting acceptance',
            'database_bytes': {str(p.relative_to(root)): p.stat().st_size
                               for p in (settings.database, repository.path)}, 'passes': {}}
        with repository.connection() as db:
            result['market_observations'] = db.execute('SELECT COUNT(*) FROM observations').fetchone()[0]
        for temperature in ('cold', 'warm'):
            reads = {}
            with verification_scope(root, persist=temperature == 'cold') as metrics:
                statuses, reads['metadata'] = measure(lambda: {
                    account: {'revision': book.revision_state(account),
                              'status': book.workflow_status(account, through=END)} for account in ACCOUNTS})
                assert all(len(v['status']['statements']) == len(sessions) for v in statuses.values())
                due, reads['recovery_due_and_statement_inputs'] = measure(lambda: {
                    account: app._due(account, {'completed_session': END, 'trading_sessions': sessions})
                    for account in ACCOUNTS})
                assert all(days == sessions[-31:] for days in due.values())

                def economic():
                    return {account: book._read(account, lambda tx: {
                        'cash_fen': tx.balance(), 'latest': latest_statements(tx, summary=True),
                        'today_orders': tx.select('orders', fields=('id', 'quantity', 'amount_fen', 'fees_fen'),
                                                 equals={'target_session': END}),
                        'fill_totals': tuple(tx.db.execute("""SELECT
                            SUM(json_extract(data,'$.realized_pnl_fen')),
                            SUM(json_extract(data,'$.fees_fen')) FROM fills
                            WHERE account_id=? AND json_extract(data,'$.target_session')<=?""",
                            (account, END)).fetchone()),
                        'pending_rights': tx.select('entitlements', fields=('id', 'status', 'amount_fen'))})
                        for account in ACCOUNTS}

                economics, reads['close_economic_projection_only'] = measure(economic)
                assert all(len(v['pending_rights']) == 20 * years for v in economics.values())
                prices, reads['market_prices_121_sessions_all_current_codes'] = measure(lambda: {
                    code: market.prices(code, sessions[-121], END, knowledge_cutoff=NOW).as_dict()
                    for code in codes})
                assert all(v['state'] == 'READY' and len(v['data']) == 121 for v in prices.values())
                reports, reads['recent_60_report_selection_no_render'] = measure(lambda: {
                    account: book.report_snapshot(account, END, mode='recent', recent_sessions=60)
                    for account in ACCOUNTS})
                assert all(len(v['statements']) == 60 and len(v['orders']) == 60 * ACCOUNTS[account]
                           for account, v in reports.items())
            result['passes'][temperature] = {'reads': reads, 'verification': dict(metrics),
                'total_measured_seconds': sum(row['seconds'] for row in reads.values())}
        result['databases_unchanged'] = all(sha(root / path) == digest for path, digest in before.items())
        assert result['databases_unchanged']
        warm = result['passes']['warm']['verification']
        assert warm['full_checks'] == 0 and warm['cache_hits'] >= len(sessions) * 3
        result['validation_receipt_bytes'] = sum(p.stat().st_size for p in
            (root / 'data/state/verification').glob('*.json'))
        result['raw_objects_bytes'] = sum(p.stat().st_size for p in repository.objects.glob('*.json'))
        result['peak_process_rss_bytes'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (
            1 if sys.platform == 'darwin' else 1024)
        result['peak_rss_scope'] = 'Process lifetime including fixture construction and prior scales; not delta'
        return result


def storage_sizes(root):
    groups = {'ledger': 'data/state/ledger.sqlite3', 'market': 'data/state/market.sqlite3',
              'objects': 'data/normalized/evidence-objects', 'plans': 'data/plans',
              'raw_market': 'data/raw', 'reports': 'reports', 'backups': 'data/backups',
              'verification': 'data/state/verification'}
    result = {}
    for name, relative in groups.items():
        path = root / relative
        files = [path] if path.is_file() else list(path.rglob('*')) if path.exists() else []
        seen, logical, allocated, count = set(), 0, 0, 0
        for file in files:
            if not file.is_file() or file.is_symlink():
                continue
            info = file.stat()
            logical += info.st_size
            count += 1
            key = info.st_dev, info.st_ino
            if key not in seen:
                allocated += info.st_blocks * 512
                seen.add(key)
        result[name] = {'logical_bytes': logical, 'inode_unique_allocated_bytes': allocated,
                        'files': count}
    return result


def historical_digest(database):
    result = {}
    with sqlite3.connect(database) as db:
        for table in ('orders', 'fills', 'daily_nav'):
            digest = hashlib.sha256()
            count = 0
            day = 'session' if table == 'daily_nav' else 'target_session'
            for row in db.execute(f'SELECT id,account_id,data FROM {table} '
                                  f"WHERE json_extract(data,'$.{day}')<=? ORDER BY account_id,id", (END,)):
                digest.update(encoded(row))
                count += 1
            result[table] = {'count': count, 'sha256': digest.hexdigest()}
    return result


def growth_estimates(core, backup_bytes):
    return {'trading_days_per_month': 21, 'business_bytes_per_day': core,
        'business_bytes_per_21_similar_days': 21 * core,
        'backup_current_day_logical_bytes': backup_bytes,
        'backup_30_identical_size_points_logical_bytes': 30 * backup_bytes,
        'backup_30_growing_points_scenario_bytes': 30 * backup_bytes + sum(range(30)) * core,
        'note': 'Conditional same 180-code/16-order shape. Growing-point scenario adds the measured business delta per subsequent point; not an observed month, universal upper bound, or APFS exclusive bytes.'}


def run_append_day(output):
    """One real new-day API workload over five years of explicitly synthetic history."""
    day, planning_time = '2026-11-03', END + 'T20:00:00+08:00'
    with tempfile.TemporaryDirectory(prefix='ashare-append-5y-') as directory:
        root = Path(directory).resolve()
        output['isolated_root'] = str(root)
        setup = perf_counter()
        # Base policy creation cannot be later than the controlled planning cutoff.
        with patch.dict(globals(), NOW=planning_time):
            settings, repository, sessions, codes, counts, _ = fixture(root, 5)
        clock = [datetime.fromisoformat(planning_time)]
        market = MarketDataService(root)  # All observations below are local synthetic originals.
        book = LedgerService(settings, market=market, clock=lambda: clock[0], test_mode=True)
        app = ExecutionApplication(settings, market=market, ledger=book,
                                   clock=lambda: clock[0], test_mode=True)
        # Preserve future pending-rights pressure without falsely making them due today.
        with sqlite3.connect(settings.database) as db:
            db.execute("UPDATE corporate_action_cases SET data=json_set(data,'$.record_date','2027-11-01')")
        with repository.connection(write=True) as db:
            for code in codes:
                security = [{'code': code, 'date': sessions[0], 'listed': '2000-01-01',
                    'name': '隔离压力证券', 'board': '沪市主板', 'source_status': 1,
                    'eligibility_known': True}]
                repository.put('security', security, source(security), connection=db)
                coverage = [{'code': code, 'date': END, 'purpose': 'events', 'start': sessions[0],
                    'end': END, 'complete': True, 'scope': 'reported_cash_bonus_capital_distributions'}]
                repository.put('coverage', coverage, source(coverage), connection=db)
            pool = [{'code': '', 'date': END, 'effective_from': day,
                'selected': [{'code': code, 'name': '隔离压力证券'} for code in codes],
                'excluded': [], 'frozen_at': planning_time}]
            repository.put('pool', pool, source(pool, planning_time), connection=db)
        output.update(fixture_setup_seconds=perf_counter() - setup, years=5,
            trading_sessions=len(sessions), cumulative_codes=len(codes),
            historical_counts=dict(counts), order_counts=ACCOUNTS,
            history_semantics='Synthetic pressure only; not historical financial conservation or a backtest',
            stages={}, account_results={})
        before = storage_sizes(root)
        history = historical_digest(settings.database)
        initial_cash = {account: book._read(account, lambda tx: tx.balance()) for account in ACCOUNTS}
        plans = {}

        def phase(name, function):
            cpu = resource.getrusage(resource.RUSAGE_SELF)
            with verification_scope(root, persist=True) as metrics:
                result, measured = measure(function)
            after_cpu = resource.getrusage(resource.RUSAGE_SELF)
            measured['cpu_seconds'] = (after_cpu.ru_utime + after_cpu.ru_stime - cpu.ru_utime - cpu.ru_stime)
            measured['verification'] = dict(metrics)
            output['stages'][name] = measured
            print(json.dumps({'stage': name, 'seconds': measured['seconds'],
                              'state': result.get('state') if isinstance(result, dict) else 'MEASURED'}), flush=True)
            return result

        with patch('china_stock_sim.market.service.timestamp', lambda: clock[0].isoformat()):
            for account, size in ACCOUNTS.items():
                state = book.account_state(account, day)
                proposal = StrategyProposal(proposal_id=f'capacity:{account}:{day}', account_id=account,
                    strategy_id=state['strategy_id'], strategy_version='isolated-capacity',
                    target_session=day, market_as_of=END, generated_at=planning_time,
                    input_cutoff=planning_time, account_snapshot_id=state['state_id'],
                    account_revision=state['account_revision'], ordered_intents=tuple(
                        ExecutionIntent(code, 'BUY', 'isolated capacity', number, '10.00', quantity=100)
                        for number, code in enumerate(codes[:size])))
                saved = phase(f'freeze:{account}', lambda p=proposal, a=account:
                              app.freeze(p, operation_id=f'capacity-freeze:{a}'))
                if saved['state'] != 'APPLIED':
                    raise AssertionError({'phase': 'freeze', 'account': account, 'result': saved})
                plans[account] = saved['plan']['plan_id']
            clock[0] = datetime.fromisoformat(day + 'T20:00:00+08:00')

            def new_market_day():
                with repository.connection(write=True) as db:
                    for code in codes:
                        rows = [price(code, day)]
                        repository.put('bar', rows, source(rows, day + 'T18:11:00+08:00'), connection=db)
                    for code in codes:
                        rows = [{'code': code, 'date': day, 'purpose': 'events', 'start': sessions[0],
                            'end': day, 'complete': True, 'scope': 'reported_cash_bonus_capital_distributions'}]
                        repository.put('coverage', rows, source(rows, day + 'T18:11:00+08:00'), connection=db)
                return {'state': 'LOCAL_FIXTURE_INSERTED', 'bars': len(codes), 'coverage': len(codes)}

            phase('market_day', new_market_day)
            for account, size in ACCOUNTS.items():
                settled = phase(f'settle:{account}', lambda a=account: app.settle(
                    a, plans[a], operation_id=f'capacity-settle:{a}'))
                if settled.get('completion_status') != 'COMPLETE':
                    raise AssertionError({'phase': 'settle', 'account': account, 'result': settled})
                closed = phase(f'close:{account}', lambda a=account: book.close_session(a,
                    {'session': day}, operation_id=f'capacity-close:{a}',
                    expected_revision=book.revision_state(a)['account_revision']))
                if closed['state'] != 'APPLIED':
                    raise AssertionError({'phase': 'close', 'account': account, 'result': closed})
                assert closed['statement']['accounting_status'] == 'COMPLETE', closed['statement']['issues']
                assert closed['statement']['valuation_status'] == 'FINAL'
                fills = book._read(account, lambda tx: tx.select('fills',
                    fields=('id', 'quantity', 'amount_fen', 'fees_fen'), equals={'target_session': day}))
                fee = fee_breakdown('10.00', 100, 'BUY', settings.raw['fees'], day)
                expected_fee = fee['fees_fen']
                cash = book._read(account, lambda tx: tx.balance())
                assert len(fills) == size and all(f['quantity'] == 100 and f['amount_fen'] == 100000
                    and f['fees_fen'] == expected_fee for f in fills)
                assert cash == initial_cash[account] - size * (100000 + expected_fee)
                output['account_results'][account] = {'new_fills': len(fills), 'closing_cash_fen': cash,
                    'fees_fen': size * expected_fee, 'statement_status': closed.get('statement', {})}
            output['after_business'] = storage_sizes(root)
            render = phase('render_recent60', lambda: write_ledger_report(
                settings, read_snapshot(settings), ledger=book))
            assert render['state'] == 'LEDGER_REPORT'
            output['after_report'] = storage_sizes(root)
            backup = phase('backup', lambda: book.backup(label='daily'))
            if backup['state'] != 'COMPLETE':
                raise AssertionError({'phase': 'backup', 'evidence': backup['evidence']})
            verified = phase('verify_backup', lambda: book.verify_backup(backup['backup_id']))
            assert verified['state'] == 'READY', verified
            output['backup'] = {k: backup[k] for k in ('backup_id', 'state', 'storage')}
            output['backup_verification'] = verified
            output['after_backup'] = storage_sizes(root)
            # One explicit idempotency boundary; no second benchmark of the day.
            before_replay = sha(settings.database)
            for account in ACCOUNTS:
                replay = book.close_session(account, {'session': day},
                    operation_id=f'capacity-close:{account}', expected_revision=0)
                assert replay['state'] == 'ALREADY_APPLIED', replay
            assert sha(settings.database) == before_replay
        assert historical_digest(settings.database) == history
        output['before'] = before
        output['historical_rows_unchanged'] = True
        output['replay_database_unchanged'] = True
        with sqlite3.connect(settings.database) as db:
            assert db.execute('SELECT count(*) FROM model_attempts').fetchone()[0] == 0
        after = output['after_backup']
        output['growth'] = {key: {metric: after[key][metric] - before[key][metric]
            for metric in ('logical_bytes', 'inode_unique_allocated_bytes', 'files')} for key in before}
        core = sum(output['growth'][key]['logical_bytes'] for key in ('ledger', 'market', 'objects', 'plans', 'raw_market'))
        output['conditional_estimates'] = growth_estimates(core, after['backups']['logical_bytes'])
        output['peak_process_rss_bytes'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (
            1 if sys.platform == 'darwin' else 1024)
        output['thresholds'] = {'read_bundle_same_5y_workload_seconds': 2 * 6.573778290767223,
            'business_stage_budget_seconds': 3600, 'same_shape_daily_growth_review_bytes': 2 * core,
            'action': 'Investigate changed workload or scaling; never auto-delete evidence.'}
        output['isolated_fixture_removed_on_exit'] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--append-day', action='store_true', help='Only one 5-year new-day write/render/backup workload')
    args = parser.parse_args()
    if args.append_day:
        output = {'schema_version': 1, 'state': 'RUNNING', 'recorded_at': datetime.now().astimezone().isoformat(),
            'scope': 'One synthetic 5-year pressure background plus real new-day ledger APIs; no production/network/model',
            'script_sha256': sha(Path(__file__)), 'verification_identity_start': verification_identity()}
        try:
            with patch.object(socket.socket, 'connect', side_effect=AssertionError('Network forbidden')):
                run_append_day(output)
            output['state'] = 'PASS'
        except Exception as exc:
            output.update(state='FAILED', error_type=type(exc).__name__, error=str(exc)[:4000])
            raise
        finally:
            output['verification_identity_end'] = verification_identity()
            output['validator_unchanged_during_measurement'] = (
                output['verification_identity_start'] == output['verification_identity_end'])
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n')
        return
    output = {'schema_version': 1, 'state': 'RUNNING', 'recorded_at': datetime.now().astimezone().isoformat(),
        'scope': 'Isolated compact read paths at 1/3/5 years, one cold and one warm pass per size',
        'exclusions': ['No production reads/writes', 'No collection/models/network',
            'No actual close_session/settle', 'No HTML rendering/backup/growth-day acceptance',
            'No claim of constant-time historical statement fingerprint enumeration'],
        'measurement': {'sqlite_bytes': 'UTF-8 returned value sizes, not filesystem I/O',
            'query_plan': 'EXPLAIN first instance of each SQL shape, included in measured wall time',
            'calendar': 'Synthetic weekdays, not exchange calendar',
            'cache': 'cold is application verification-cache cold; OS page cache is not flushed'},
        'verification_identity_start': verification_identity(),
        'script_sha256': sha(Path(__file__)), 'scales': []}
    try:
        with patch.object(socket.socket, 'connect', side_effect=AssertionError('Network forbidden')):
            for years in (1, 3, 5):
                row = run_scale(years)
                output['scales'].append(row)
                print(json.dumps({'years': years, 'market_observations': row['market_observations'],
                    'seconds': {k: v['total_measured_seconds'] for k, v in row['passes'].items()},
                    'verification': {k: v['verification'] for k, v in row['passes'].items()}}, ensure_ascii=False),
                    flush=True)
        output['state'] = 'PASS'
    finally:
        output['verification_identity_end'] = verification_identity()
        output['validator_unchanged_during_measurement'] = (
            output['verification_identity_start'] == output['verification_identity_end'])
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    main()
