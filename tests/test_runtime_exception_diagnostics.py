"""Failure location is useful; arbitrary exception text, local values and source lines are not logs."""

import json

import pytest
from test_runtime_recording import events, runtime_root

from china_stock_sim.runtime import history, recording

assert runtime_root


def test_chained_project_stack_retains_location_without_arguments_or_locals(runtime_root):
    root = runtime_root
    namespace = {}
    code = """
def inner():
    credential = 'PRIVATE_LITERAL_VALUE'
    raise ValueError('https://private.invalid/api?key=' + credential)
def outer():
    try:
        inner()
    except ValueError as previous:
        raise OverflowError('/private/home/user/database SQLITE_SECRET') from previous
"""
    exec(compile(code, str(root / 'src/application/example.py'), 'exec'), namespace)
    with pytest.raises(OverflowError):
        recording.run_observed(root, 'workflow',
            lambda: recording.stage('workflow.close', namespace['outer']))
    rows = events(root)
    encoded = json.dumps(rows)
    for secret in ('PRIVATE_LITERAL_VALUE', 'private.invalid', '/private/home', 'SQLITE_SECRET', 'credential ='):
        assert secret not in encoded
    diagnostic = rows[-1]['exception']
    assert [item['type'] for item in diagnostic['chain']] == ['OverflowError', 'ValueError']
    assert diagnostic['chain'][1]['relation'] == 'cause'
    assert diagnostic['chain'][0]['summary'] == 'NUMERIC_OR_RECORD_SIZE_LIMIT'
    assert diagnostic['chain'][0]['frames'] == [
        {'file': 'src/application/example.py', 'function': 'outer', 'line': 9}]
    assert max(len(json.dumps(row).encode()) for row in rows) < recording.MAX_EVENT_BYTES
    result = history.query_history(root)
    assert result['records'][0]['exception'] == diagnostic
    assert result['records'][0]['events'][-1]['exception']['chain']


def test_caught_exception_hook_is_fail_open_and_no_context_is_zero_io(runtime_root):
    root = runtime_root
    assert recording.record_exception(ValueError('PRIVATE_LITERAL')) is None
    assert not (root / 'logs').exists()

    def callback():
        try:
            raise RuntimeError('Authorization: Bearer PRIVATE_LITERAL')
        except RuntimeError as exc:
            assert recording.record_exception(exc, account_id='S1', run_id='run:1')
        return {'state': 'FAILED'}, 2

    assert recording.run_observed(root, 'workflow', callback) == ({'state': 'FAILED'}, 2)
    recorded = events(root)[1]
    assert recorded['kind'] == 'exception' and recorded['account_id'] == 'S1'
    assert 'PRIVATE_LITERAL' not in json.dumps(events(root))


def test_diagnostic_projection_rejects_injected_fields_paths_and_unbounded_chains():
    frame = {'file': '/private/SECRET.py', 'function': 'danger', 'line': 1, 'locals': {'key': 'SECRET'}}
    safe = recording.safe_exception({'chain': [{'type': 'ValueError', 'message': 'SECRET',
        'frames': [frame], 'relation': 'cause'}] * 100, 'locals': 'SECRET'})
    assert len(safe['chain']) == 6
    assert not any(item['frames'] for item in safe['chain'])
    assert 'SECRET' not in json.dumps(safe)


def test_broken_trace_projection_never_replaces_original_exception(runtime_root, monkeypatch):
    error = ValueError('PRIVATE_LITERAL')
    monkeypatch.setattr(recording, '_exception_details', lambda *args: (_ for _ in ()).throw(OSError()))
    with pytest.raises(ValueError) as raised:
        recording.run_observed(runtime_root, 'workflow', lambda: (_ for _ in ()).throw(error))
    assert raised.value is error
    assert events(runtime_root)[-1]['exception']['truncated'] is True
