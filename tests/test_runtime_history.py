"""History queries report partial evidence, remain bounded, and cannot mutate execution."""

import json

import pytest

from china_stock_sim.runtime import history


@pytest.fixture
def history_root(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    manifest = root / ('data/normalized/runtime-evidence/identities/' + 'a' * 64 + '.json')
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{}\n')
    return root


def write_record(root, *, number=1, task='workflow', date='2026-11-02', closed=True, extra=None):
    identity = f'{number:032x}'
    common = {'schema_version': 1, 'invocation_id': identity, 'task': task}
    rows = [{**common, 'event': 'START', 'sequence': 1, 'recorded_at': date + 'T09:00:00+08:00',
             'pid': 123, 'process_birth': 'Mon Nov 2 01:00:00 2026', 'scheduled': True,
             'identity': {'state': 'READY', 'identity_id': 'a' * 64,
                 'manifest_path': 'data/normalized/runtime-evidence/identities/' + 'a' * 64 + '.json'}}]
    for fields in extra or []:
        rows.append({**common, 'event': 'EVENT', 'recorded_at': date + 'T09:00:01+08:00',
                     'sequence': len(rows) + 1, **fields})
    if closed:
        rows.append({**common, 'event': 'END', 'sequence': len(rows) + 1,
                     'recorded_at': date + 'T09:00:02+08:00', 'result_state': 'COMPLETE',
                     'exit_code': 0, 'dropped_events': 0, 'result': {'state': 'COMPLETE'},
                     'metrics': {'elapsed_seconds': 2, 'peak_rss_bytes': 4096}})
    path = root / f'logs/runtime/{date}/{identity}.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    return path


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_empty_history_does_not_create_any_directories(tmp_path):
    result = history.query_history(tmp_path)
    assert result['state'] == 'READY' and result['records'] == []
    assert list(tmp_path.iterdir()) == []


def test_filter_by_stage_identity_even_when_outside_returned_tail(history_root):
    fields = [{'kind': 'decision', 'run_id': 'run:one', 'account_id': 'a:含冒号',
               'result': {'decision_id': 'decision:one'}}]
    fields += [{'kind': 'data_request', 'request_id': str(i)} for i in range(55)]
    path = write_record(history_root, extra=fields)
    before = snapshot(history_root)
    result = history.query_history(history_root, date='2026-11-02', task='workflow',
        invocation_id=path.stem, run_id='run:one', account_id='a:含冒号', decision_id='decision:one')
    assert result['state'] == 'READY' and len(result['records']) == 1
    row = result['records'][0]
    assert row['state'] == 'FINISHED' and row['events_truncated']
    assert len(row['events']) == 50
    assert snapshot(history_root) == before
    assert not history.query_history(history_root, account_id='different')['records']


def test_filtered_healthy_task_keeps_unmatched_old_gaps_in_scanned_counts(history_root):
    write_record(history_root, task='homepage')
    upload = write_record(history_root, number=2, task='upload', extra=[
        {'kind': 'upload_state', 'omitted_fields': ['next_retry_at'], 'omitted_field_count': 1}])
    rows = [json.loads(line) for line in upload.read_text().splitlines()]
    rows[-1]['dropped_events'] = 1
    upload.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    before = snapshot(history_root)
    result = history.query_history(history_root, task='homepage')
    assert result['state'] == 'READY' and len(result['records']) == 1
    assert result['coverage']['counts_scope'] == 'MATCHING_RECORDS'
    assert result['coverage']['counts']['omitted_fields'] == 0
    assert result['coverage']['counts']['recording_gaps'] == 0
    assert result['coverage']['scanned_counts']['omitted_fields'] == 1
    assert result['coverage']['scanned_counts']['recording_gaps'] == 1
    assert result['coverage']['partial_reasons'] == []
    unfiltered = history.query_history(history_root)
    assert unfiltered['state'] == 'PARTIAL'
    assert unfiltered['coverage']['counts']['omitted_fields'] == 1
    assert unfiltered['coverage']['partial_reasons'] == ['MATCHING_RECORD_QUALITY']
    assert snapshot(history_root) == before


def test_single_invocation_excludes_other_history_without_hiding_its_inventory(history_root):
    healthy = write_record(history_root, task='homepage')
    other = write_record(history_root, number=2, task='upload')
    other.write_bytes(other.read_bytes() + b'broken tail')
    before = snapshot(history_root)
    result = history.query_history(history_root, invocation_id=healthy.stem)
    assert result['state'] == 'READY' and len(result['records']) == 1
    assert result['coverage']['scanned_files'] == 1
    assert result['coverage']['inventory']['files_seen'] == 2
    assert result['coverage']['scanned_counts']['corrupt'] == 0
    assert snapshot(history_root) == before


def test_scanned_corrupt_nonmatching_task_is_still_partial_with_explicit_reason(history_root):
    write_record(history_root, task='homepage')
    other = write_record(history_root, number=2, task='upload')
    other.write_bytes(other.read_bytes() + b'broken tail')
    result = history.query_history(history_root, task='homepage')
    assert result['state'] == 'PARTIAL' and len(result['records']) == 1
    assert result['coverage']['counts']['corrupt'] == 0
    assert result['coverage']['scanned_counts']['corrupt'] == 1
    assert result['coverage']['partial_reasons'] == ['SCANNED_RECORD_CORRUPTION']


@pytest.mark.parametrize('observed,state', [({'state': 'ALIVE', 'birth': 'Mon Nov 2 01:00:00 2026'}, 'RUNNING'),
    ({'state': 'ALIVE', 'birth': 'different birth'}, 'INTERRUPTED'),
    ({'state': 'DEAD', 'birth': None}, 'INTERRUPTED'), ({'state': 'UNKNOWN', 'birth': None}, 'UNKNOWN')])
def test_unclosed_record_uses_pid_birth_without_rewriting(history_root, monkeypatch, observed, state):
    path = write_record(history_root, closed=False)
    before = path.read_bytes()
    monkeypatch.setattr(history, 'probe_process', lambda _pid: observed)
    result = history.query_history(history_root)
    assert result['records'][0]['state'] == state
    assert result['records'][0]['finished_at'] is None and not result['records'][0]['closed']
    assert result['coverage']['counts']['unclosed'] == 1
    assert path.read_bytes() == before


@pytest.mark.parametrize('tail', [b'{"PRIVATE_SECRET":', b'\xff\xfe\n', b'not json\n'])
def test_corrupt_or_truncated_tail_is_preserved_and_not_reported_success(history_root, tail):
    path = write_record(history_root)
    path.write_bytes(path.read_bytes() + tail)
    before = snapshot(history_root)
    result = history.query_history(history_root)
    assert result['state'] == 'PARTIAL' and result['records'][0]['state'] == 'CORRUPT'
    assert result['coverage']['counts']['corrupt'] == 1
    assert 'PRIVATE_SECRET' not in json.dumps(result)
    assert snapshot(history_root) == before


def test_untrusted_event_fields_are_not_echoed_and_missing_identity_is_visible(history_root):
    path = write_record(history_root, extra=[{'kind': 'data_request', 'url': 'PRIVATE_SECRET',
        'response': 'PRIVATE_SECRET', 'account_id': 'bad\nPRIVATE_SECRET',
        'metrics': {'elapsed_seconds': 'PRIVATE_SECRET', 'cpu_user_seconds': -1}}])
    next(history_root.glob('data/normalized/runtime-evidence/identities/*.json')).unlink()
    result = history.query_history(history_root)
    row = result['records'][0]
    assert row['identity']['state'] == 'MISSING'
    assert result['coverage']['counts']['omitted_fields'] == 1
    assert result['state'] == 'PARTIAL' and 'PRIVATE_SECRET' not in json.dumps(result)
    assert path.exists()


def test_symlink_record_and_day_are_not_followed(history_root, tmp_path):
    external = tmp_path / 'external'
    external.mkdir()
    target = external / ('b' * 32 + '.jsonl')
    target.write_text('PRIVATE_SECRET')
    folder = history_root / 'logs/runtime/2026-11-02'
    folder.mkdir(parents=True)
    (folder / target.name).symlink_to(target)
    (folder.parent / '2026-11-01').symlink_to(external, target_is_directory=True)
    result = history.query_history(history_root)
    assert result['state'] == 'PARTIAL' and result['records'] == []
    assert result['coverage']['inventory']['unsafe_entries'] == 2
    assert 'PRIVATE_SECRET' not in json.dumps(result)


def test_symlink_runtime_root_is_rejected_without_reading_target(history_root, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (history_root / 'logs').mkdir()
    (history_root / 'logs/runtime').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='RUNTIME_UNSAFE_PATH'):
        history.query_history(history_root)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize('kwargs', [{'date': '2026-2-1'}, {'date': '2026-02-30'},
    {'date': '../../outside'}, {'task': 'PRIVATE_SECRET'}, {'invocation_id': '../secret'},
    {'run_id': 'x\nsecret'}, {'account_id': 'https://private'}, {'decision_id': 'bad secret'},
    {'limit': 0}, {'limit': 101}, {'limit': True}])
def test_invalid_filters_fail_safely_and_without_writes(history_root, kwargs):
    before = snapshot(history_root)
    with pytest.raises(ValueError, match='RUNTIME_INVALID_') as error:
        history.query_history(history_root, **kwargs)
    assert 'PRIVATE_SECRET' not in str(error.value)
    assert snapshot(history_root) == before


def test_file_and_result_budgets_are_explicit(history_root, monkeypatch):
    for index in range(1, 5):
        write_record(history_root, number=index)
    result = history.query_history(history_root, limit=1)
    assert result['coverage']['matched'] == 4 and result['coverage']['returned'] == 1
    assert result['coverage']['truncated']
    assert result['state'] == 'READY' and result['coverage']['results_truncated']
    assert not result['coverage']['scan_truncated']
    monkeypatch.setattr(history, 'MAX_SCAN_FILES', 2)
    result = history.query_history(history_root)
    assert result['coverage']['scanned_files'] == 2 and result['coverage']['truncated']


def test_byte_budget_does_not_load_unbounded_file(history_root, monkeypatch):
    write_record(history_root)
    monkeypatch.setattr(history, 'MAX_SCAN_BYTES', 100)
    result = history.query_history(history_root)
    assert result['state'] == 'PARTIAL' and not result['records']
    assert result['coverage']['scanned_bytes'] == 100
    assert result['coverage']['skipped'][0]['reason'] == 'FILE_BUDGET_EXCEEDED'


def test_workflow_storage_warning_uses_stat_without_reading_giant_json(history_root):
    path = history_root / 'data/state/workflow/journal.json'
    path.parent.mkdir(parents=True)
    with path.open('wb') as stream:
        stream.truncate(80_000_000)
    result = history.query_history(history_root)
    assert result['storage']['workflow_journal_warning'] == 'NEAR_LIMIT'
    assert path.stat().st_size == 80_000_000


def test_cross_midnight_end_remains_in_start_date_file(history_root):
    path = write_record(history_root)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[-1]['recorded_at'] = '2026-11-03T00:01:00+08:00'
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    result = history.query_history(history_root, date='2026-11-02')
    assert result['state'] == 'READY'
    assert result['records'][0]['finished_at'].startswith('2026-11-03')


@pytest.mark.parametrize('bad', [{'event': []}, {'task': {}}, {'schema_version': True}])
def test_wrong_json_field_types_are_corrupt_not_a_query_crash(history_root, bad):
    path = write_record(history_root)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0].update(bad)
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    result = history.query_history(history_root)
    assert result['state'] == 'PARTIAL'
    assert 'INVALID_EVENT_SCHEMA' in result['records'][0]['issues']


