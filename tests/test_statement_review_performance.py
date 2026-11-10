"""WP07/08: fresh dynamic guards around disposable, run-local semantic reuse."""

import json
import os
import sqlite3
from contextvars import copy_context
from copy import deepcopy
from datetime import datetime
from threading import Thread
from types import SimpleNamespace

import pytest

from china_stock_sim.application.workflow import WorkflowApplication
from china_stock_sim.application.workflow_ledger import LedgerWorkflow
from china_stock_sim.ledger import LedgerService
from china_stock_sim.ledger import statement_inputs as inputs
from china_stock_sim.ledger.decision_evidence import verification_scope
from china_stock_sim.ledger.kernel import LedgerError, canonical
from china_stock_sim.market.events import normalize_eastmoney_events
from china_stock_sim.market.repository import MarketRepository
from china_stock_sim.market.service import MarketDataService

DAY = '2026-11-03'
EARLY = DAY + 'T19:00:00+08:00'
LATE = DAY + 'T21:00:00+08:00'


class Facts:
    def __init__(self):
        self.result = {'state': 'READY', 'data': [{'code': 'SH600000', 'date': DAY,
                        'close': '10.00'}], 'issues': []}
        self.fingerprint = 'original'
        self.fingerprints, self.queries = [], []

    def statement_input_fingerprint(self, method, kwargs):
        self.fingerprints.append((method, dict(kwargs)))
        return {'state': 'READY', 'fingerprint': self.fingerprint}

    def prices(self, **kwargs):
        self.queries.append(kwargs)
        return SimpleNamespace(as_dict=lambda: deepcopy(self.result))


def seed(book, *, account='S1', revision=1, query=None, identity=None):
    query = query or {'method': 'prices', 'kwargs': {'code': 'SH600000', 'start': DAY,
        'end': DAY, 'knowledge_cutoff': EARLY}, 'result': deepcopy(book.market.result)}
    row = {'session': DAY, 'statement_revision': revision, 'knowledge_cutoff': EARLY,
           'accounting_status': 'COMPLETE', 'valuation_status': 'FINAL',
           'account_revision': 0, 'data_evidence': [query]}
    with sqlite3.connect(book.settings.database) as db:
        db.execute('INSERT INTO daily_nav VALUES (?,?,?)',
                   (identity or f'nav:{revision}', account, canonical(row)))


@pytest.fixture
def book(settings, monkeypatch):
    import socket

    monkeypatch.setattr(socket.socket, 'connect', lambda *a: pytest.fail('must remain offline'))
    now = [datetime.fromisoformat(LATE)]
    value = LedgerService(settings, market=Facts(), clock=lambda: now[0], test_mode=True)
    value.initialize()
    value.test_now = now
    seed(value)
    return value


def review(book, *, account='S1', cutoff=EARLY):
    return book.statement_input_changes(account, DAY, knowledge_cutoff=cutoff)


def projection_counter(monkeypatch):
    actual, calls = inputs._projection, []

    def project(*args):
        calls.append(args[0])
        return actual(*args)

    monkeypatch.setattr(inputs, '_projection', project)
    return calls


def test_scope_reuses_semantics_but_rechecks_each_fingerprint_and_changes_no_files(book, monkeypatch):
    calls = projection_counter(monkeypatch)
    before = {str(p): p.read_bytes() for p in book.root.rglob('*') if p.is_file()}
    with book.statement_input_review_scope() as metrics:
        first = review(book)
        assert calls == ['prices', 'prices']
        assert review(book) == first
        later = review(book, cutoff=LATE)
        assert later == {**first, 'knowledge_cutoff': LATE}
        assert len(calls) == 2 and len(book.market.queries) == 1
        assert len(book.market.fingerprints) == 3 and metrics['hits'] == 4
        assert set(metrics) == {'hits', 'entries', 'bytes', 'disabled'}
    assert inputs._REVIEW_SCOPE.get() is None
    assert {str(p): p.read_bytes() for p in book.root.rglob('*') if p.is_file()} == before
    review(book)
    assert len(calls) == 4 and len(book.market.queries) == 2


