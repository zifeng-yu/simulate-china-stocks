"""Operational logging must never become a prerequisite for business execution."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from china_stock_sim.runtime import recording


@pytest.fixture
def runtime_root(tmp_path, monkeypatch):
    root = tmp_path / 'project'
    root.mkdir()

    def identity(root):
        name = 'a' * 64
        path = root / f'data/normalized/runtime-evidence/identities/{name}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}\n')
        return {'state': 'READY', 'identity_id': name,
                'manifest_path': str(path.relative_to(root))}

    monkeypatch.setattr(recording, '_identity', identity)
    return root


def events(root):
    paths = list(root.glob('logs/runtime/*/*.jsonl'))
    assert len(paths) == 1
    return [json.loads(line) for line in paths[0].read_text().splitlines()]


@pytest.mark.parametrize('state,code', [('COMPLETE', 0), ('FAILED', 2), ('DISABLED', 0),
                                      ('BUSY', 0), ('RETRY_WAIT', 0), ('WAITING_TIME', 0)])
def test_result_and_exit_are_exact_with_start_end_even_for_no_work(runtime_root, state, code):
    value = ({'state': state, 'changed': False, 'last_success_at': '2026-11-02T09:00:00+08:00',
              'next_due_at': '2026-11-03T09:00:00+08:00'}, code)
    returned = recording.run_observed(runtime_root, 'upload', lambda: value, scheduled=True,
                                     options={'republish': False, 'private': 'PRIVATE_SECRET'})
    assert returned is value
    rows = events(runtime_root)
    assert [r['event'] for r in rows] == ['START', 'END']
    assert rows[0]['scheduled'] is True and rows[0]['trigger'] == 'SCHEDULED'
    assert rows[0]['options'] == {'republish': False}
    assert rows[-1]['exit_code'] == code and rows[-1]['result_state'] == state
    assert rows[-1]['result'] == value[0]
    assert rows[-1]['metrics']['elapsed_seconds'] >= 0
    assert rows[-1]['metrics']['peak_rss_scope'] == 'PROCESS_LIFETIME_NOT_INTERVAL_DELTA'
    assert 'PRIVATE_SECRET' not in json.dumps(rows)


def test_nested_stages_requests_and_unicode_account_identity_are_safe(runtime_root):
    result = {'state': 'COMPLETE', 'decision': {'decision_id': 'decision:abc',
              'private_prompt': 'PRIVATE_SECRET'}, 'large_result': 'PRIVATE_SECRET' * 100_000}

    def work():
        recording.emit_event('data_request', request_id='a' * 64, source='akshare', category='equity',
            attempts=2, retry_failures=1, status='FAILED', error_code='TIMEOUT',
            server_retry_at='2026-11-02T09:01:00+08:00', url='https://private.example/SECRET',
            response='PRIVATE_SECRET', environment={'password': 'PRIVATE_SECRET'})
        assert recording.stage('workflow.plan', lambda: result, run_id='r:1', account_id='a:含冒号',
                               session='2026-11-02') is result
        return result, 0

    recording.run_observed(runtime_root, 'workflow', work, options={'download': True})
    rows = events(runtime_root)
    assert [r['sequence'] for r in rows] == list(range(1, len(rows) + 1))
    assert rows[2]['account_id'] == rows[3]['account_id'] == 'a:含冒号'
    assert rows[3]['result']['decision_id'] == rows[-1]['result']['decision_id'] == 'decision:abc'
    assert rows[1]['attempts'] == 2 and rows[1]['retry_failures'] == 1
    assert 'PRIVATE_SECRET' not in json.dumps(rows, ensure_ascii=False)
    assert 'private.example' not in json.dumps(rows)
    assert max(len(json.dumps(row).encode()) for row in rows) < recording.MAX_EVENT_BYTES


@pytest.mark.parametrize('scheduled_for', ['2026-11-02', '2026-11-02T21:00:00+08:00'])
def test_upload_slot_result_survives_end_and_history_projection(runtime_root, scheduled_for):
    from china_stock_sim.runtime.history import query_history

    result = {'state': 'COMPLETE', 'changed': False, 'scheduled_for': scheduled_for,
              'last_success_slot': '2026-11-02T21:00:00+08:00',
              'next_due_at': '2026-11-03T09:00:00+08:00'}
    recording.run_observed(runtime_root, 'upload', lambda: (result, 0), scheduled=True)
    record = query_history(runtime_root, task='upload')['records'][0]
    assert record['result'] == result
    assert record['omitted_field_count'] == 0 and record['dropped_events'] == 0
    assert events(runtime_root)[-1]['result'] == result


@pytest.mark.parametrize('value', ['password=PRIVATE_SECRET', '2026-11-02T21:00:00',
                                  '2026-11-33T21:00:00+08:00'])
def test_slot_projection_rejects_non_temporal_or_naive_values(runtime_root, value):
    result = {'state': 'COMPLETE', 'scheduled_for': value, 'last_success_slot': value}
    recording.run_observed(runtime_root, 'upload', lambda: (result, 0))
    saved = events(runtime_root)[-1]['result']
    assert 'scheduled_for' not in saved and 'last_success_slot' not in saved
    assert saved['omitted_field_count'] == 2
    assert value not in json.dumps(events(runtime_root))


def test_rejected_scalar_identity_has_visible_omission_without_secret(runtime_root):
    def work():
        recording.emit_event('data_request', account_id='bad\nPRIVATE_SECRET', source='https://secret',
                             error_code='bearer SECRET', attempts=-1)
        return {'state': 'COMPLETE', 'run_id': 'PRIVATE_SECRET'}, 0

    recording.run_observed(runtime_root, 'workflow', work)
    rows = events(runtime_root)
    assert rows[1]['omitted_fields'] == ['account_id', 'attempts', 'error_code', 'source']
    assert rows[-1]['result']['omitted_fields'] == ['run_id']
    assert 'PRIVATE_SECRET' not in json.dumps(rows)


def test_publication_and_business_references_survive_recording_and_history(runtime_root):
    from china_stock_sim.runtime.history import query_history

    references = {'target_id': 'a' * 64, 'release_id': 'b' * 64,
                  'plan_id': 'plan:2026-11-02', 'backup_id': 'backup-20261102',
                  'published_at': '2026-11-02T09:00:00+08:00'}

    def work():
        recording.emit_event('upload_release', **references, state='COMPLETE')
        return dict(references, state='COMPLETE'), 0

    recording.run_observed(runtime_root, 'upload', work, scheduled=True)
    rows = events(runtime_root)
    for key, value in references.items():
        assert rows[1][key] == rows[-1]['result'][key] == value
    result = query_history(runtime_root)
    record = result['records'][0]
    assert result['state'] == 'READY'
    for key, value in references.items():
        assert record['result'][key] == value
        assert record['events'][0][key] == value


@pytest.mark.parametrize('value', ['release-local-name', 'a' * 63, 'a' * 65,
                                  'A' * 64, 'g' * 64, 'https://host.invalid/release', 123])
def test_target_and_release_references_require_exact_hash(value):
    result = recording.safe_fields({'target_id': value, 'release_id': value})
    assert result == {'omitted_fields': ['release_id', 'target_id'], 'omitted_field_count': 2}


def test_explicit_unknown_scalars_remain_null_without_false_omissions(runtime_root):
    from china_stock_sim.runtime.history import query_history

    null_fields = dict.fromkeys(recording._IDS | recording._TIMES | recording._NUMBERS
                               | recording._DATES | recording._LABELS | recording._FLAGS)
    result = dict(null_fields, state='COMPLETE')
    recording.run_observed(runtime_root, 'upload', lambda: (result, 0))
    assert events(runtime_root)[-1]['result'] == result
    history = query_history(runtime_root)
    assert history['state'] == 'READY'
    assert history['coverage']['counts']['omitted_fields'] == 0
    assert history['records'][0]['result'] == result
    assert recording.safe_fields({'decision': {'decision_id': None}}) == {'decision_id': None}
    assert recording.safe_fields({'next_retry_at': 'PRIVATE_SECRET'}) == {
        'omitted_fields': ['next_retry_at'], 'omitted_field_count': 1}


@pytest.mark.parametrize('error', [ValueError('PRIVATE_SECRET'), KeyboardInterrupt('PRIVATE_SECRET'),
                                  SystemExit(7)])
def test_original_exception_is_reraised_and_context_is_reset(runtime_root, error):
    def work():
        return recording.stage('upload.transfer', lambda: raise_same(error))

    with pytest.raises(type(error)) as raised:
        recording.run_observed(runtime_root, 'upload', work)
    assert raised.value is error
    rows = events(runtime_root)
    assert rows[-2]['event'] == 'STAGE_ERROR' and rows[-1]['event'] == 'END'
    assert rows[-1]['result_state'] == 'EXCEPTION'
    assert 'PRIVATE_SECRET' not in json.dumps(rows)
    assert recording._CURRENT.get() is None


@pytest.mark.parametrize('handled,expected', [((), None), ((ValueError,), 2)])
def test_only_explicit_caller_exception_contract_records_exit_two(runtime_root, handled, expected):
    error = ValueError('PRIVATE_SECRET')
    with pytest.raises(ValueError) as raised:
        recording.run_observed(runtime_root, 'workflow', lambda: raise_same(error),
                               handled_exceptions=handled)
    assert raised.value is error
    assert events(runtime_root)[-1]['exit_code'] == expected


def test_system_exit_none_is_known_zero_without_swallowing(runtime_root):
    error = SystemExit(None)
    with pytest.raises(SystemExit) as raised:
        recording.run_observed(runtime_root, 'workflow', lambda: raise_same(error))
    assert raised.value is error and events(runtime_root)[-1]['exit_code'] == 0


def raise_same(error):
    raise error


def test_no_context_event_and_stage_create_no_records(tmp_path):
    before = list(tmp_path.iterdir())
    recording.emit_event('data_request', request_id='abc')
    assert recording.stage('workflow.plan', lambda: 23) == 23
    assert list(tmp_path.iterdir()) == before


def test_logging_write_failure_and_broken_stderr_do_not_change_callback(runtime_root, monkeypatch):
    class BrokenStderr:
        def write(self, _value):
            raise OSError('PRIVATE_SECRET')

        def flush(self):
            raise OSError('PRIVATE_SECRET')

    monkeypatch.setattr(recording.sys, 'stderr', BrokenStderr())
    monkeypatch.setattr(recording.os, 'write', lambda *_args: raise_same(OSError('PRIVATE_SECRET')))
    value = ({'state': 'COMPLETE'}, 0)
    assert recording.run_observed(runtime_root, 'homepage', lambda: value) is value
    error = RuntimeError('ORIGINAL_BUSINESS_FAILURE')
    with pytest.raises(RuntimeError) as raised:
        recording.run_observed(runtime_root, 'homepage', lambda: raise_same(error))
    assert raised.value is error


def test_identity_sampling_failure_is_fail_open_and_visible(runtime_root, monkeypatch, capsys):
    monkeypatch.setattr(recording, '_identity', lambda *_args: raise_same(RuntimeError('PRIVATE_SECRET')))
    value = ({'state': 'COMPLETE'}, 0)
    assert recording.run_observed(runtime_root, 'workflow', lambda: value) is value
    assert 'PRIVATE_SECRET' not in capsys.readouterr().err
    assert events(runtime_root)[-1]['dropped_events'] > 0


def test_logs_symlink_does_not_write_outside_project(runtime_root, tmp_path, capsys):
    external = tmp_path / 'external'
    external.mkdir()
    (runtime_root / 'logs').symlink_to(external, target_is_directory=True)
    value = ({'state': 'DISABLED'}, 0)
    assert recording.run_observed(runtime_root, 'workflow', lambda: value) is value
    assert list(external.iterdir()) == []
    assert capsys.readouterr().err.strip() == 'RUNTIME_RECORDING_UNAVAILABLE'


def test_concurrent_tasks_have_separate_complete_invocation_files(runtime_root):
    def run(index):
        def work():
            recording.emit_event('data_request', request_id=str(index), status='COMPLETE')
            return {'state': 'COMPLETE', 'run_id': str(index)}, 0
        return recording.run_observed(runtime_root, 'workflow', work)

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert len(list(pool.map(run, range(8)))) == 8
    paths = list(runtime_root.glob('logs/runtime/*/*.jsonl'))
    assert len(paths) == 8
    for path in paths:
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        assert [r['event'] for r in rows] == ['START', 'EVENT', 'END']
        assert rows[1]['request_id'] == rows[-1]['result']['run_id']
        assert all(r['invocation_id'] == path.stem for r in rows)


@pytest.mark.parametrize('system,rss,expected', [('Darwin', 2048, 2048), ('Linux', 2048, 2097152),
                                             ('Unknown', 2048, None), ('Darwin', -1, None)])
def test_rss_units_and_unknown_platform(system, rss, expected):
    assert recording._rss_bytes(rss, system) == expected


def test_cpu_deltas_children_and_peak_rss_scope(monkeypatch):
    monkeypatch.setattr(recording, 'measurement', lambda: {'monotonic': 12.0, 'cpu_user': 5.0,
        'cpu_system': 3.0, 'child_cpu_user': 6.0, 'child_cpu_system': 2.0,
        'peak_rss_bytes': 9000, 'disk_free_bytes': 8000})
    result = recording.measure_since({'monotonic': 10.0, 'cpu_user': 4.0, 'cpu_system': 2.5,
                                     'child_cpu_user': 4.0, 'child_cpu_system': 1.0})
    assert result['elapsed_seconds'] == 2 and result['cpu_user_seconds'] == 1
    assert result['cpu_system_seconds'] == .5 and result['cpu_percent_one_core'] == 75
    assert result['child_cpu_user_seconds'] == 2 and result['child_cpu_system_seconds'] == 1
    assert result['peak_rss_bytes'] == 9000
    assert recording.measure_since({})['elapsed_seconds'] is None


def test_resource_failure_is_unknown_not_zero(monkeypatch):
    monkeypatch.setattr(recording, 'resource', SimpleNamespace(
        RUSAGE_SELF=0, RUSAGE_CHILDREN=-1,
        getrusage=lambda _kind: raise_same(OSError('PRIVATE_SECRET'))))
    sample = recording.measurement()
    assert sample['cpu_user'] is None and sample['peak_rss_bytes'] is None


def test_actual_hard_kill_keeps_started_record_without_fabricated_end(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    script = '''import sys,time
from china_stock_sim.runtime import recording
recording._identity=lambda root: {'state':'UNAVAILABLE','reason':'IDENTITY_CAPTURE_FAILED'}
def work():
 print('STARTED',flush=True)
 time.sleep(30)
 return {'state':'COMPLETE'},0
recording.run_observed(sys.argv[1],'workflow',work)
'''
    process = subprocess.Popen([sys.executable, '-c', script, str(root)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == 'STARTED'
        process.kill()
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    rows = events(root)
    assert [r['event'] for r in rows] == ['START']
    assert rows[0]['pid'] == process.pid
    assert 'finished_at' not in rows[0]
    assert os.stat(next(root.glob('logs/runtime/*/*.jsonl'))).st_mode & 0o777 == 0o600


def test_homepage_result_only_keeps_owned_snapshot_path(runtime_root):
    path = runtime_root / ('reports/dashboard-snapshots/' + 'a' * 64 + '.json')
    value = ({'state': 'DASHBOARD_PUBLISHED', 'status': str(path)}, 0)
    recording.run_observed(runtime_root, 'homepage', lambda: value)
    assert events(runtime_root)[-1]['result']['status'] == str(path.relative_to(runtime_root))
    assert recording.safe_fields({'status': '/private/key.pem'}, runtime_root)['omitted_fields'] == ['status']


def test_identity_capture_crossing_midnight_keeps_true_invocation_start_date(runtime_root, monkeypatch):
    times = iter(['2026-11-02T23:59:59+08:00', '2026-11-03T00:00:01+08:00'])
    monkeypatch.setattr(recording, '_now', lambda: next(times))
    recording.run_observed(runtime_root, 'upload', lambda: ({'state': 'COMPLETE'}, 0))
    rows = events(runtime_root)
    path = next(runtime_root.glob('logs/runtime/*/*.jsonl'))
    assert path.parent.name == '2026-11-02'
    assert rows[0]['recorded_at'] == '2026-11-02T23:59:59+08:00'
    assert rows[-1]['recorded_at'] == '2026-11-03T00:00:01+08:00'
