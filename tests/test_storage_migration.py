import hashlib
import io
import json
import os
import sqlite3
import zlib
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from china_stock_sim.common.evidence_objects import (
    DIRECTORY,
    EvidenceObjectError,
    is_reference,
    iter_bytes,
    put_blob,
    put_json,
    read_json,
    verify,
)
from china_stock_sim.ledger.kernel import canonical
from china_stock_sim.ledger.storage_migration import (
    StorageMigrationError,
    apply_isolated,
    archive_record,
    capture_baseline,
    compact_record,
    dry_run,
    verify_financial_equivalence,
)


def test_cas_canonical_unicode_decimal_dates_and_exact_bytes(tmp_path):
    @dataclass
    class Item:
        amount: Decimal

    value = {'z': [True, None, Decimal('3.50'), Item(Decimal('1.01'))],
             '中文': '\n"\\', 'a': date(2026, 11, 3),
             'time': datetime(2026, 11, 3, tzinfo=timezone.utc)}
    ref = put_json(tmp_path, value)
    expected = canonical(value).encode()
    assert b''.join(iter_bytes(tmp_path, ref)) == expected
    assert ref['_evidence_ref']['sha256'] == hashlib.sha256(expected).hexdigest()
    assert read_json(tmp_path, ref) == json.loads(expected)
    assert verify(tmp_path, ref)['state'] == 'READY'


@pytest.mark.parametrize('factory', [lambda b: b, io.BytesIO, lambda b: iter([b[:3], b[3:]])])
def test_blob_stream_preserves_whitespace_and_shares_objects(tmp_path, factory):
    original = b' {"b": 2, "a": [1, 3]}\n'
    first = put_blob(tmp_path, factory(original))
    second = put_blob(tmp_path, original)
    assert first == second
    assert b''.join(iter_bytes(tmp_path, first)) == original
    assert len(list((tmp_path / DIRECTORY).rglob('*.zlib'))) == 1
    assert not list((tmp_path / DIRECTORY).glob('.object-*'))


def test_streaming_compression_and_bounded_restore(tmp_path):
    chunk = b'abc' * 400_000
    ref = put_blob(tmp_path, iter([chunk] * 3))
    assert ref['_evidence_ref']['stored_bytes'] < len(chunk) // 100
    assert sum(map(len, iter_bytes(tmp_path, ref))) == len(chunk) * 3
    assert max(map(len, iter_bytes(tmp_path, ref))) <= 1024 * 1024
    with pytest.raises(EvidenceObjectError, match='LIMIT'):
        read_json(tmp_path, ref, max_bytes=100)
    with pytest.raises(EvidenceObjectError, match='LIMIT'):
        put_blob(tmp_path, [chunk], max_bytes=10)
    assert not list((tmp_path / DIRECTORY).glob('.object-*'))


@pytest.mark.parametrize('value', [1.2, float('nan'), Decimal('NaN'), {1: 'bad'},
                                  datetime(2026, 11, 3), object()])
def test_invalid_financial_json_values_fail_closed(tmp_path, value):
    with pytest.raises(EvidenceObjectError):
        put_json(tmp_path, value)
    assert not list((tmp_path / DIRECTORY).rglob('*.zlib'))


def test_json_cycle_rejected_without_temp_leak(tmp_path):
    value = {}
    value['self'] = value
    with pytest.raises(EvidenceObjectError):
        put_json(tmp_path, value)
    assert not list((tmp_path / DIRECTORY).glob('.object-*'))


@pytest.mark.parametrize('raw', [b'{"x":1,"x":2}', b'{"x":1.2}', b'{"x":NaN}', b'not json'])
def test_explicit_json_read_checks_numbers_duplicates_and_syntax(tmp_path, raw):
    ref = put_blob(tmp_path, raw)
    assert verify(tmp_path, ref)['state'] == 'READY'
    with pytest.raises(EvidenceObjectError, match='JSON_INVALID'):
        read_json(tmp_path, ref)


@pytest.mark.parametrize('damage', ['truncate', 'append', 'alter', 'declared_size', 'raw_hash'])
def test_corrupt_object_or_reference_fails_closed(tmp_path, damage):
    ref = put_json(tmp_path, {'test': ['abc'] * 100})
    path = tmp_path / ref['_evidence_ref']['path']
    if damage == 'declared_size':
        ref['_evidence_ref']['bytes'] -= 1
    elif damage == 'raw_hash':
        ref['_evidence_ref']['sha256'] = '0' * 64
    else:
        path.chmod(0o644)
        raw = path.read_bytes()
        path.write_bytes(raw[:-1] if damage == 'truncate' else
                         raw + b'other stream' if damage == 'append' else b'wrong' + raw[5:])
    with pytest.raises(EvidenceObjectError):
        verify(tmp_path, ref)


