"""Public read boundary and failure behavior of the local shutdown indicator."""

import json
from types import SimpleNamespace

import pytest

from china_stock_sim.cli import main


def inventory(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob('*') if path.is_file()}


@pytest.mark.parametrize('state', ['READY', 'WORKING', 'WAITING', 'BLOCKED', 'UNKNOWN'])
def test_business_states_are_successful_queries_with_no_observation_writes(
        project, monkeypatch, capsys, state):
    from china_stock_sim.readiness import status

    monkeypatch.setattr(status, 'build_status', lambda settings: {'schema_version': 1,
        'state': state, 'accounts': []})
    before = inventory(project)
    assert main(['--root', str(project), 'shutdown-status']) == 0
    assert json.loads(capsys.readouterr().out)['state'] == state
    assert inventory(project) == before


def test_probe_fault_clears_green_and_preserves_safe_stack(project, monkeypatch, capsys):
    from china_stock_sim.readiness import status

    def fail(settings):
        raise RuntimeError('secret-token-must-not-appear')

    monkeypatch.setattr(status, 'build_status', fail)
    before = inventory(project)
    assert main(['--root', str(project), 'shutdown-status']) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out)['state'] == 'UNKNOWN'
    assert 'SHUTDOWN_STATUS_UNAVAILABLE' in captured.err
    assert 'in fail' in captured.err
    assert 'secret-token-must-not-appear' not in captured.out + captured.err
    assert inventory(project) == before


@pytest.mark.parametrize('flag', ['--now', '--fixture', '--download'])
def test_production_command_has_no_clock_fixture_or_collection_overrides(project, flag):
    before = inventory(project)
    with pytest.raises(SystemExit) as error:
        main(['--root', str(project), 'shutdown-status', flag, '2026-11-04'])
    assert error.value.code == 2
    assert inventory(project) == before


def test_structured_probe_fault_is_nonzero_and_preserves_frames(project, monkeypatch, capsys):
    from china_stock_sim.readiness import status

    monkeypatch.setattr(status, 'build_status', lambda settings: {
        'schema_version': 1, 'state': 'UNKNOWN', 'diagnostic': {
            'exception_type': 'ValueError', 'stack': ['probe.py:21 in read_facts']}})
    before = inventory(project)
    assert main(['--root', str(project), 'shutdown-status']) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out)['state'] == 'UNKNOWN'
    assert 'probe.py:21 in read_facts' in captured.err
    assert 'ValueError: SHUTDOWN_STATUS_UNAVAILABLE' in captured.err
    assert inventory(project) == before


@pytest.mark.parametrize(('system', 'supported', 'state', 'exit_code'), [
    ('Darwin', True, 'BUILD_REQUIRED', 0),
    ('Linux', False, 'UNSUPPORTED_PLATFORM', 2),
])
def test_installation_status_is_read_only_and_does_not_build(
        project, monkeypatch, capsys, system, supported, state, exit_code):
    from china_stock_sim.desktop import indicator

    calls = []

    def probe(command, runner, *, timeout=15):
        assert command[0] == '/bin/launchctl'
        assert command[1] in {'print', 'print-disabled'}
        calls.append(command[1])
        return SimpleNamespace(returncode=3 if command[1] == 'print' else 0, stdout='')

    def forbidden(*args, **kwargs):
        pytest.fail('A status query must not build or install the indicator')

    monkeypatch.setattr(indicator.platform, 'system', lambda: system)
    monkeypatch.setattr(indicator, '_invoke', probe)
    monkeypatch.setattr(indicator, '_directory', lambda root, *args: root / 'isolated-agents')
    monkeypatch.setattr(indicator, 'build', forbidden)
    monkeypatch.setattr(indicator, 'install', forbidden)
    before = inventory(project)
    assert main(['--root', str(project), 'shutdown-indicator-status']) == exit_code
    result = json.loads(capsys.readouterr().out)
    assert result['supported'] is supported
    assert result['state'] == state
    assert result['installed'] is False and result['artifact_matches'] is False
    assert result['query_error'] is None
    assert calls == (['print', 'print-disabled'] if supported else [])
    assert inventory(project) == before