def test_raw_json_projection_is_opt_in_and_default_stays_decoded(book):
    decoded = book._read('S1', lambda tx: tx.select('daily_nav', fields=('data_evidence',)))
    encoded = book._read('S1', lambda tx: tx.select('daily_nav', fields=('data_evidence',), decode_json=False))
    assert isinstance(decoded[0]['data_evidence'], list)
    assert json.loads(encoded[0]['data_evidence']) == decoded[0]['data_evidence']


@pytest.mark.parametrize('change', ['new_revision', 'same_id_bytes', 'other_account', 'validator'])
def test_static_projection_binds_exact_statement_identity_and_validator(book, monkeypatch, change):
    calls = projection_counter(monkeypatch)
    with book.statement_input_review_scope():
        review(book)
        if change == 'new_revision':
            seed(book, revision=2)
        elif change == 'same_id_bytes':
            with sqlite3.connect(book.settings.database) as db:
                row = json.loads(db.execute('SELECT data FROM daily_nav').fetchone()[0])
                row['data_evidence'][0]['result']['data'][0]['close'] = '11.00'
                db.execute('UPDATE daily_nav SET data=?', (canonical(row),))
        elif change == 'other_account':
            seed(book, account='LLM')
        else:
            monkeypatch.setattr(inputs, 'verification_identity', lambda: 'new-validator')
        result = review(book, account='LLM' if change == 'other_account' else 'S1')
        assert len(calls) >= 3
        assert result['state'] == ('CHANGED' if change == 'same_id_bytes' else 'READY')


def test_fresh_dependency_finds_same_clock_late_data_and_return_mutation_is_isolated(book):
    with book.statement_input_review_scope():
        assert review(book)['state'] == 'READY'
        book.market.result['data'][0]['close'] = '12.00'
        book.market.fingerprint = 'late-data'
        changed = review(book)
        assert changed['state'] == 'CHANGED'
        expected = deepcopy(changed)
        changed['changes'][0]['data_evidence']['result']['data'][0]['close'] = 'invented'
        assert review(book) == expected
        book.market.result['data'][0]['close'] = '13.00'
        book.market.fingerprint = 'later-data'
        assert review(book)['changes'][0]['after_hash'] != expected['changes'][0]['after_hash']


@pytest.mark.parametrize('fallback', ['absent', 'unavailable', 'capacity', 'entry_capacity',
                                     'cache_failure', 'cache_write_failure'])
def test_missing_proof_or_cache_failure_uses_full_query(book, monkeypatch, fallback):
    calls = projection_counter(monkeypatch)
    if fallback == 'absent':
        book.market.statement_input_fingerprint = None
    elif fallback == 'unavailable':
        book.market.statement_input_fingerprint = lambda *a: {'state': 'UNAVAILABLE'}
    elif fallback == 'capacity':
        monkeypatch.setattr(inputs, '_REVIEW_MAX_BYTES', 1)
    elif fallback == 'entry_capacity':
        monkeypatch.setattr(inputs, '_REVIEW_MAX_ENTRIES', 0)
    with book.statement_input_review_scope() as metrics:
        expected = review(book)
        if fallback in {'cache_failure', 'cache_write_failure'}:
            class Broken(dict):
                def get(self, *args):
                    if fallback == 'cache_failure':
                        raise OSError('disposable cache failed')
                    return None

                def __setitem__(self, *args):
                    raise OSError('disposable cache write failed')
            inputs._REVIEW_SCOPE.get()['values'] = Broken()
        assert review(book) == expected
        assert len(book.market.queries) == 2
        assert len(calls) >= 3
        if fallback in {'cache_failure', 'cache_write_failure'}:
            assert metrics['disabled']
        if fallback in {'capacity', 'entry_capacity'}:
            assert metrics['bytes'] == metrics['entries'] == 0


