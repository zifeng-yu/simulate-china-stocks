"""Retention acceptance operates on isolated logs and archives, never production data."""

import argparse
import gzip
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from test_runtime_identity import identity_root

from china_stock_sim.runtime import archive, cli, history, identity, retention

assert identity_root
DAY = '2026-01-01'
NOW = datetime.fromisoformat('2026-02-01T12:00:00+08:00')
INVOCATION = 'a' * 32


def snapshot(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file() and not p.is_symlink()}


def log(root, *, day=DAY, invocation=INVOCATION, state='COMPLETE', closed=True):
    common = {'schema_version': 1, 'invocation_id': invocation, 'task': 'homepage',
              'recorded_at': day + 'T12:00:00+08:00'}
    rows = [{**common, 'event': 'START', 'sequence': 1, 'pid': 123, 'process_birth': 'BIRTH',
             'identity': identity.capture_identity(root)}]
    if closed:
        rows.append({**common, 'event': 'END', 'sequence': 2, 'result_state': state,
                     'exit_code': 0 if state == 'COMPLETE' else 2})
    path = root / f'logs/runtime/{day}/{invocation}.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b''.join(identity.encode(row) for row in rows))
    return path


def package(root, monkeypatch, day=DAY):
    monkeypatch.setattr(archive, '_now', lambda: datetime.fromisoformat(day + 'T14:00:00+08:00'))
    result = archive.archive_runtime(root, date=day)
    assert result['state'] in {'COMPLETE', 'PARTIAL'}
    return result


def plan(root, **kwargs):
    return retention.plan_retention(root, now=NOW, reference_inventory_complete=True, **kwargs)


def apply(root, result, **kwargs):
    return retention.apply_retention(root, expected_plan=result['plan_id'], now=NOW,
                                    reference_inventory_complete=True, **kwargs)


def test_dry_run_needs_complete_protection_and_is_zero_write(identity_root, monkeypatch):
    root = identity_root
    log(root)
    package(root, monkeypatch)
    before = snapshot(root)
    assert retention.plan_retention(root, now=NOW)['reason'] == 'PROTECTION_INVENTORY_REQUIRED'
    assert retention.maintenance_due(root, now=NOW)['state'] == 'WAITING'
    result = plan(root)
    assert result['state'] == 'READY' and len(result['candidates']) == 1
    assert snapshot(root) == before


@pytest.mark.parametrize('age,expected', [(29, 0), (30, 1), (31, 1)])
def test_raw_thirty_day_boundary_requires_exact_verified_archive(identity_root, monkeypatch, age, expected):
    root = identity_root
    day = (NOW - timedelta(days=age)).date().isoformat()
    raw = log(root, day=day)
    assert not plan(root)['candidates']
    package(root, monkeypatch, day)
    result = plan(root)
    assert len(result['candidates']) == expected
    if expected:
        assert apply(root, result)['state'] == 'COMPLETE'
        assert not raw.exists()
        after = snapshot(root)
        assert apply(root, result)['state'] == 'UNCHANGED'
        assert snapshot(root) == after


@pytest.mark.parametrize('age,expected', [(179, 0), (180, 2), (181, 2)])
def test_archive_expiry_is_separate_and_keeps_identity_objects(identity_root, monkeypatch, age, expected):
    root = identity_root
    day = (NOW - timedelta(days=age)).date().isoformat()
    log(root, day=day)
    bundled = package(root, monkeypatch, day)
    first = plan(root)
    assert {c['kind'] for c in first['candidates']} == {'RAW_LOG'}
    assert apply(root, first)['state'] == 'COMPLETE'
    second = plan(root)
    assert len(second['candidates']) == expected
    before = {k: v for k, v in snapshot(root).items() if k.startswith('data/normalized/')}
    if expected:
        assert apply(root, second)['state'] == 'COMPLETE'
        assert not (root / bundled['archive_path']).exists()
    assert all(snapshot(root)[k] == v for k, v in before.items())


def test_financial_or_sealed_reference_protects_raw_and_archive(identity_root, monkeypatch):
    root = identity_root
    raw = log(root)
    package(root, monkeypatch)
    reference = {'path': str(raw.relative_to(root)), 'sha256': hashlib.sha256(raw.read_bytes()).hexdigest(),
                 'reason': 'SEALED_RECEIPT'}
    result = plan(root, protected_references=[reference])
    assert not result['candidates']
    assert len(result['protected']) == 2
    assert all('REFERENCED_EVIDENCE' in r['reasons'] for r in result['protected'])


@pytest.mark.parametrize('process', [{'state': 'ALIVE', 'birth': 'BIRTH'}, {'state': 'DEAD'},
                                    {'state': 'ALIVE', 'birth': 'REUSED'}, {'state': 'UNKNOWN'}])