def test_untrusted_numeric_metrics_are_bounded_and_finite(history_root):
    path = write_record(history_root)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[-1]['metrics'] = {'elapsed_seconds': 10**400, 'peak_rss_bytes': -1,
                          'cpu_user_seconds': float('nan'), 'cpu_system_seconds': True}
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    result = history.query_history(history_root)
    assert result['records'][0]['metrics'] == {
        'peak_rss_scope': 'PROCESS_LIFETIME_NOT_INTERVAL_DELTA'}
    assert result['state'] == 'PARTIAL'
    assert 'INVALID_METRIC_FIELDS' in result['records'][0]['issues']


def test_process_probe_budget_is_finite_and_visible_as_unknown(history_root, monkeypatch):
    for index in range(1, 5):
        path = write_record(history_root, number=index, closed=False)
        value = json.loads(path.read_text())
        value['pid'] += index
        path.write_text(json.dumps(value) + '\n')
    calls = []
    monkeypatch.setattr(history, 'MAX_PROCESS_PROBES', 2)
    monkeypatch.setattr(history, 'probe_process', lambda pid: calls.append(pid) or {'state': 'DEAD'})
    result = history.query_history(history_root)
    assert len(calls) == 2
    assert result['coverage']['counts']['interrupted'] == 2
    assert result['coverage']['counts']['unknown'] == 2