def test_cache_does_not_cross_ledger_adapter_or_nested_scope_and_clears_on_error(book):
    with pytest.raises(RuntimeError), book.statement_input_review_scope():
        review(book)
        other = LedgerService(book.settings, market=book.market, clock=book._clock, test_mode=True)
        review(other)
        with book.statement_input_review_scope():
            review(book)
        assert len(book.market.queries) == 3
        assert review(book)['state'] == 'READY'
        replacement = Facts()
        book.market = replacement
        review(book)
        assert len(replacement.queries) == 1
        raise RuntimeError('isolated interruption')
    assert inputs._REVIEW_SCOPE.get() is None


@pytest.mark.parametrize('failure', ['corrupt_fingerprint', 'corrupt_result', 'query_exception'])
def test_failures_never_reuse_old_success_or_become_cached_success(book, failure):
    with book.statement_input_review_scope():
        assert review(book)['state'] == 'READY'
        book.market.fingerprint = 'different'
        if failure == 'corrupt_fingerprint':
            book.market.statement_input_fingerprint = lambda *a: {'state': 'CORRUPT'}
        elif failure == 'corrupt_result':
            book.market.result['state'] = 'CORRUPT'
        else:
            book.market.prices = lambda **k: (_ for _ in ()).throw(OSError('unavailable'))
        for _ in range(2):
            with pytest.raises(LedgerError) as caught:
                review(book)
            assert caught.value.state == 'INTEGRITY_ERROR'
            if failure == 'corrupt_result':
                assert str(caught.value) == '账单输入原件损坏'


def test_new_cutoff_before_statement_is_rejected_even_when_compiled(book):
    with book.statement_input_review_scope():
        review(book)
        with pytest.raises(LedgerError) as caught:
            review(book, cutoff=DAY + 'T18:00:00+08:00')
        assert caught.value.state == 'INVALID_INPUT'


def test_copied_context_after_exit_cannot_reopen_cache_lifetime(book):
    with book.statement_input_review_scope() as metrics:
        review(book)
        context = copy_context()
    assert context.run(review, book)['state'] == 'READY'
    assert context.run(review, book)['state'] == 'READY'
    assert len(book.market.queries) == 3 and metrics['hits'] == 0


def test_copied_context_in_other_thread_uses_full_checks(book):
    results = []
    with book.statement_input_review_scope() as metrics:
        review(book)
        context = copy_context()
        thread = Thread(target=lambda: results.append(context.run(review, book)))
        thread.start()
        thread.join(5)
        assert not thread.is_alive() and results[0]['state'] == 'READY'
        assert len(book.market.queries) == 2 and metrics['hits'] == 0


def test_refresh_rechecks_inside_transaction_and_cas_still_applies(book):
    with book.statement_input_review_scope():
        assert review(book)['state'] == 'READY'
        book.market.result['data'][0]['close'] = '12.00'
        book.market.fingerprint = 'late'
        result = book.refresh_statement_inputs('S1', {'session': DAY, 'knowledge_cutoff': EARLY},
            operation_id='refresh', expected_revision=0)
        assert result['state'] == 'APPLIED' and result['changed']
        assert book.revision_state('S1')['restate_from'] == DAY
        before = book.settings.database.read_bytes()
        stale = book.refresh_statement_inputs('S1', {'session': DAY, 'knowledge_cutoff': EARLY},
            operation_id='stale-refresh', expected_revision=0)
        assert stale['state'] == 'STALE_ACCOUNT'
        assert book.settings.database.read_bytes() == before


