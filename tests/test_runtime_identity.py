"""Isolated source identity capture never resolves environment credentials."""

import json
import os
import tarfile
from datetime import datetime
from pathlib import Path

import pytest

from china_stock_sim.runtime import identity


@pytest.fixture
def identity_root(tmp_path):
    root = tmp_path / 'project'
    (root / 'src/package').mkdir(parents=True)
    (root / 'src/package/main.py').write_text('VALUE = 1\n')
    (root / 'pyproject.toml').write_text('[project]\nname="fixture"\n')
    (root / 'uv.lock').write_text('version = 1\n')
    (root / 'configs').mkdir()
    (root / 'configs/default.toml').write_text('enabled=true\n[fees]\nrate="0.003"\n')
    return root


def manifest(root, result):
    raw = (root / result['manifest_path']).read_bytes()
    return identity.validate_identity(raw, result['identity_id'])


def test_identity_is_stable_and_preserves_old_bytes_when_source_changes(identity_root):
    root = identity_root
    first = identity.capture_identity(root)
    assert identity.capture_identity(root) == first
    recorded = manifest(root, first)
    assert first['source_file_count'] == 3 and first['configuration_file_count'] == 1
    source = next(row for row in recorded['source_files'] if row['path'].endswith('main.py'))
    assert (root / source['object_path']).read_bytes() == b'VALUE = 1\n'
    (root / source['path']).write_text('VALUE = 2\n')
    later = identity.capture_identity(root)
    assert later['identity_id'] != first['identity_id']
    assert manifest(root, first) == recorded
    assert (root / source['object_path']).read_bytes() == b'VALUE = 1\n'
    assert all(p.suffix == '.evidence' for p in (root / identity.EVIDENCE / 'objects').iterdir())
    assert all(p.stat().st_mode & 0o777 == 0o600
               for p in (root / identity.EVIDENCE / 'objects').iterdir())


def test_configuration_is_public_semantics_not_raw_bytes_or_environment(identity_root, monkeypatch):
    root = identity_root
    (root / '.env').write_text('API_KEY=SECRET_FROM_ENV')
    (root / 'private.pem').write_text('PRIVATE_KEY_NOT_READ')
    (root / 'configs/models.toml').write_text('''[model]
name = "public-model"
max_output_tokens = 2048
access_token = "ACCESS_TOKEN_NOT_ARCHIVED"
api_key = "CONFIG_KEY_NOT_ARCHIVED"
base_url = "https://private.endpoint.invalid"
model_env = "MODEL_ENV_NAME"
notes = ["password=INLINE_PASSWORD", "Bearer INLINE_CREDENTIAL", "ordinary note"]
[model.nested]
secret = "NESTED_SECRET"
location = "/Users/private/location"
''')
    touched = []
    real_read = identity.read_bytes

    def read(path, *args, **kwargs):
        touched.append(str(path))
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(identity, 'read_bytes', read)
    monkeypatch.setattr(os, 'getenv', lambda *args, **kwargs: pytest.fail('must not read environment'))
    captured = identity.capture_identity(root)
    row = next(x for x in manifest(root, captured)['configuration_files']
               if x['path'] == 'configs/models.toml')
    public = json.loads((root / row['object_path']).read_bytes())
    assert public['model']['name'] == 'public-model'
    assert public['model']['max_output_tokens'] == 2048
    assert public['model']['model_env'] == '[ENVIRONMENT_NOT_READ]'
    assert public['model']['notes'] == ['[REDACTED]', '[REDACTED]', 'ordinary note']
    archive_bytes = b''.join(p.read_bytes() for p in (root / identity.EVIDENCE).rglob('*') if p.is_file())
    for secret in [b'CONFIG_KEY_NOT_ARCHIVED', b'private.endpoint.invalid', b'SECRET_FROM_ENV',
                   b'PRIVATE_KEY_NOT_READ', b'MODEL_ENV_NAME', b'INLINE_PASSWORD',
                   b'INLINE_CREDENTIAL', b'NESTED_SECRET', b'/Users/private', b'ACCESS_TOKEN_NOT_ARCHIVED']:
        assert secret not in archive_bytes
    assert not any(path.endswith(('.env', '.pem')) for path in touched)
    assert row['sha256'] != identity.digest((root / 'configs/models.toml').read_bytes())


