"""Protection declarations are complete before optional runtime retention receives authority."""

import hashlib
import json
from pathlib import Path

import pytest
from test_runtime_recording import events, runtime_root

from china_stock_sim.common import audit_archive
from china_stock_sim.runtime import protection, recording

assert runtime_root

LOG_A = 'logs/runtime/2026-11-02/' + 'a' * 32 + '.jsonl'
LOG_B = 'logs/runtime/2026-11-02/' + 'b' * 32 + '.jsonl'


def put(root, relative, value):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value.encode() if isinstance(value, str) else value)
    return path


def evidence(root, raw):
    raw = raw.encode() if isinstance(raw, str) else raw
    relative = audit_archive.DIRECTORY + '/' + hashlib.sha256(raw).hexdigest() + '.evidence'
    put(root, relative, raw)
    return relative


def test_complete_inventory_includes_sealed_docs_and_original_evidence_and_reuses_content(tmp_path, monkeypatch):
    put(tmp_path, LOG_A, 'a')
    put(tmp_path, LOG_B, 'b')
    put(tmp_path, 'logs/sealed.json', json.dumps({'runtime_log': LOG_A}))
    put(tmp_path, 'docs/current.txt', 'Read [' + LOG_B + '].')
    original = evidence(tmp_path, 'Previous evidence: ' + LOG_A)
    first = protection.protection_inventory(tmp_path)
    assert first['state'] == 'READY' and first['reference_inventory_complete']
    assert {row['path'] for row in first['protected_references']} == {LOG_A, LOG_B}
    assert first['financial_database_scanned'] is False
    monkeypatch.setattr(protection, '_scan', lambda *a: pytest.fail('unchanged source content reread'))
    second = protection.protection_inventory(tmp_path)
    assert second['state'] == 'READY' and second['bytes_scanned'] == 0
    assert second['manifest_hash'] == first['manifest_hash']
    assert original in json.loads((tmp_path / protection.CACHE).read_bytes())['body']['sources']


def test_new_sealed_receipt_invalidates_complete_manifest(tmp_path):
    put(tmp_path, LOG_A, 'a')
    put(tmp_path, LOG_B, 'b')
    put(tmp_path, 'logs/first.json', json.dumps({'path': LOG_A}))
    first = protection.protection_inventory(tmp_path)
    put(tmp_path, 'logs/new.json', json.dumps({'path': LOG_B}))
    second = protection.protection_inventory(tmp_path)
    assert second['state'] == 'READY' and second['manifest_hash'] != first['manifest_hash']
    assert len(second['protected_references']) == 2


def test_budget_progress_never_claims_complete_until_all_sources_and_targets_checked(tmp_path):
    put(tmp_path, LOG_A, 'a')
    put(tmp_path, LOG_B, 'b')
    put(tmp_path, 'logs/a.json', json.dumps({'path': LOG_A, 'description': 'x' * 30}))
    put(tmp_path, 'logs/b.json', json.dumps({'path': LOG_B, 'description': 'y' * 30}))
    first = protection.protection_inventory(tmp_path, max_scan_bytes=150)
    assert first['state'] == 'WAITING' and not first['reference_inventory_complete']
    for _ in range(4):
        result = protection.protection_inventory(tmp_path, max_scan_bytes=150)
        if result['state'] == 'READY':
            break
    assert result['state'] == 'READY' and len(result['protected_references']) == 2


@pytest.mark.parametrize('damage', ['missing_source', 'missing_target', 'malformed_receipt', 'damaged_cache', 'symlink'])
def test_incomplete_protection_never_calls_retention(tmp_path, monkeypatch, damage):
    put(tmp_path, LOG_A, 'a')
    source = put(tmp_path, 'logs/sealed.json', json.dumps({'path': LOG_A}))
    assert protection.protection_inventory(tmp_path)['state'] == 'READY'
    if damage == 'missing_source':
        source.unlink()
    elif damage == 'missing_target':
        (tmp_path / LOG_A).unlink()
    elif damage == 'malformed_receipt':
        source.write_text('{broken')
    elif damage == 'damaged_cache':
        (tmp_path / protection.CACHE).write_text('{}')
    else:
        source.unlink()
        source.symlink_to(tmp_path / LOG_A)
    monkeypatch.setattr(protection, 'maintenance_due', lambda *a, **k: pytest.fail('unsafe deletion authority'))
    result = protection.maintain_runtime(tmp_path)
    assert result['state'] == 'WAITING' and not result['reference_inventory_complete']


def test_compressed_audit_original_is_scanned_and_changed_cas_dependency_invalidates(tmp_path):
    put(tmp_path, LOG_A, 'a')
    reference = evidence(tmp_path, ('prior ' + LOG_A + '\n') * 100)
    archived = audit_archive.archive_file(tmp_path, reference, apply=True)
    assert archived['state'] == 'READY' and archived['changed']
    first = protection.protection_inventory(tmp_path)
    assert first['state'] == 'READY' and first['protected_references'][0]['path'] == LOG_A
    layout = audit_archive.layout(tmp_path, reference)
    path = tmp_path / layout['object_ref']['_evidence_ref']['path']
    path.chmod(0o600)
    raw = bytearray(path.read_bytes())
    raw[-1] ^= 1
    path.write_bytes(raw)
    assert protection.protection_inventory(tmp_path)['state'] == 'WAITING'