def test_due_rechecks_new_financial_obligations_with_warm_input_cache(book):
    with sqlite3.connect(book.settings.database) as db:
        state = json.loads(db.execute("SELECT data FROM account_state WHERE account_id='S1'").fetchone()[0])
        state['simulation_start_session'] = DAY
        db.execute("UPDATE account_state SET data=? WHERE account_id='S1'", (canonical(state),))
    app = WorkflowApplication(book.settings, ledger=book, market=book.market,
                              clock=lambda: book.test_now[0], test_mode=True)
    schedule = {'completed_session': DAY, 'trading_sessions': [DAY]}
    with book.statement_input_review_scope():
        assert app._due('S1', schedule) == []
        with sqlite3.connect(book.settings.database) as db:
            db.execute('INSERT INTO orders VALUES (?,?,?)', ('new-order', 'S1', canonical({
                'target_session': DAY, 'status': 'PENDING', 'code': 'SH600000', 'plan_id': 'plan'})))
        assert app._due('S1', schedule) == [DAY]


def source(rows, fetched, **extra):
    return {'provider': 'isolated', 'adapter_version': 'test', 'rows': rows,
            'fetched_at': fetched, **extra}


def test_exact_coverage_cutoff_rechecks_future_raw_leaf_even_when_fingerprint_is_same(book, monkeypatch):
    repository = MarketRepository(book.root)
    leaf = repository.save_raw(source([], DAY + 'T20:00:00+08:00'))
    rows = [{'code': 'SH600000', 'date': DAY, 'purpose': 'events', 'start': DAY,
             'end': DAY, 'complete': True}]
    repository.put('coverage', rows, source(rows, EARLY, dependencies=[leaf]))
    market = book.market = MarketDataService(book.root)
    kwargs = {'code': 'SH600000', 'start': DAY, 'end': DAY, 'knowledge_cutoff': EARLY}
    original = market.event_coverage(**kwargs)
    assert original.state == 'WAITING_DATA'
    query = {'method': 'event_coverage', 'kwargs': kwargs, 'result': original.as_dict()}
    with sqlite3.connect(book.settings.database) as db:
        db.execute('DELETE FROM daily_nav')
    seed(book, query=query)
    early_fp = market.statement_input_fingerprint('event_coverage', kwargs)
    assert early_fp == market.statement_input_fingerprint('event_coverage', {**kwargs, 'knowledge_cutoff': LATE})
    assert market.event_coverage(**{**kwargs, 'knowledge_cutoff': LATE}).state == 'READY'
    calls = projection_counter(monkeypatch)
    with verification_scope(book.root, persist=True), book.statement_input_review_scope():
        assert review(book)['state'] == 'READY'
        assert review(book)['state'] == 'READY'
        changed = review(book, cutoff=LATE)
        assert changed['state'] == 'CHANGED'
        assert changed['changes'][0]['data_evidence']['result']['state'] == 'READY'
        assert len(calls) == 3  # Same original before projection; two distinct cutoff results.
    # Time-sensitive queries do not create transient persistent receipts.
    assert not (book.root / 'data/state/verification').exists()
    assert review(book, cutoff=LATE) == changed
    assert review(book)['state'] == 'READY'


