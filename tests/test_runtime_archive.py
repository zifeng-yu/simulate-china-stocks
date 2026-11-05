"""Runtime archive acceptance uses only isolated source/log fixtures, never business data."""

import gzip
import io
import json
import tarfile
from datetime import datetime

import pytest
from test_runtime_identity import identity_root

from china_stock_sim.runtime import archive, identity

assert identity_root
TODAY = datetime.fromisoformat('2026-11-02T15:00:00+08:00')
DAY = '2026-11-01'
INVOCATION = 'a' * 32


@pytest.fixture
def archive_root(identity_root, monkeypatch):
    monkeypatch.setattr(archive, '_now', lambda: TODAY)
    root = identity_root
    write_log(root)
    (root / 'data/state').mkdir()
    (root / 'data/state/ledger.sqlite3').write_bytes(b'NEVER_ARCHIVE_LEDGER')
    (root / 'data/state/market.sqlite3').write_bytes(b'NEVER_ARCHIVE_MARKET')
    return root


def event(kind, invocation=INVOCATION, **extra):
    return {'schema_version': 1, 'event': kind, 'invocation_id': invocation,
            'task': 'homepage', 'recorded_at': DAY + 'T23:59:00+08:00', **extra}


def write_log(root, day=DAY, invocation=INVOCATION, *, closed=True, captured=None):
    captured = identity.capture_identity(root) if captured is None else captured
    path = root / 'logs/runtime' / day / (invocation + '.jsonl')
    path.parent.mkdir(parents=True, exist_ok=True)
    contents = identity.encode(event('START', invocation, identity=captured))
    if closed:
        contents += identity.encode(event('END', invocation, exit_code=0, result_state='READY'))
    path.write_bytes(contents)
    return path


def snapshot(root):
    return {str(p.relative_to(root)): (identity.digest(p.read_bytes()), p.stat().st_mtime_ns)
            for p in root.rglob('*') if p.is_file() and not p.is_symlink()}


def rewrite_package(root, result, transform):
    """Recompute outer digest so malicious member tests exercise inner validation."""
    path = root / result['archive_path']
    members = []
    with tarfile.open(path, 'r:gz') as reader:
        for member in reader:
            members.append((member, reader.extractfile(member).read()))
    members = transform(members)
    with tarfile.open(path, 'w:gz', format=tarfile.USTAR_FORMAT) as writer:
        for member, body in members:
            writer.addfile(member, io.BytesIO(body) if member.isfile() else None)
    receipt_path = root / result['manifest_path']
    receipt = json.loads(receipt_path.read_bytes())
    receipt.update(archive_sha256=identity.digest(path.read_bytes()), archive_size=path.stat().st_size)
    receipt_path.write_bytes(identity.encode(receipt))


def test_archive_is_self_contained_bounded_and_verify_is_read_only(archive_root):
    root = archive_root
    before = snapshot(root)
    result = archive.archive_runtime(root, date=DAY)
    assert result['state'] == 'COMPLETE' and result['changed']
    assert result['log_count'] == 1 and result['unclosed_count'] == 0
    assert all(snapshot(root)[key] == value for key, value in before.items())
    receipt = json.loads((root / result['manifest_path']).read_bytes())
    assert receipt['archive_sha256'] == identity.digest((root / result['archive_path']).read_bytes())
    names = [row['path'] for row in receipt['manifest']['files']]
    assert not any('ledger.sqlite3' in name or 'market.sqlite3' in name for name in names)
    assert any('/identities/' in name for name in names) and any('/objects/' in name for name in names)
    after = snapshot(root)
    verified = archive.verify_archive(root, result['archive_path'])
    assert verified['state'] == 'READY' and verified['read_only']
    assert snapshot(root) == after
    # Verification depends on archived originals, not today's source/config/log files.
    for name in names:
        (root / name).unlink()
    (root / 'src/package/main.py').write_text('CHANGED = True\n')
    assert archive.verify_archive(root, result['archive_path'])['state'] == 'READY'


def test_identical_content_is_idempotent_and_old_tar_is_never_rewritten(archive_root):
    root = archive_root
    first = archive.archive_runtime(root, date=DAY)
    before = snapshot(root)
    again = archive.archive_runtime(root, date=DAY)
    assert again == {**first, 'changed': False}
    assert snapshot(root) == before