def test_json_escaped_reference_is_protected(tmp_path):
    put(tmp_path, LOG_A, 'a')
    put(tmp_path, 'logs/sealed.json', json.dumps({'path': LOG_A}).replace('logs', r'lo\u0067s'))
    result = protection.protection_inventory(tmp_path)
    assert result['state'] == 'READY' and result['protected_references'][0]['path'] == LOG_A


def test_daily_hook_supplies_complete_current_declarations_without_touching_financial_files(tmp_path, monkeypatch):
    put(tmp_path, LOG_A, 'a')
    put(tmp_path, 'logs/sealed.json', json.dumps({'path': LOG_A}))
    financial = put(tmp_path, 'data/state/ledger.sqlite3', b'opaque financial bytes')
    calls = []

    def maintain(root, **kwargs):
        calls.append(kwargs)
        assert kwargs['reference_inventory_complete'] is True
        assert kwargs['protected_references'][0]['path'] == LOG_A
        return {'state': 'UNCHANGED', 'changed': False}

    monkeypatch.setattr(protection, 'maintenance_due', maintain)
    before = financial.read_bytes()
    original_read = Path.read_bytes

    def guarded_read(path):
        if path == financial:
            pytest.fail('protection scanner read the financial database')
        return original_read(path)

    monkeypatch.setattr(Path, 'read_bytes', guarded_read)
    result = protection.maintain_runtime(tmp_path, now='2026-11-03T12:00:00+08:00')
    assert result['state'] == 'UNCHANGED' and len(calls) == 1
    assert original_read(financial) == before


def test_sealed_zero_byte_stdout_is_preserved_and_cached_as_empty_artifact(tmp_path):
    empty = put(tmp_path, 'logs/interrupted-stdout.json', b'')
    empty_hash = hashlib.sha256(b'').hexdigest()
    put(tmp_path, 'logs/sealed-receipt.json', json.dumps({'original': {
        'path': 'logs/interrupted-stdout.json', 'sha256': empty_hash,
        'historical_json_exception': 'interrupted stdout retained unchanged'}}))
    first = protection.protection_inventory(tmp_path)
    assert first['state'] == 'READY' and first['protected_references'] == []
    cached = json.loads((tmp_path / protection.CACHE).read_bytes())['body']['sources']['logs/interrupted-stdout.json']
    assert cached['source_type'] == 'EMPTY_ARTIFACT' and cached['bytes'] == 0
    assert cached['sha256'] == empty_hash and cached['references'] == []
    assert empty.read_bytes() == b''
    assert protection.protection_inventory(tmp_path)['bytes_scanned'] == 0
    empty.write_text('{not valid JSON')
    failed = protection.protection_inventory(tmp_path)
    assert failed['state'] == 'WAITING' and not failed['reference_inventory_complete']


@pytest.mark.parametrize('content', [b' ', b'{broken', b'null trailing', b'\xff'])
def test_nonempty_invalid_json_is_never_treated_as_empty(tmp_path, content):
    put(tmp_path, 'logs/nonempty.json', content)
    result = protection.protection_inventory(tmp_path)
    assert result['state'] == 'WAITING' and not result['reference_inventory_complete']


def test_zero_byte_source_growing_during_scan_cannot_authorize_retention(tmp_path, monkeypatch):
    path = put(tmp_path, 'logs/in-progress.json', b'')
    original = protection._identity
    mutated = []

    def identity(root, relative, previous=None):
        if relative == 'logs/in-progress.json' and previous is not None and not mutated:
            mutated.append(True)
            path.write_text(json.dumps({'new_reference': LOG_A}))
        return original(root, relative, previous)

    monkeypatch.setattr(protection, '_identity', identity)
    monkeypatch.setattr(protection, 'maintenance_due', lambda *a, **k: pytest.fail('moving source authorized retention'))
    result = protection.maintain_runtime(tmp_path)
    assert mutated and result['state'] == 'WAITING' and not result['reference_inventory_complete']


def test_protection_failure_records_safe_stack_only_with_active_runtime_context(runtime_root):
    put(runtime_root, 'logs/bad-receipt.json', '{PRIVATE_UNSAFE_CONTENT')
    result = protection.protection_inventory(runtime_root)
    assert result['state'] == 'WAITING' and not (runtime_root / 'logs/runtime').exists()
    observed = recording.run_observed(runtime_root, 'homepage', lambda: (
        protection.protection_inventory(runtime_root), 2))
    assert observed[0]['state'] == 'WAITING'
    rows = events(runtime_root)
    errors = [row for row in rows if row.get('kind') == 'exception']
    assert len(errors) == 1 and errors[0]['exception']['chain'][0]['type'] == 'JSONDecodeError'
    assert 'PRIVATE_UNSAFE_CONTENT' not in json.dumps(rows)