def test_future_event_batch_member_with_same_fingerprint_rechecks_exact_cutoff(book, monkeypatch):
    from test_market_event_rebuild import row

    repository = MarketRepository(book.root)
    raw = source([row('SH600000')], LATE, provider='eastmoney_events')
    reference = repository.save_raw(raw)
    events = normalize_eastmoney_events(raw['rows'], 'SH600000', LATE, raw_reference=reference)
    members = repository.put('event', events, raw)
    batch = {'code': 'SH600000', 'date': DAY, 'batch_key': 'eastmoney:all', 'members': members}
    repository.put('event_batch', [batch], source([], EARLY, dependencies=[reference]),
                   key_fields=['code', 'batch_key'])
    market = book.market = MarketDataService(book.root)
    kwargs = {'code': 'SH600000', 'knowledge_cutoff': EARLY}
    original = market.events(**kwargs)
    assert original.ready and original.data == []
    later = {**kwargs, 'knowledge_cutoff': LATE}
    assert market.events(**later).ready and len(market.events(**later).data) == 1
    assert (market.statement_input_fingerprint('events', kwargs)
            == market.statement_input_fingerprint('events', later))
    with sqlite3.connect(book.settings.database) as db:
        db.execute('DELETE FROM daily_nav')
    seed(book, query={'method': 'events', 'kwargs': kwargs, 'result': original.as_dict()})
    calls = projection_counter(monkeypatch)
    # Neither old cross-cutoff proofs nor new exact-cutoff transient files are used.
    monkeypatch.setattr(inputs, '_receipt', lambda *a: pytest.fail('time-sensitive durable receipt read'))
    monkeypatch.setattr(inputs, '_save_receipt', lambda *a, **k: pytest.fail('transient receipt write'))
    with verification_scope(book.root, persist=True), book.statement_input_review_scope():
        assert review(book)['state'] == 'READY'
        assert review(book)['state'] == 'READY'
        changed = review(book, cutoff=LATE)
        assert changed['state'] == 'CHANGED' and len(calls) == 3
        evidence = changed['changes'][0]['data_evidence']
        assert evidence['kwargs'] == later and len(evidence['result']['data']) == 1
    assert not (book.root / 'data/state/verification').exists()


@pytest.mark.parametrize('damage', ['raw_same_size', 'index_payload', 'deleted_source'])
def test_real_fingerprint_invalidates_warm_projection_on_damage(book, damage):
    repository = MarketRepository(book.root)
    rows = [{'code': 'SH600000', 'date': DAY, 'purpose': 'events', 'start': DAY,
             'end': DAY, 'complete': True}]
    repository.put('coverage', rows, source(rows, EARLY))
    book.market = MarketDataService(book.root)
    kwargs = {'code': 'SH600000', 'start': DAY, 'end': DAY, 'knowledge_cutoff': EARLY}
    query = {'method': 'event_coverage', 'kwargs': kwargs,
             'result': book.market.event_coverage(**kwargs).as_dict()}
    with sqlite3.connect(book.settings.database) as db:
        db.execute('DELETE FROM daily_nav')
    seed(book, query=query)
    with book.statement_input_review_scope():
        assert review(book)['state'] == 'READY'
        with repository.connection() as db:
            raw = db.execute("SELECT raw_ref FROM observations WHERE kind='coverage'").fetchone()[0]
        path = book.root / raw
        if damage == 'deleted_source':
            path.unlink()
        elif damage == 'raw_same_size':
            stamp = path.stat()
            path.write_bytes(path.read_bytes().replace(b'isolated', b'damaged!'))
            os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        else:
            with repository.connection(write=True) as db:
                db.execute("UPDATE observations SET payload=replace(payload, 'true', 'false') WHERE kind='coverage'")
        with pytest.raises(LedgerError) as caught:
            review(book)
        assert caught.value.state == 'INTEGRITY_ERROR'


@pytest.mark.parametrize('legacy', [False, True])
def test_workflow_revision_only_writes_keep_ids_and_cas_with_legacy_fallback(settings, legacy):
    seen = []
    ledger = SimpleNamespace(account_state=lambda *a: {'account_revision': 7},
        close_session=lambda account, payload, **kwargs: seen.append((account, payload, kwargs)) or {'state': 'APPLIED'},
        daily_statement=lambda *a: {'session': DAY})
    if not legacy:
        ledger.revision_state = lambda account: {'account_revision': 7}
        ledger.account_state = lambda *a: pytest.fail('revision-only call expanded account_state')
    adapter = LedgerWorkflow(settings, ledger=ledger, market=None, clock=lambda: datetime.fromisoformat(LATE))
    assert adapter.close('S1', DAY)['receipt']['state'] == 'APPLIED'
    assert seen[0][2]['expected_revision'] == 7
    assert seen[0][2]['operation_id'].startswith('workflow:')