def test_cross_midnight_end_creates_new_fingerprint_and_preserves_sample_boundary(archive_root):
    root = archive_root
    path = write_log(root, closed=False)
    first = archive.archive_runtime(root, date=DAY)
    assert first['state'] == 'PARTIAL' and first['unclosed_count'] == 1
    previous = (root / first['archive_path']).read_bytes()
    with path.open('ab') as stream:
        stream.write(identity.encode(event('END', exit_code=0, result_state='COMPLETE',
                                          recorded_at='2026-11-02T00:02:00+08:00')))
    following = archive.archive_due(root)
    assert following['state'] == 'COMPLETE' and following['date'] == DAY
    assert following['fingerprint'] != first['fingerprint']
    assert (root / first['archive_path']).read_bytes() == previous
    assert archive.verify_archive(root, first['archive_path'])['state'] == 'PARTIAL'
    assert archive.verify_archive(root, following['archive_path'])['state'] == 'READY'


@pytest.mark.parametrize('tail', [b'{"event":', b'not-json\n', b'[]\n', b'\xff\n'])
def test_malformed_or_truncated_records_are_preserved_with_explicit_gaps(archive_root, tail):
    root = archive_root
    path = write_log(root, closed=False)
    raw = path.read_bytes() + tail
    path.write_bytes(raw)
    result = archive.archive_runtime(root, date=DAY)
    assert result['state'] == 'PARTIAL' and result['issue_count'] > 0
    with tarfile.open(root / result['archive_path']) as saved:
        assert saved.extractfile(str(path.relative_to(root))).read() == raw
    assert path.read_bytes() == raw
    assert archive.verify_archive(root, result['archive_path'])['state'] == 'PARTIAL'


@pytest.mark.parametrize('missing', ['manifest', 'object', 'unavailable', 'unsafe_reference'])
def test_identity_gaps_are_visible_without_reading_unrelated_data(archive_root, missing):
    root = archive_root
    captured = identity.capture_identity(root)
    write_log(root, captured=captured)
    if missing == 'manifest':
        (root / captured['manifest_path']).unlink()
    elif missing == 'object':
        body = json.loads((root / captured['manifest_path']).read_bytes())
        (root / body['source_files'][0]['object_path']).unlink()
    else:
        captured = {'state': 'UNAVAILABLE', 'reason': 'IDENTITY_CAPTURE_FAILED'} if missing == 'unavailable' else {
            'state': 'READY', 'identity_id': 'x', 'manifest_path': 'data/state/ledger.sqlite3'}
        write_log(root, captured=captured)
    result = archive.archive_runtime(root, date=DAY)
    assert result['state'] == 'PARTIAL' and result['issue_count']
    assert archive.verify_archive(root, result['archive_path'])['state'] == 'PARTIAL'
    assert (root / 'data/state/ledger.sqlite3').read_bytes() == b'NEVER_ARCHIVE_LEDGER'


def test_new_log_on_previously_archived_date_is_not_permanently_skipped(archive_root):
    first = archive.archive_due(archive_root)
    write_log(archive_root, invocation='b' * 32)
    second = archive.archive_due(archive_root)
    assert first['state'] == second['state'] == 'COMPLETE'
    assert second['log_count'] == 2 and second['fingerprint'] != first['fingerprint']


def test_automatic_archive_handles_one_prior_date_and_ignores_today(archive_root):
    root = archive_root
    write_log(root, '2026-10-30', 'b' * 32)
    write_log(root, '2026-11-02', 'c' * 32, closed=False)
    first = archive.archive_due(root)
    assert first['state'] == 'COMPLETE' and first['date'] == '2026-10-30'
    assert len(list((root / archive.BASE).glob('*/*.tar.gz'))) == 1
    second = archive.archive_due(root)
    assert second['state'] == 'COMPLETE' and second['date'] == DAY
    assert archive.archive_due(root)['state'] == 'SKIPPED'
    assert len(list((root / archive.BASE).glob('*/*.tar.gz'))) == 2
    manual = archive.archive_runtime(root, date='2026-11-02')
    assert manual['state'] == 'PARTIAL' and manual['unclosed_count'] == 1


