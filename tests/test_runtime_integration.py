"""Public entrypoints retain behavior while adding isolated operational evidence."""

import json
from importlib.util import find_spec
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_strategy_acceptance import tree_state
from test_workflow_observation import observed_app

from china_stock_sim.cli import main
from china_stock_sim.market.repository import MarketRepository
from china_stock_sim.runtime.history import query_history
from china_stock_sim.runtime.recording import run_observed


def events(root):
    return [json.loads(line) for path in sorted((root / 'logs/runtime').glob('*/*.jsonl'))
            for line in path.read_text().splitlines()]


@pytest.mark.parametrize('command,handler,result,extra', [
    ('workflow-run', 'china_stock_sim.application.workflow_cli.execute',
     {'state': 'DISABLED', 'reason': 'SCHEDULE_DISABLED'}, ['--scheduled']),
    ('dashboard-refresh', 'china_stock_sim.scheduler.dashboard_cli.execute',
     {'state': 'BUSY', 'reason': 'DASHBOARD_REFRESH_IN_PROGRESS'}, ['--scheduled']),
    ('dashboard-sync', 'china_stock_sim.publishing.cli.execute',
     {'state': 'COMPLETE', 'changed': False, 'last_success_at': '2026-11-02T09:00:00+08:00'},
     ['--scheduled']),
])
def test_cli_records_all_three_invocations_without_changing_results(
        project, monkeypatch, capsys, command, handler, result, extra):
    if command == 'dashboard-sync' and find_spec('china_stock_sim.publishing') is None:
        pytest.skip('Local publishing component is not installed')
    monkeypatch.setattr(handler, lambda args, root: (result, 0))
    assert main(['--root', str(project), command, *extra]) == 0
    assert json.loads(capsys.readouterr().out) == result
    rows = events(project)
    assert rows[0]['event'] == 'START' and rows[-1]['event'] == 'END'
    assert rows[0]['scheduled'] is True
    assert rows[-1]['exit_code'] == 0 and rows[-1]['result_state'] == result['state']
    assert not list(project.glob('data/**/*.sqlite3'))
    assert not list(project.glob('data/plans/**/*'))


def test_execution_configuration_failure_is_recorded_before_main_handles_it(project, monkeypatch, capsys):
    def fail(args, root):
        raise ValueError('invalid configuration')
    monkeypatch.setattr('china_stock_sim.application.workflow_cli.execute', fail)
    assert main(['--root', str(project), 'workflow-run', '--scheduled']) == 2
    capsys.readouterr()
    rows = events(project)
    assert [row['event'] for row in rows] == ['START', 'END']
    assert rows[-1]['exit_code'] == 2
    assert 'invalid configuration' not in json.dumps(rows)


def test_history_cli_does_not_create_observability_state(project, capsys):
    before = tree_state(project)
    assert main(['--root', str(project), 'runtime-history']) == 0
    value = json.loads(capsys.readouterr().out)
    assert value['records'] == []
    assert tree_state(project) == before