def test_explicit_node_budget_completes_large_json_and_default_reuses_it(tmp_path, monkeypatch):
    source = put(tmp_path, 'logs/large.json', '[' + ','.join(['0'] * 1_000_000) + ']')
    before = source.read_bytes()
    failed = protection.protection_inventory(tmp_path)
    assert failed == {'state': 'WAITING', 'reason': 'PROTECTION_JSON_TOO_COMPLEX',
                      'source': 'logs/large.json', 'reference_inventory_complete': False}
    complete = protection.protection_inventory(tmp_path, max_json_nodes=2_000_000)
    assert complete['state'] == 'READY'
    cache = json.loads((tmp_path / protection.CACHE).read_bytes())['body']
    assert cache['sources']['logs/large.json']['json_nodes'] == 1_000_001
    monkeypatch.setattr(protection, '_scan', lambda *a: pytest.fail('verified large content reread'))
    assert protection.protection_inventory(tmp_path)['bytes_scanned'] == 0
    assert source.read_bytes() == before


@pytest.mark.parametrize('budget', [0, -1, True, 1.5, 10_000_001])
def test_node_budget_is_explicit_positive_integer_with_ten_million_hard_cap(tmp_path, budget):
    result = protection.protection_inventory(tmp_path, max_json_nodes=budget)
    assert result['state'] == 'WAITING' and not result['reference_inventory_complete']
    assert not (tmp_path / 'data').exists()


def test_completed_archive_scan_is_checkpointed_when_later_json_fails(tmp_path, monkeypatch):
    put(tmp_path, LOG_A, 'runtime evidence')
    reference = evidence(tmp_path, ('prior ' + LOG_A + '\n') * 100)
    audit_archive.archive_file(tmp_path, reference, apply=True)
    bad = put(tmp_path, 'logs/later.json', '{bad')
    scanned = []
    original = protection._scan

    def scan(*args):
        scanned.append(args[1])
        return original(*args)

    monkeypatch.setattr(protection, '_scan', scan)
    assert protection.protection_inventory(tmp_path)['state'] == 'WAITING'
    cache = json.loads((tmp_path / protection.CACHE).read_bytes())['body']
    assert reference in cache['sources'] and 'logs/later.json' not in cache['sources']
    assert 'manifest_hash' not in cache
    bad.write_text(json.dumps({'reference': LOG_A}))
    assert protection.protection_inventory(tmp_path)['state'] == 'READY'
    assert scanned.count(reference) == 1
    assert scanned.count('logs/later.json') == 2


def test_completed_targets_and_sources_survive_later_missing_target(tmp_path, monkeypatch):
    put(tmp_path, LOG_A, 'a')
    put(tmp_path, 'logs/sealed.json', json.dumps({'references': [LOG_A, LOG_B]}))
    assert protection.protection_inventory(tmp_path)['state'] == 'WAITING'
    cache = json.loads((tmp_path / protection.CACHE).read_bytes())['body']
    assert LOG_A in cache['targets'] and LOG_B not in cache['targets']
    assert 'manifest_hash' not in cache
    put(tmp_path, LOG_B, 'b')
    original_open = Path.open

    def guard(path, *args, **kwargs):
        if path == tmp_path / LOG_A:
            pytest.fail('previously verified target reopened')
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', guard)
    monkeypatch.setattr(protection, '_scan', lambda *a: pytest.fail('completed source rescanned'))
    result = protection.protection_inventory(tmp_path)
    assert result['state'] == 'READY' and result['bytes_scanned'] == 1


def test_partial_checkpoint_never_reuses_a_source_changed_after_its_scan(tmp_path, monkeypatch):
    first = put(tmp_path, 'logs/a.json', '{}')
    second = put(tmp_path, 'logs/b.json', '{}')
    original = protection._scan
    changed = []

    def scan(*args):
        if args[1] == 'logs/b.json' and not changed:
            changed.append(True)
            first.write_text('{bad')
            second.write_text('{also bad')
        return original(*args)

    monkeypatch.setattr(protection, '_scan', scan)
    assert protection.protection_inventory(tmp_path)['state'] == 'WAITING'
    cache = json.loads((tmp_path / protection.CACHE).read_bytes())['body']
    assert 'logs/a.json' in cache['sources'] and 'manifest_hash' not in cache
    second.write_text('{}')
    assert protection.protection_inventory(tmp_path)['state'] == 'WAITING'
    first.write_text('{}')
    assert protection.protection_inventory(tmp_path)['state'] == 'READY'


def test_failed_refresh_clears_previous_complete_manifest_marker(tmp_path):
    put(tmp_path, 'logs/a.json', '{}')
    assert protection.protection_inventory(tmp_path)['state'] == 'READY'
    put(tmp_path, 'logs/b.json', '{bad')
    assert protection.protection_inventory(tmp_path)['state'] == 'WAITING'
    cache = json.loads((tmp_path / protection.CACHE).read_bytes())['body']
    assert 'manifest_hash' not in cache
    assert 'logs/a.json' in cache['sources'] and 'logs/b.json' not in cache['sources']