def test_automatic_budget_is_visible_and_does_not_change_sources(archive_root, monkeypatch):
    before = snapshot(archive_root)
    monkeypatch.setattr(archive, 'AUTO_TOTAL_BYTES', 1)
    result = archive.archive_due(archive_root)
    assert result == {'state': 'WAITING_MANUAL', 'reason': 'ARCHIVE_BUDGET_EXCEEDED', 'changed': False}
    assert not list((archive_root / archive.BASE).glob('*/*.tar.gz'))
    assert all(snapshot(archive_root)[key] == value for key, value in before.items())


def test_concurrent_archive_is_nonblocking(archive_root):
    with archive._lock(archive_root):
        assert archive.archive_due(archive_root)['state'] == 'BUSY'
        assert archive.archive_runtime(archive_root, date=DAY)['state'] == 'BUSY'


def test_failed_receipt_commit_can_recover_verified_orphan_without_replacing_it(archive_root, monkeypatch):
    real_save = archive.save_immutable

    def fail(*args, **kwargs):
        raise OSError('private disk details must not escape')

    monkeypatch.setattr(archive, 'save_immutable', fail)
    failed = archive.archive_runtime(archive_root, date=DAY)
    assert failed == {'state': 'FAILED', 'reason': 'ARCHIVE_FAILED', 'changed': False}
    tar = next((archive_root / archive.BASE).glob('*/*.tar.gz'))
    raw, stamp = tar.read_bytes(), tar.stat().st_mtime_ns
    monkeypatch.setattr(archive, 'save_immutable', real_save)
    recovered = archive.archive_due(archive_root)
    assert recovered['state'] == 'COMPLETE'
    assert tar.read_bytes() == raw and tar.stat().st_mtime_ns == stamp
    assert archive.verify_archive(archive_root, recovered['archive_path'])['state'] == 'READY'


@pytest.mark.parametrize('attack', ['traversal', 'absolute', 'duplicate', 'symlink', 'hardlink',
                                  'foreign_path', 'directory'])
def test_archive_verify_rejects_unsafe_members_even_with_updated_outer_checksum(archive_root, attack):
    result = archive.archive_runtime(archive_root, date=DAY)

    def mutate(members):
        if attack == 'duplicate':
            return [*members, members[-1]]
        name = {'traversal': '../outside', 'absolute': '/tmp/outside',
                'foreign_path': 'data/state/ledger.sqlite3'}.get(attack, 'linked')
        member = tarfile.TarInfo(name)
        if attack in {'symlink', 'hardlink'}:
            member.type = tarfile.SYMTYPE if attack == 'symlink' else tarfile.LNKTYPE
            member.linkname = '../private'
        elif attack == 'directory':
            member.type = tarfile.DIRTYPE
        return [*members, (member, b'')]

    rewrite_package(archive_root, result, mutate)
    before = snapshot(archive_root)
    assert archive.verify_archive(archive_root, result['archive_path'])['state'] == 'INTEGRITY_ERROR'
    assert snapshot(archive_root) == before


@pytest.mark.parametrize('attack', ['missing_member', 'wrong_member_hash', 'truncated_gzip', 'bad_receipt'])
def test_archive_verify_detects_missing_or_corrupt_bytes(archive_root, attack):
    result = archive.archive_runtime(archive_root, date=DAY)
    if attack == 'missing_member':
        rewrite_package(archive_root, result, lambda members: members[:-1])
    elif attack == 'wrong_member_hash':
        def corrupt(members):
            member, raw = members[-1]
            return [*members[:-1], (member, b'X' * len(raw))]
        rewrite_package(archive_root, result, corrupt)
    elif attack == 'truncated_gzip':
        path = archive_root / result['archive_path']
        path.write_bytes(path.read_bytes()[:-5])
    else:
        (archive_root / result['manifest_path']).write_text('[]')
    assert archive.verify_archive(archive_root, result['archive_path'])['state'] == 'INTEGRITY_ERROR'


def test_verify_limits_inflated_padding_and_does_not_extract(archive_root, monkeypatch):
    result = archive.archive_runtime(archive_root, date=DAY)
    path = archive_root / result['archive_path']
    raw = gzip.decompress(path.read_bytes()) + b'\0' * 100_000
    path.write_bytes(gzip.compress(raw))
    monkeypatch.setattr(archive, 'MAX_TOTAL_BYTES', 20_000)
    monkeypatch.setattr(archive, 'MAX_FILES', 20)
    assert archive.verify_archive(archive_root, result['archive_path'])['state'] == 'INTEGRITY_ERROR'