def test_unclosed_alive_interrupted_or_unknown_logs_are_protected(identity_root, monkeypatch, process):
    root = identity_root
    log(root, closed=False)
    package(root, monkeypatch)
    result = plan(root, process_probe=lambda pid: process)
    assert not result['candidates']
    assert result['protected']


def test_failure_needs_explicit_hash_bound_resolution_not_later_success(identity_root, monkeypatch):
    root = identity_root
    log(root, state='FAILED')
    log(root, invocation='b' * 32)
    package(root, monkeypatch)
    before = plan(root)
    assert len(before['candidates']) == 1 and before['candidates'][0]['path'].endswith('b' * 32 + '.jsonl')
    proof = root / 'resolved.json'
    proof.write_text(json.dumps({'state': 'RESOLVED', 'invocation_id': INVOCATION}))
    resolution = {'path': 'resolved.json', 'sha256': hashlib.sha256(proof.read_bytes()).hexdigest(),
                  'invocation_id': INVOCATION}
    result = plan(root, resolved_failures=[resolution])
    assert len(result['candidates']) == 2
    assert apply(root, result, resolved_failures=[resolution])['state'] == 'COMPLETE'


def test_stale_plan_or_archive_damage_never_deletes(identity_root, monkeypatch):
    root = identity_root
    raw = log(root)
    bundled = package(root, monkeypatch)
    result = plan(root)
    raw.write_bytes(raw.read_bytes() + b'\n')
    assert apply(root, result)['state'] == 'REJECTED'
    assert raw.exists()
    (root / bundled['archive_path']).write_bytes(b'CORRUPT')
    assert plan(root)['state'] == 'WAITING'
    assert raw.exists()


def test_apply_resumes_durable_unlink_intent_after_interruption(identity_root, monkeypatch):
    root = identity_root
    raw = log(root)
    package(root, monkeypatch)
    result = plan(root)
    original = retention._atomic
    count = 0

    def interrupt(root, name, value):
        nonlocal count
        count += 1
        if count == 2:
            raise KeyboardInterrupt()
        return original(root, name, value)

    monkeypatch.setattr(retention, '_atomic', interrupt)
    with pytest.raises(KeyboardInterrupt):
        apply(root, result)
    assert not raw.exists()
    monkeypatch.setattr(retention, '_atomic', original)
    assert apply(root, result)['state'] == 'COMPLETE'


def test_partial_unlink_failure_reports_actual_progress_and_resumes(identity_root, monkeypatch):
    root = identity_root
    log(root)
    log(root, invocation='b' * 32)
    package(root, monkeypatch)
    result = plan(root)
    original = Path.unlink
    later = root / result['candidates'][1]['path']

    def fail(path, *args, **kwargs):
        if path == later:
            raise OSError('simulated unlink failure')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'unlink', fail)
    failed = apply(root, result)
    assert failed['state'] == 'WAITING' and failed['changed'] is True
    assert failed['deleted_count'] == 1 and failed['resume_same_plan'] is True
    monkeypatch.setattr(Path, 'unlink', original)
    assert apply(root, result)['state'] == 'COMPLETE'


def test_changed_protection_after_interruption_blocks_resume(identity_root, monkeypatch):
    root = identity_root
    raw = log(root)
    package(root, monkeypatch)
    result = plan(root)
    original = Path.unlink

    def interrupt(path, *args, **kwargs):
        if path == raw:
            raise KeyboardInterrupt()
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'unlink', interrupt)
    with pytest.raises(KeyboardInterrupt):
        apply(root, result)
    monkeypatch.setattr(Path, 'unlink', original)
    ref = {'path': str(raw.relative_to(root)), 'sha256': hashlib.sha256(raw.read_bytes()).hexdigest()}
    assert apply(root, result, protected_references=[ref])['state'] == 'REJECTED'
    assert raw.exists()


def test_daily_hook_is_bounded_to_one_successful_check(identity_root, monkeypatch):
    root = identity_root
    log(root)
    package(root, monkeypatch)
    assert retention.maintenance_due(root, now=NOW, reference_inventory_complete=True)['state'] == 'COMPLETE'
    monkeypatch.setattr(retention, 'plan_retention', lambda *a, **k: pytest.fail('repeated scan'))
    assert retention.maintenance_due(root, now=NOW, reference_inventory_complete=True)['state'] == 'SKIPPED'