def test_scheduled_home_archive_error_cannot_turn_publication_into_failure(project, monkeypatch, capsys):
    monkeypatch.setattr('china_stock_sim.scheduler.dashboard_cli.refresh_dashboard',
        lambda settings, **kw: {'state': 'DASHBOARD_PUBLISHED', 'business_writes': False})
    def failed(root):
        raise OSError('secret endpoint in IO failure')
    monkeypatch.setattr('china_stock_sim.scheduler.dashboard_cli.archive_due', failed)
    assert main(['--root', str(project), 'dashboard-refresh', '--scheduled']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['state'] == 'DASHBOARD_PUBLISHED'
    assert result['runtime_archive']['state'] == 'FAILED'
    assert 'secret endpoint' not in json.dumps(events(project))


def test_workflow_stage_records_resource_and_decision_identity_without_changing_journal(settings):
    app = observed_app(settings)
    result = {'state': 'COMPLETE', 'decision_id': 'decision:observed'}
    actual, code = run_observed(settings.root, 'workflow', lambda: (
        app._observe('plan', lambda: result, account_id='S1'), 0))
    assert actual is result and code == 0
    history = query_history(settings.root, account_id='S1', decision_id='decision:observed')
    assert len(history['records']) == 1
    phases = app.journal.read()['runs'][app.run_id]['attempts'][-1]['phases']
    assert [(p['phase'], p['state']) for p in phases] == [('plan', 'RUNNING'), ('plan', 'COMPLETE')]
    assert not settings.database.exists()


def test_request_events_preserve_latest_projection_but_retain_transitions(project):
    repository = MarketRepository(project)
    def action():
        for index, state in enumerate(['RUNNING', 'FAILED', 'OK'], 1):
            repository.save_state('requests', 'a' * 64, {'state': state, 'attempts': index,
                'identity': {'source': 'baostock', 'query': 'history',
                             'params': {'secret': 'not-for-operational-log'}},
                'category': 'NETWORK', 'error': 'password=do-not-copy',
                'started_at': '2026-11-02T09:00:00+08:00'})
        return {'state': 'COMPLETE'}, 0
    run_observed(project, 'workflow', action)
    rows = [row for row in events(project) if row.get('kind') == 'data_request']
    assert [row['status'] for row in rows] == ['RUNNING', 'FAILED', 'OK']
    assert repository.states('requests')['a' * 64]['state'] == 'OK'
    assert 'do-not-copy' not in json.dumps(events(project))
    assert 'not-for-operational-log' not in json.dumps(events(project))


def test_home_launchagent_explicitly_marks_scheduled_refresh(settings):
    from china_stock_sim.scheduler.dashboard_launchd import render
    source = Path(__file__).resolve().parents[1] / 'configs/dashboard.toml'
    (settings.root / 'configs/dashboard.toml').write_bytes(source.read_bytes())
    arguments = render(settings)['plist']['ProgramArguments']
    assert arguments[-2:] == ['dashboard-refresh', '--scheduled']
    assert '--republish' not in arguments


@pytest.mark.parametrize('state', ['BUSY', 'WAITING_MANUAL', 'PARTIAL', 'FAILED'])
def test_explicit_archive_reports_incomplete_exit_code(project, monkeypatch, capsys, state):
    monkeypatch.setattr('china_stock_sim.runtime.cli.archive_runtime',
                        lambda root, **kw: {'state': state})
    assert main(['--root', str(project), 'runtime-archive', '--date', '2026-11-02']) == 2
    assert json.loads(capsys.readouterr().out)['state'] == state


def test_upload_release_and_noop_keep_stable_references_without_new_transfer(project, monkeypatch):
    if find_spec('china_stock_sim.publishing') is None:
        pytest.skip('Local publishing component is not installed')
    from china_stock_sim.publishing.common import clock, digest, encode
    from china_stock_sim.publishing.sync import _completed, sync_dashboard

    settings = SimpleNamespace(root=project, target_id='a' * 64, enabled=True,
                               configured=True, target_type='local')
    state = {'schema_version': 1, 'targets': {}}
    manifest = {'scheduled_for': '2026-11-02', 'published_at': '2026-11-02T09:00:00+08:00',
                'source_sampled_at': '2026-11-02T08:59:00+08:00'}
    run_observed(project, 'upload', lambda: (_completed(project, state, settings, manifest,
                                                     clock(manifest['published_at'])), 0))
    monkeypatch.setattr('china_stock_sim.publishing.sync.load_publishing_settings', lambda root: settings)
    class UnusedTransport:
        def current_manifest(self):
            pytest.fail('A completed-day noop must not access the transport')
    result, code = run_observed(project, 'upload', lambda: (sync_dashboard(
        project, now=clock('2026-11-02T12:00:00+08:00'), transport=UnusedTransport()), 0))
    assert result['changed'] is False and code == 0
    rows = [r for r in events(project) if r.get('kind') in {'upload_state', 'upload_release'}]
    assert len(rows) == 2
    assert {r['target_id'] for r in rows} == {'a' * 64}
    assert {r['release_id'] for r in rows} == {digest(encode(manifest))}