def test_existing_corrupt_object_is_not_replaced(tmp_path):
    ref = put_json(tmp_path, {'x': 1})
    path = tmp_path / ref['_evidence_ref']['path']
    path.chmod(0o644)
    path.write_bytes(b'corrupt')
    with pytest.raises(EvidenceObjectError):
        put_json(tmp_path, {'x': 1})
    assert path.read_bytes() == b'corrupt'


def test_alternative_valid_zlib_stream_reused(tmp_path):
    raw = b'{"x":"' + b'ab' * 1000 + b'"}'
    ref = put_blob(tmp_path, raw)
    path = tmp_path / ref['_evidence_ref']['path']
    path.chmod(0o644)
    path.write_bytes(zlib.compress(raw, level=1))
    new = put_blob(tmp_path, raw)
    assert new['_evidence_ref']['sha256'] == ref['_evidence_ref']['sha256']
    assert b''.join(iter_bytes(tmp_path, new)) == raw


def test_path_and_marker_validation(tmp_path):
    assert not is_reference({'_evidence_ref': {}})
    ref = put_json(tmp_path, {'ok': True})
    assert is_reference(ref)
    ref['_evidence_ref']['path'] = '../outside'
    assert not is_reference(ref)
    with pytest.raises(EvidenceObjectError):
        verify(tmp_path, ref)