def test_private_changes_do_not_change_identity_but_public_changes_do(identity_root):
    root = identity_root
    path = root / 'configs/private.toml'
    path.write_text('api_key="first-private-value"\ntimeout_seconds=12\n')
    first = identity.capture_identity(root)
    path.write_text('api_key="second-private-value"\ntimeout_seconds=12\n')
    assert identity.capture_identity(root) == first
    path.write_text('api_key="second-private-value"\ntimeout_seconds=13\n')
    assert identity.capture_identity(root)['identity_id'] != first['identity_id']


@pytest.mark.parametrize('key', ['max_output_tokens', 'max_tokens'])
def test_public_token_budget_changes_identity_while_credentials_remain_redacted(identity_root, key):
    root = identity_root
    path = root / 'configs/models.toml'
    path.write_text(f'{key}=2048\ntoken="FIRST_PRIVATE"\naccess_token="SECOND_PRIVATE"\n')
    first = identity.capture_identity(root)
    row = next(r for r in manifest(root, first)['configuration_files'] if r['path'] == 'configs/models.toml')
    public = json.loads((root / row['object_path']).read_bytes())
    assert public == {key: 2048, 'token': '[REDACTED]', 'access_token': '[REDACTED]'}
    path.write_text(f'{key}=4096\ntoken="FIRST_PRIVATE"\naccess_token="SECOND_PRIVATE"\n')
    second = identity.capture_identity(root)
    assert second['identity_id'] != first['identity_id']
    path.write_text(f'{key}=4096\ntoken="CHANGED_PRIVATE"\naccess_token="CHANGED_OTHER"\n')
    assert identity.capture_identity(root) == second
    objects = b''.join(p.read_bytes() for p in (root / identity.EVIDENCE / 'objects').iterdir())
    for secret in [b'FIRST_PRIVATE', b'SECOND_PRIVATE', b'CHANGED_PRIVATE', b'CHANGED_OTHER']:
        assert secret not in objects


@pytest.mark.parametrize('key', ['max_output_tokens', 'max_tokens'])
@pytest.mark.parametrize('value', ['"UNMARKED_PRIVATE_CREDENTIAL"', '"2048"', 'true',
                                 '2048.0', '0', '-1', '8193',
                                 '["UNMARKED_PRIVATE_CREDENTIAL"]',
                                 '{value="UNMARKED_PRIVATE_CREDENTIAL"}'])
def test_token_budget_name_cannot_disguise_noninteger_or_unbounded_values(identity_root, key, value):
    root = identity_root
    (root / 'configs/models.toml').write_text(f'{key}={value}\n')
    result = identity.capture_identity(root)
    row = next(r for r in manifest(root, result)['configuration_files'] if r['path'] == 'configs/models.toml')
    assert json.loads((root / row['object_path']).read_bytes()) == {key: '[REDACTED]'}
    assert not any(b'UNMARKED_PRIVATE_CREDENTIAL' in p.read_bytes()
                   for p in (root / identity.EVIDENCE / 'objects').iterdir())


def test_token_budget_allowlist_is_exact_and_inclusive_of_supported_bounds():
    assert identity._public({'max_output_tokens': 1, 'max_tokens': 8192,
                             'other_tokens': 2048, 'token': 2048,
                             'access_token': 4096}) == {
        'max_output_tokens': 1, 'max_tokens': 8192, 'other_tokens': '[REDACTED]',
        'token': '[REDACTED]', 'access_token': '[REDACTED]'}


@pytest.mark.parametrize('value', [
    'sk-' + 'thisLooksLikeARealCredential123456789',
    'Authorization: Bearer realLiteralCredential123456789',
    'https://privateuser:secretvalue@private.invalid/simple',
    '-----BEGIN ' + 'PRIVATE KEY-----\nQUJDREVGR0g=\n-----END PRIVATE KEY-----',
])
@pytest.mark.parametrize('file', ['src/package/main.py', 'uv.lock'])
def test_source_with_obvious_credential_is_not_archived(identity_root, value, file):
    root = identity_root
    raw = ('VALUE = ' + repr(value) + '\n').encode()
    # PEM is realistically multiline rather than the escaped repr used for normal string constants.
    if value.startswith('-----BEGIN'):
        raw = ('VALUE = """' + value + '"""\n').encode()
    (root / file).write_bytes(raw)
    with pytest.raises(identity.RuntimeEvidenceError, match='^SOURCE_CONTAINS_CREDENTIAL$'):
        identity.capture_identity(root)
    assert not (root / identity.EVIDENCE / 'objects' / (identity.digest(raw) + '.evidence')).exists()