def test_history_reports_archive_catalog_after_raw_removal(identity_root, monkeypatch):
    root = identity_root
    log(root)
    package(root, monkeypatch)
    assert apply(root, plan(root))['state'] == 'COMPLETE'
    before = snapshot(root)
    result = history.query_history(root, date=DAY, invocation_id=INVOCATION)
    assert result['records'] == []
    assert result['archives']['state'] == 'ARCHIVED_AVAILABLE'
    assert result['archives']['verification'] == 'NOT_RUN'
    assert snapshot(root) == before


def test_symlink_or_invalid_configuration_fails_closed(identity_root, tmp_path):
    root = identity_root
    directory = root / 'logs/runtime'
    directory.mkdir(parents=True)
    (directory / DAY).symlink_to(tmp_path, target_is_directory=True)
    assert plan(root)['state'] == 'WAITING'
    config = root / 'configs/runtime_retention.toml'
    config.write_text('raw_days = 180\narchive_days = 30\n')
    assert plan(root)['state'] == 'WAITING'


def test_stdio_rotation_preserves_bytes_and_retention_guards(identity_root):
    root = identity_root
    config = root / 'configs/runtime_retention.toml'
    config.write_text('stdout_max_bytes=1024\n')
    raw = root / 'logs/scheduler-launchd.stdout.log'
    raw.parent.mkdir(parents=True, exist_ok=True)
    contents = b'normal output\n' * 100
    raw.write_bytes(contents)
    assert retention.rotate_stdio(root)['state'] == 'WAITING'
    result = retention.rotate_stdio(root, stopped_tasks=['workflow'], now=NOW - timedelta(days=31))
    assert result['state'] == 'COMPLETE' and raw.read_bytes() == b''
    packed = root / result['rotated'][0]['archive']
    assert gzip.decompress(packed.read_bytes()) == contents
    assert apply(root, plan(root))['state'] == 'COMPLETE'
    assert packed.exists() and not packed.with_suffix('').exists()


def test_stdout_rotation_refuses_live_process_even_with_stopped_declaration(identity_root, monkeypatch):
    root = identity_root
    log(root, closed=False)
    (root / 'configs/runtime_retention.toml').write_text('stdout_max_bytes=1024\n')
    path = root / 'logs/dashboard-refresh.stdout.log'
    path.write_bytes(b'output\n' * 200)
    monkeypatch.setattr(retention, 'probe_process', lambda pid: {'state': 'ALIVE', 'birth': 'BIRTH'})
    result = retention.rotate_stdio(root, stopped_tasks=['homepage'], now=NOW)
    assert result['state'] == 'WAITING' and path.stat().st_size > 0


def test_retention_lock_and_probe_error_preserve_all_sources(identity_root, monkeypatch):
    root = identity_root
    raw = log(root)
    package(root, monkeypatch)
    result = plan(root)
    with retention._lock(root):
        assert apply(root, result)['state'] == 'BUSY'
    assert raw.exists()
    log(root, invocation='c' * 32, closed=False)
    result = plan(root, process_probe=lambda pid: (_ for _ in ()).throw(RuntimeError()))
    assert result['state'] == 'WAITING' and raw.exists()


def test_stderr_and_referenced_stdout_are_not_silently_discarded(identity_root):
    root = identity_root
    (root / 'configs/runtime_retention.toml').write_text('stdout_max_bytes=1024\n')
    path = root / 'logs/scheduler-launchd.stderr.log'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'UNRESOLVED FAILURE\n' * 100)
    retention.rotate_stdio(root, stopped_tasks=['workflow'], now=NOW - timedelta(days=181))
    result = plan(root)
    assert not result['candidates']
    assert result['protected'][0]['reasons'] == ['UNASSESSED_STDERR']


def test_cli_plan_and_apply_are_explicit_and_business_free(identity_root, monkeypatch):
    root = identity_root
    log(root)
    package(root, monkeypatch)
    original_now = retention._now
    monkeypatch.setattr(retention, '_now', lambda value=None: NOW if value is None else original_now(value))
    parser = argparse.ArgumentParser()
    cli.register_commands(parser.add_subparsers(dest='command', required=True))
    missing, code = cli.execute(parser.parse_args(['runtime-retention-plan']), root)
    assert code == 2 and missing['reason'] == 'PROTECTION_INVENTORY_REQUIRED'
    proof = root / 'protection.json'
    proof.write_text(json.dumps({'reference_inventory_complete': True, 'protected_references': []}))
    args = ['--protection-manifest', str(proof)]
    result, code = cli.execute(parser.parse_args(['runtime-retention-plan', *args]), root)
    assert code == 0
    result, code = cli.execute(parser.parse_args(['runtime-retention-apply', *args,
                                                '--expected-plan', result['plan_id']]), root)
    assert code == 0 and result['state'] == 'COMPLETE'