@pytest.mark.parametrize('where', ['logs', 'object', 'backup'])
def test_archive_refuses_symlinks_without_writing_the_target(archive_root, tmp_path, where):
    outside = tmp_path / 'outside'
    outside.mkdir()
    if where == 'logs':
        (archive_root / 'logs/runtime' / DAY / ('d' * 32 + '.jsonl')).symlink_to(outside)
    elif where == 'backup':
        (archive_root / 'data/backups').mkdir()
        (archive_root / archive.BASE).symlink_to(outside, target_is_directory=True)
    else:
        obj = next((archive_root / identity.EVIDENCE / 'objects').iterdir())
        obj.unlink()
        obj.symlink_to(outside)
    assert archive.archive_runtime(archive_root, date=DAY)['state'] == 'FAILED'
    assert not list(outside.iterdir())


def test_automatic_skip_checks_compressed_bytes_and_bad_receipt_safely(archive_root):
    result = archive.archive_runtime(archive_root, date=DAY)
    path = archive_root / result['archive_path']
    raw = path.read_bytes()
    path.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
    assert archive.archive_due(archive_root)['state'] == 'FAILED'
    path.write_bytes(raw)
    (archive_root / result['manifest_path']).write_text('null')
    assert archive.archive_due(archive_root)['state'] == 'FAILED'


def test_many_archived_days_do_not_starve_new_dates_or_later_historical_updates(archive_root, monkeypatch):
    root = archive_root
    days = [f'2026-10-{day}' for day in range(20, 27)] + [DAY]
    for day in days:
        path = write_log(root, day)
        data = path.read_bytes().splitlines(keepends=True)
        path.write_bytes(data[0] + identity.encode(event('EVENT', detail='x' * 3000)) + data[1])
        assert archive.archive_runtime(root, date=day)['state'] == 'COMPLETE'
    new_day = '2026-10-29'
    write_log(root, new_day)
    monkeypatch.setattr(archive, 'AUTO_TOTAL_BYTES', 12000)
    real = archive._snapshot
    visited = []

    def capture(root, day, budget):
        visited.append(day)
        return real(root, day, budget)

    monkeypatch.setattr(archive, '_snapshot', capture)
    newest = archive.archive_due(root)
    assert newest['state'] == 'COMPLETE' and newest['checked_date'] == new_day
    assert visited == [new_day]  # No historical evidence/decompression scan first.
    write_log(root, days[0], 'e' * 32)
    checked = []
    for _ in range(len(days) + 1):
        visited.clear()
        result = archive.archive_due(root)
        assert result['state'] in {'SKIPPED', 'COMPLETE'}
        assert len(visited) == 1
        checked.append(result['checked_date'])
        if result['checked_date'] == days[0]:
            assert result['state'] == 'COMPLETE' and result['log_count'] == 2
            break
    assert days[0] in checked
    state = json.loads((root / archive.BASE / 'auto-state.json').read_bytes())
    assert state['last_checked_date'] == days[0]


@pytest.mark.parametrize('state', ['not-json', 'null', '{"schema_version":99,"last_checked_date":null}',
                                 '{"schema_version":1,"last_checked_date":"../outside"}'])
def test_damaged_automatic_cursor_is_not_reset_or_overwritten(archive_root, state):
    path = archive_root / archive.BASE / 'auto-state.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(state)
    result = archive.archive_due(archive_root)
    assert result['state'] == 'FAILED' and result['reason'] == 'AUTO_STATE_INVALID'
    assert path.read_text() == state


@pytest.mark.parametrize('date', ['../2026-11-01', '2026-02-30', '2026-11-03', '', None])
def test_invalid_or_future_dates_do_not_create_archive_area(identity_root, monkeypatch, date):
    monkeypatch.setattr(archive, '_now', lambda: TODAY)
    assert archive.archive_runtime(identity_root, date=date)['state'] == 'FAILED'
    assert not (identity_root / archive.BASE).exists()


def test_verify_cannot_read_outside_archive_area_or_create_a_missing_root(identity_root):
    root = identity_root / 'not-created'
    assert archive.verify_archive(root, '../private.tar.gz')['state'] == 'INTEGRITY_ERROR'
    assert not root.exists()