def test_source_redaction_patterns_are_not_themselves_mistaken_for_credentials(identity_root):
    raw = Path(identity.__file__).read_bytes()
    (identity_root / 'src/package/main.py').write_bytes(raw)
    assert identity.capture_identity(identity_root)['state'] == 'READY'


@pytest.mark.parametrize('location', ['src/package/main.py', 'src/linked', 'configs/default.toml',
                                    'data/normalized/runtime-evidence/objects'])
def test_linked_inputs_and_evidence_directories_are_rejected(identity_root, tmp_path, location):
    target = tmp_path / 'outside'
    target.mkdir()
    path = identity_root / location
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    path.symlink_to(target, target_is_directory=True)
    with pytest.raises(identity.RuntimeEvidenceError, match='UNSAFE_PATH'):
        identity.capture_identity(identity_root)
    assert not list(target.iterdir())


def test_pycache_does_not_consume_source_scan_budget(identity_root, monkeypatch):
    cache = identity_root / 'src/package/__pycache__'
    cache.mkdir()
    for i in range(30):
        (cache / f'{i}.pyc').write_bytes(b'cache')
    monkeypatch.setattr(identity, 'MAX_FILES', 6)
    assert identity.capture_identity(identity_root)['source_file_count'] == 3


@pytest.mark.parametrize('constant,value', [('MAX_FILE_BYTES', 1), ('MAX_TOTAL_BYTES', 1), ('MAX_FILES', 1)])
def test_identity_size_and_scan_budgets_are_fail_safe(identity_root, monkeypatch, constant, value):
    monkeypatch.setattr(identity, constant, value)
    with pytest.raises(identity.RuntimeEvidenceError, match='SIZE_LIMIT|SCAN_LIMIT'):
        identity.capture_identity(identity_root)


def test_bad_toml_and_damaged_immutable_object_report_only_safe_codes(identity_root):
    root = identity_root
    first = identity.capture_identity(root)
    row = manifest(root, first)['source_files'][0]
    (root / row['object_path']).write_bytes(b'corrupt')
    with pytest.raises(identity.RuntimeEvidenceError, match='^EVIDENCE_CONFLICT$'):
        identity.capture_identity(root)
    (root / 'configs/default.toml').write_text('value="PRIVATE_ERROR_TEXT\n')
    (root / row['object_path']).write_bytes((root / row['path']).read_bytes())
    with pytest.raises(identity.RuntimeEvidenceError, match='^IDENTITY_CAPTURE_FAILED$'):
        identity.capture_identity(root)


def test_dependency_declaration_is_identity_input_and_missing_lock_is_explicit(identity_root):
    root = identity_root
    first = identity.capture_identity(root)
    (root / 'uv.lock').write_text('version=2\n')
    second = identity.capture_identity(root)
    assert second['identity_id'] != first['identity_id']
    (root / 'uv.lock').unlink()
    third = identity.capture_identity(root)
    assert manifest(root, third)['missing_declarations'] == ['uv.lock', *identity.LAUNCHERS]
    (root / 'pyproject.toml').unlink()
    with pytest.raises(identity.RuntimeEvidenceError, match='DECLARATION_MISSING'):
        identity.capture_identity(root)


@pytest.mark.parametrize('alter', ['duplicate', 'traversal', 'foreign_object', 'bad_hash'])
def test_identity_manifest_rejects_unsafe_or_damaged_references(identity_root, alter):
    first = identity.capture_identity(identity_root)
    body = manifest(identity_root, first)
    if alter == 'duplicate':
        body['source_files'].append(body['source_files'][0])
    elif alter == 'traversal':
        body['source_files'][0]['path'] = '../private.py'
    elif alter == 'foreign_object':
        body['source_files'][0]['object_path'] = 'data/state/ledger.sqlite3'
    raw = identity.encode(body)
    expected = '0' * 64 if alter == 'bad_hash' else identity.digest(raw)
    with pytest.raises(identity.RuntimeEvidenceError, match='IDENTITY_INVALID'):
        identity.validate_identity(raw, expected)