def test_symlink_store_rejected(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    directory = tmp_path / 'data/normalized'
    directory.mkdir(parents=True)
    (directory / 'evidence-objects').symlink_to(outside, target_is_directory=True)
    with pytest.raises(EvidenceObjectError, match='UNSAFE_PATH'):
        put_json(tmp_path, {'a': 1})
    assert not list(outside.iterdir())


def _fixture(root):
    path = root / 'data/state/ledger.sqlite3'
    path.parent.mkdir(parents=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    for table in ('orders', 'fills', 'daily_nav', 'plans', 'position_lots', 'account_state'):
        db.execute(f'CREATE TABLE {table}(id TEXT,account_id TEXT,data TEXT CHECK(json_valid(data)))')
    db.execute('CREATE TABLE operations(account_id TEXT,operation_id TEXT,payload_hash TEXT,'
               'result TEXT CHECK(json_valid(result)),processed_at TEXT)')
    db.execute('CREATE TABLE cash_entries(entry_id TEXT,account_id TEXT,amount_fen INTEGER)')
    db.execute("INSERT INTO cash_entries VALUES('initial','S1',5000000)")
    db.execute("INSERT INTO cash_entries VALUES('fill','S1',-10010)")
    evidence = {'financial': {'cash_fen': 5000000}, 'frozen_plan': {'large': ['row' * 100] * 100}}
    fill = {'fill_id': 'o1', 'order_id': 'o1', 'plan_id': 'p1', 'code': 'sh.600000',
            'side': 'BUY', 'quantity': 100, 'price': '1.00', 'amount_fen': 10000,
            'fees_fen': 10, 'net_cash_fen': -10010, 'cost_fen': 10010,
            'account_view_evidence': evidence, 'account_view_hash': 'old-view-hash',
            'lot_allocations': [], 'data_evidence': [{'observation_id': 'obs'}]}
    order = {'order_id': 'o1', 'plan_id': 'p1', 'code': 'sh.600000', 'side': 'BUY',
             'quantity': 100, 'limit_price': '1.00', 'status': 'FILLED', 'result_hash': 'old-result',
             'last_attempt': {'state': 'WAITING_DATA', 'account_view_evidence': evidence},
             'result': fill}
    records = {'orders': order, 'fills': fill,
               'daily_nav': {'session': '2026-11-02', 'net_assets_fen': 5000000,
                             'orders': [order], 'fills': [fill]},
               'plans': {'plan_id': 'p1', 'target_session': '2026-11-02',
                         'document': {'path': 'data/plans/frozen/old.json', 'content_hash': 'old'}},
               'position_lots': {'quantity': 100, 'cost_fen': 10010},
               'account_state': {'account_revision': 9, 'report_revision': 10}}
    for table, value in records.items():
        db.execute(f'INSERT INTO {table} VALUES(?,?,?)', ('o1', 'S1', canonical(value)))
    db.execute('INSERT INTO operations VALUES(?,?,?,?,?)',
               ('S1', 'op', 'old-payload-hash', canonical({'operation_id': 'op',
                  'statement': records['daily_nav']}), '2026-11-03T00:00:00+00:00'))
    db.execute('PRAGMA user_version=3')
    db.commit()
    return db, path


def test_sqlite_text_blob_archive_preserves_exact_original(tmp_path):
    db, _ = _fixture(tmp_path)
    original = db.execute('SELECT data FROM orders').fetchone()[0].encode()
    ref = archive_record(tmp_path, db, 'orders', 1)
    assert b''.join(iter_bytes(tmp_path, ref)) == original
    assert ref['_evidence_ref']['sha256'] == hashlib.sha256(original).hexdigest()
    db.close()


def test_dry_run_does_not_write_database_or_objects(tmp_path):
    db, path = _fixture(tmp_path)
    db.close()
    before = path.read_bytes()
    result = dry_run(tmp_path)
    assert result['schema_version'] == 3
    assert result['record_count'] == 4
    assert len(result['baseline']['fills']) == 1
    assert path.read_bytes() == before
    assert not (tmp_path / DIRECTORY).exists()


def test_isolated_migration_preserves_financial_values_old_hashes_and_shared_evidence(tmp_path):
    db, path = _fixture(tmp_path)
    old_order = db.execute('SELECT data FROM orders').fetchone()[0].encode()
    old_plan = db.execute('SELECT data FROM plans').fetchone()[0]
    before = capture_baseline(db)
    db.close()
    result = apply_isolated(tmp_path)
    assert result['state'] == 'COMPLETE'
    assert result['changed'] is True
    assert result['equivalence']['state'] == 'READY'
    assert result['before'] == before
    assert result['before']['financial_sha256'] == result['after']['financial_sha256']
    with sqlite3.connect(path) as check:
        assert check.execute('PRAGMA user_version').fetchone()[0] == 4
        order = json.loads(check.execute('SELECT data FROM orders').fetchone()[0])
        fill = json.loads(check.execute('SELECT data FROM fills').fetchone()[0])
        daily = json.loads(check.execute('SELECT data FROM daily_nav').fetchone()[0])
        operation = json.loads(check.execute('SELECT result FROM operations').fetchone()[0])
        assert check.execute('SELECT data FROM plans').fetchone()[0] == old_plan
        assert check.execute('SELECT payload_hash FROM operations').fetchone()[0] == 'old-payload-hash'
    ref = fill['account_view_evidence']
    assert is_reference(ref)
    assert order['result']['account_view_evidence'] == ref
    assert order['last_attempt']['account_view_evidence'] == ref
    assert daily['fills'][0]['account_view_evidence'] == ref
    assert operation['statement']['fills'][0]['account_view_evidence'] == ref
    assert order['result_hash'] == 'old-result'
    assert b''.join(iter_bytes(tmp_path, order['original_record_ref'])) == old_order
    assert len(canonical(order)) < len(old_order) // 2
    assert ref['_evidence_ref']['stored_bytes'] < ref['_evidence_ref']['bytes'] // 5
    again = apply_isolated(tmp_path)
    assert again['changed'] is False
    assert again['after'] == result['after']
    with sqlite3.connect(path) as check:
        saved = json.loads(check.execute('SELECT data FROM orders').fetchone()[0])
    assert saved['original_record_ref'] == order['original_record_ref']


def test_migration_rolls_back_on_unknown_large_data_evidence(tmp_path):
    db, path = _fixture(tmp_path)
    row = json.loads(db.execute('SELECT data FROM orders').fetchone()[0])
    row['result']['data_evidence'] = ['unknown-large-field' * 2000]
    db.execute('UPDATE orders SET data=?', (canonical(row),))
    db.commit()
    before = capture_baseline(db)
    original = db.execute('SELECT data FROM orders').fetchone()[0]
    db.close()
    with pytest.raises(StorageMigrationError, match='PROJECTION_TOO_LARGE'):
        apply_isolated(tmp_path, max_projected_bytes=20_000)
    with sqlite3.connect(path) as check:
        check.row_factory = sqlite3.Row
        assert check.execute('PRAGMA user_version').fetchone()[0] == 3
        assert check.execute('SELECT data FROM orders').fetchone()[0] == original
        assert capture_baseline(check) == before


def test_migration_row_limit_and_equivalence_fail_closed(tmp_path):
    db, _ = _fixture(tmp_path)
    baseline = capture_baseline(db)
    db.close()
    with pytest.raises(StorageMigrationError, match='ROW_TOO_LARGE'):
        apply_isolated(tmp_path, max_row_bytes=100)
    altered = {**baseline, 'financial_sha256': 'different'}
    with pytest.raises(StorageMigrationError, match='FINANCIAL_MISMATCH'):
        verify_financial_equivalence(baseline, altered)


def test_compaction_does_not_expand_or_accept_corrupt_reference(tmp_path):
    ref = put_json(tmp_path, {'x': 'proof'})
    record = {'account_view_evidence': ref}
    assert compact_record(tmp_path, record)['record']['account_view_evidence'] == ref
    path = tmp_path / ref['_evidence_ref']['path']
    path.chmod(0o644)
    path.write_bytes(b'bad')
    with pytest.raises(EvidenceObjectError):
        compact_record(tmp_path, record)


def test_production_apply_is_forbidden(monkeypatch, tmp_path):
    db, _ = _fixture(tmp_path)
    db.close()
    monkeypatch.setattr('china_stock_sim.ledger.storage_migration._PRODUCTION', tmp_path)
    with pytest.raises(StorageMigrationError, match='PRODUCTION_APPLY_FORBIDDEN'):
        apply_isolated(tmp_path)


def test_replaced_directory_link_is_rejected_on_read(tmp_path):
    ref = put_json(tmp_path, {'a': 'value'})
    path = tmp_path / ref['_evidence_ref']['path']
    parent = path.parent
    renamed = parent.with_name('relocated')
    os.rename(parent, renamed)
    parent.symlink_to(renamed, target_is_directory=True)
    with pytest.raises(EvidenceObjectError, match='UNSAFE_PATH'):
        verify(tmp_path, ref)


def test_nested_financial_field_tampering_rolls_back(monkeypatch, tmp_path):
    from china_stock_sim.ledger import storage_migration

    db, path = _fixture(tmp_path)
    old = db.execute('SELECT data FROM daily_nav').fetchone()[0]
    db.close()
    original_compact = storage_migration.compact_record

    def corrupt(root, record, **kwargs):
        result = original_compact(root, record, **kwargs)
        if 'fills' in result['record']:
            result['record']['fills'][0]['quantity'] = 9999
        return result

    monkeypatch.setattr(storage_migration, 'compact_record', corrupt)
    with pytest.raises(StorageMigrationError, match='PROJECTION_MISMATCH'):
        apply_isolated(tmp_path)
    with sqlite3.connect(path) as check:
        assert check.execute('SELECT data FROM daily_nav').fetchone()[0] == old
        assert check.execute('PRAGMA user_version').fetchone()[0] == 3


def test_original_reference_missing_on_completed_migration_is_rejected(tmp_path):
    db, path = _fixture(tmp_path)
    db.close()
    apply_isolated(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE orders SET data=json_remove(data,'$.original_record_ref')")
    with pytest.raises(StorageMigrationError, match='ORIGINAL_MISSING'):
        apply_isolated(tmp_path)


def test_nested_isolated_staging_is_allowed_but_hardlink_to_production_is_not(monkeypatch, tmp_path):
    db, source = _fixture(tmp_path)
    db.close()
    monkeypatch.setattr('china_stock_sim.ledger.storage_migration._PRODUCTION', tmp_path)
    staging = tmp_path / 'data/maintenance-staging'
    db, _ = _fixture(staging)
    db.close()
    assert apply_isolated(staging)['state'] == 'COMPLETE'
    alias_root = tmp_path / 'data/hardlink-staging'
    destination = alias_root / 'data/state/ledger.sqlite3'
    destination.parent.mkdir(parents=True)
    os.link(source, destination)
    with pytest.raises(StorageMigrationError, match='PRODUCTION_APPLY_FORBIDDEN'):
        apply_isolated(alias_root)


def test_progress_reports_record_boundaries_without_evidence_bodies(tmp_path):
    db, _ = _fixture(tmp_path)
    db.close()
    events = []
    apply_isolated(tmp_path, progress=events.append)
    assert len(events) == 12
    assert [e['event'] for e in events[:3]] == ['RECORD_START', 'RECORD_ARCHIVED', 'RECORD_COMPLETE']
    assert all('record' not in e and 'account_view_evidence' not in e for e in events)


def test_binary_objects_are_recoverable_but_not_claimed_as_json(tmp_path):
    original = b'\xff\x00<html>original</html>\n'
    reference = put_blob(tmp_path, original, encoding='bytes+zlib')
    assert reference['_evidence_ref']['path'].endswith('.bin.zlib')
    assert is_reference(reference)
    assert verify(tmp_path, reference)['state'] == 'READY'
    assert b''.join(iter_bytes(tmp_path, reference)) == original
    with pytest.raises(EvidenceObjectError, match='ENCODING_MISMATCH'):
        read_json(tmp_path, reference)
    assert put_blob(tmp_path, io.BytesIO(original), encoding='bytes+zlib') == reference


def test_malformed_nested_reference_is_not_wrapped_again(tmp_path):
    with pytest.raises(StorageMigrationError, match='REFERENCE_INVALID'):
        compact_record(tmp_path, {'account_view_evidence': {'_evidence_ref': {'bad': 'value'}}})
