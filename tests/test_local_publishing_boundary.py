"""The private local extension is optional, while installed defects stay visible."""

import builtins
import json
from datetime import datetime
from importlib.util import find_spec

import pytest
from test_run_home_status import project_status

from china_stock_sim import cli
from china_stock_sim.reporting.run_home import render_home
from china_stock_sim.scheduler.status import _publication


def unavailable_cli(monkeypatch, missing='china_stock_sim.publishing'):
    def fail(name):
        assert name == 'china_stock_sim.publishing.cli'
        raise ModuleNotFoundError('Synthetic missing component', name=missing)

    monkeypatch.setattr(cli, 'import_module', fail)


def unavailable_status(monkeypatch, error):
    original = builtins.__import__

    def intercept(name, *args, **kwargs):
        if name == 'china_stock_sim.publishing.state':
            raise error
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', intercept)


def test_core_help_without_local_publishing_keeps_core_commands(monkeypatch, capsys):
    unavailable_cli(monkeypatch)
    with pytest.raises(SystemExit) as result:
        cli.main(['--help'])
    assert result.value.code == 0
    output = capsys.readouterr().out
    for command in ('workflow-run', 'dashboard-refresh', 'runtime-history', 'shutdown-status'):
        assert command in output
    for command in ('dashboard-sync', 'dashboard-export'):
        assert command not in output


def test_doctor_reports_missing_local_component_without_changing_global_capabilities(
        project, monkeypatch, capsys):
    unavailable_cli(monkeypatch)
    assert cli.main(['--root', str(project), 'doctor']) == 2
    result = json.loads(capsys.readouterr().out)
    assert result['capabilities']['static_dashboard_publishing'] == 'LOCAL_COMPONENT_NOT_INSTALLED'
    assert result['capabilities']['runtime_homepage'] == 'IMPLEMENTED'
    assert cli.CAPABILITIES['static_dashboard_publishing'] == 'IMPLEMENTED'


@pytest.mark.parametrize('missing', ['china_stock_sim.publishing.cli', 'installed_dependency'])
def test_installed_cli_import_defects_are_not_treated_as_an_absent_component(monkeypatch, missing):
    unavailable_cli(monkeypatch, missing)
    with pytest.raises(ModuleNotFoundError) as result:
        cli.main(['--help'])
    assert result.value.name == missing


def test_home_still_renders_when_local_component_is_absent(settings, monkeypatch):
    unavailable_status(monkeypatch, ModuleNotFoundError(
        'Synthetic missing component', name='china_stock_sim.publishing'))
    result = project_status(settings)
    assert result['publication'] == {
        'state': 'NOT_CONFIGURED', 'configured': False, 'enabled': False,
        'reason': 'LOCAL_COMPONENT_NOT_INSTALLED',
    }
    html = render_home(result)
    assert 'forward_current/index.html' in html
    assert '云端尚未启用' in html
    assert {row['id'] for row in result['nodes']} >= {'data', 'settlement', 'next_plan'}


@pytest.mark.parametrize('error', [
    ModuleNotFoundError('Synthetic missing child', name='china_stock_sim.publishing.state'),
    ModuleNotFoundError('Synthetic missing dependency', name='installed_dependency'),
    ImportError('Synthetic incompatible component'),
])
def test_home_reports_installed_import_failure_as_unknown(settings, monkeypatch, error):
    unavailable_status(monkeypatch, error)
    assert _publication(settings, datetime.fromisoformat('2026-11-05T12:00:00+08:00')) == {
        'state': 'UNKNOWN', 'reason': 'LOCAL_COMPONENT_IMPORT_FAILED',
    }


def test_installed_local_component_keeps_existing_cli_commands(capsys):
    if find_spec('china_stock_sim.publishing') is None:
        pytest.skip('Local publishing component is not installed')
    with pytest.raises(SystemExit) as result:
        cli.main(['--help'])
    assert result.value.code == 0
    output = capsys.readouterr().out
    for command in ('dashboard-sync', 'dashboard-sync-status', 'dashboard-sync-install',
                    'dashboard-sync-uninstall', 'dashboard-sync-render', 'dashboard-export'):
        assert command in output