def write_launchers(root):
    for name in identity.LAUNCHERS:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'#!/bin/sh\nexit 0\n')
        path.chmod(0o755)


def test_named_launcher_bytes_and_actual_modes_are_audited_and_old_identity_stays_immutable(identity_root):
    root = identity_root
    write_launchers(root)
    first = identity.capture_identity(root)
    body = manifest(root, first)
    rows = [row for row in body['source_files'] if row['path'] in identity.LAUNCHERS]
    assert {row['path'] for row in rows} == set(identity.LAUNCHERS)
    assert first['source_file_count'] == 6
    assert body['missing_declarations'] == []
    assert all(row['mode'] == 0o755 for row in rows)
    assert all((root / row['object_path']).read_bytes() == b'#!/bin/sh\nexit 0\n' for row in rows)
    assert identity.capture_identity(root) == first
    changed = root / identity.LAUNCHERS[0]
    changed.chmod(0o700)
    second = identity.capture_identity(root)
    assert second['identity_id'] != first['identity_id']
    updated = next(row for row in manifest(root, second)['source_files'] if row['path'] == identity.LAUNCHERS[0])
    original = next(row for row in rows if row['path'] == identity.LAUNCHERS[0])
    assert updated['mode'] == 0o700 and updated['sha256'] == original['sha256']
    changed.write_bytes(b'#!/bin/sh\nexit 7\n')
    third = identity.capture_identity(root)
    assert third['identity_id'] != second['identity_id']
    assert manifest(root, first) == body
    assert (root / original['object_path']).read_bytes() == b'#!/bin/sh\nexit 0\n'
    assert (root / original['object_path']).stat().st_mode & 0o777 == 0o600


def test_missing_launchers_are_explicit_and_unrelated_deploy_files_are_never_read(identity_root, monkeypatch):
    root = identity_root
    first = identity.capture_identity(root)
    assert manifest(root, first)['missing_declarations'] == list(identity.LAUNCHERS)
    write_launchers(root)
    for name in identity.LAUNCHERS[1:]:
        (root / name).unlink()
    secret = root / 'deploy/launchd/bin/private.pem'
    secret.write_text('PRIVATE_FILE_NOT_AUDITED')
    real_read = identity.read_bytes
    def guarded(path, *args, **kwargs):
        assert path != secret
        return real_read(path, *args, **kwargs)
    monkeypatch.setattr(identity, 'read_bytes', guarded)
    captured = identity.capture_identity(root)
    body = manifest(root, captured)
    assert body['missing_declarations'] == list(identity.LAUNCHERS[1:])
    assert captured['source_file_count'] == 4
    assert not any(row['path'].endswith('private.pem') for row in body['source_files'])


@pytest.mark.parametrize('kind', ['file_link', 'parent_link', 'directory', 'fifo'])
def test_launcher_capture_rejects_links_and_nonregular_files(identity_root, tmp_path, kind):
    write_launchers(identity_root)
    path = identity_root / identity.LAUNCHERS[0]
    if kind == 'parent_link':
        saved = tmp_path / 'saved'
        path.parent.rename(saved)
        path.parent.symlink_to(saved, target_is_directory=True)
    else:
        path.unlink()
        if kind == 'file_link':
            path.symlink_to(identity_root / 'src/package/main.py')
        elif kind == 'directory':
            path.mkdir()
        else:
            os.mkfifo(path)
    with pytest.raises(identity.RuntimeEvidenceError, match='UNSAFE_PATH|UNSAFE_FILE'):
        identity.capture_identity(identity_root)


@pytest.mark.parametrize('mode', [None, True, '0755', -1, 0o10000])
def test_new_launcher_manifest_requires_bounded_integer_mode(identity_root, mode):
    write_launchers(identity_root)
    body = manifest(identity_root, identity.capture_identity(identity_root))
    row = next(row for row in body['source_files'] if row['path'] in identity.LAUNCHERS)
    if mode is None:
        row.pop('mode')
    else:
        row['mode'] = mode
    raw = identity.encode(body)
    with pytest.raises(identity.RuntimeEvidenceError, match='IDENTITY_INVALID'):
        identity.validate_identity(raw, identity.digest(raw))


def test_manifest_rejects_unregistered_deploy_executable_paths(identity_root):
    write_launchers(identity_root)
    body = manifest(identity_root, identity.capture_identity(identity_root))
    row = next(row for row in body['source_files'] if row['path'] in identity.LAUNCHERS)
    row['path'] = 'deploy/launchd/bin/arbitrary-private-tool'
    raw = identity.encode(body)
    with pytest.raises(identity.RuntimeEvidenceError, match='IDENTITY_INVALID'):
        identity.validate_identity(raw, identity.digest(raw))


def test_launcher_permission_change_during_capture_is_not_sealed_as_stable(identity_root, monkeypatch):
    write_launchers(identity_root)
    path = identity_root / identity.LAUNCHERS[0]
    real_read = identity.read_bytes
    def racing(file, *args, **kwargs):
        value = real_read(file, *args, **kwargs)
        if file == path:
            file.chmod(0o700)
        return value
    monkeypatch.setattr(identity, 'read_bytes', racing)
    with pytest.raises(identity.RuntimeEvidenceError, match='INPUT_CHANGED'):
        identity.capture_identity(identity_root)
    assert not (identity_root / identity.EVIDENCE / 'identities').exists()


def test_launcher_credential_content_is_not_archived(identity_root):
    write_launchers(identity_root)
    raw = b'#!/bin/sh\n# Bearer realisticCredentialValue123456789\n'
    (identity_root / identity.LAUNCHERS[0]).write_bytes(raw)
    with pytest.raises(identity.RuntimeEvidenceError, match='SOURCE_CONTAINS_CREDENTIAL'):
        identity.capture_identity(identity_root)
    assert not (identity_root / identity.EVIDENCE / 'objects' / (identity.digest(raw) + '.evidence')).exists()


@pytest.mark.parametrize('legacy', [False, True])
def test_archive_keeps_new_launcher_bytes_modes_and_accepts_original_legacy_manifest(identity_root, monkeypatch, legacy):
    from china_stock_sim.runtime import archive
    root = identity_root
    if not legacy:
        write_launchers(root)
    captured = identity.capture_identity(root)
    body = manifest(root, captured)
    if legacy:
        # Exactly the pre-launcher schema1 shape: no launcher paths/modes/missing markers.
        body['missing_declarations'] = []
        raw = identity.encode(body)
        key = identity.digest(raw)
        relative = f'{identity.EVIDENCE}/identities/{key}.json'
        identity.save_immutable(root, relative, raw)
        captured = {**captured, 'identity_id': key, 'manifest_path': relative}
        assert identity.validate_identity(raw, key) == body
    day = '2026-11-01'
    invocation = 'b' * 32
    common = {'schema_version': 1, 'invocation_id': invocation, 'task': 'homepage',
              'recorded_at': day + 'T09:00:00+08:00'}
    log = root / 'logs/runtime' / day / (invocation + '.jsonl')
    log.parent.mkdir(parents=True)
    log.write_bytes(identity.encode({**common, 'event': 'START', 'identity': captured})
                    + identity.encode({**common, 'event': 'END', 'exit_code': 0}))
    monkeypatch.setattr(archive, '_now', lambda: datetime.fromisoformat('2026-11-02T15:00:00+08:00'))
    result = archive.archive_runtime(root, date=day)
    assert result['state'] == 'COMPLETE'
    assert archive.verify_archive(root, result['archive_path'])['state'] == 'READY'
    with tarfile.open(root / result['archive_path'], 'r:gz') as reader:
        archived_raw = reader.extractfile(captured['manifest_path']).read()
        assert archived_raw == (root / captured['manifest_path']).read_bytes()
        archived = identity.validate_identity(archived_raw, captured['identity_id'])
        assert archived == body
        for row in archived['source_files']:
            if row['path'] in identity.LAUNCHERS:
                assert row['mode'] == 0o755
                assert reader.extractfile(row['object_path']).read() == b'#!/bin/sh\nexit 0\n'
