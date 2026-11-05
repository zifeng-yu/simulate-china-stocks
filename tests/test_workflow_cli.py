"""Command boundaries, legal switches and configuration rejection before IO."""

import json

import pytest
from execution_fixtures import execution_book
from test_strategy_acceptance import strategy_world, tree_state
from test_workflow_application import application

from china_stock_sim.application.workflow import WorkflowApplication
from china_stock_sim.cli import main
from china_stock_sim.config import load_settings

assert execution_book and strategy_world


def test_workflow_cli_inspection_is_read_only_and_disabled_runs_only_record_observations(strategy_world, capsys, monkeypatch):
    book, make_strategy, model = strategy_world
    app = WorkflowApplication(book.settings, market=book.market, ledger=book,
        strategy=make_strategy(), clock=lambda: book.test_now[0], test_mode=True)
    monkeypatch.setattr('china_stock_sim.application.workflow_cli.WorkflowApplication', lambda settings: app)
    before = tree_state(book.root)
    assert main(['--root', str(book.root), 'workflow-inspect']) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected['read_only'] is True
    assert inspected['configuration']['scheduled_enabled'] is False
    assert tree_state(book.root) == before
    assert main(['--root', str(book.root), 'workflow-run', '--download', '--scheduled']) == 0
    assert json.loads(capsys.readouterr().out)['state'] == 'DISABLED'
    assert main(['--root', str(book.root), 'workflow-run']) == 0
    assert json.loads(capsys.readouterr().out)['state'] == 'DISABLED'
    after = tree_state(book.root)
    assert all(after[path] == value for path, value in before.items())
    allowed = ('logs/runtime', 'data/normalized/runtime-evidence')
    assert all(any(path == prefix or path.startswith(prefix + '/') for prefix in allowed)
               or path in {'logs', 'data', 'data/normalized'} and after[path] == 'directory'
               for path in after.keys() - before.keys())
    records = list((book.root / 'logs/runtime').glob('*/*.jsonl'))
    assert len(records) == 2
    assert all(json.loads(path.read_text().splitlines()[-1])['result_state'] == 'DISABLED'
               for path in records)
    assert not model.calls


def test_workflow_cli_explicit_run_uses_same_application_and_public_ledger(strategy_world, monkeypatch, capsys):
    book, app, model = application(strategy_world)
    # A controlled clock/model is available only in this isolated application's fixture.
    monkeypatch.setattr('china_stock_sim.application.workflow_cli.WorkflowApplication', lambda settings: app)
    assert main(['--root', str(book.root), 'workflow-run']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['state'] == 'COMPLETE'
    assert book.get_plan('LLM', target_session='2026-10-30')
    assert len(model.calls) == 1
    assert main(['--root', str(book.root), 'workflow-run']) == 0
    capsys.readouterr()
    assert len(model.calls) == 1


def test_clock_and_backdated_session_are_not_cli_overrides(strategy_world):
    book, _, model = strategy_world
    before = tree_state(book.root)
    for option in ('--now', '--as-of', '--target-session'):
        with pytest.raises(SystemExit) as error:
            main(['--root', str(book.root), 'workflow-run', option, '2026-10-01'])
        assert error.value.code == 2
    assert tree_state(book.root) == before
    assert not model.calls


def test_legal_enabled_configuration_does_not_itself_install_or_activate(project):
    path = project / 'configs/default.toml'
    path.write_text(path.read_text().replace('enabled = false', 'enabled = true'))
    before = tree_state(project)
    settings = load_settings(project)
    assert settings.raw['schedule']['enabled'] is True
    assert settings.raw['execution']['enabled'] is True
    assert tree_state(project) == before
    assert not settings.database.exists()


@pytest.mark.parametrize('old,new', [
    ('daily_start = "18:10"', 'daily_start = "17:00"'),
    ('plan_cutoff = "09:00"', 'plan_cutoff = "09:01"'),
    ('awake_check_minutes = 15', 'awake_check_minutes = 0'),
    ('awake_check_minutes = 15', 'awake_check_minutes = true'),
    ('max_sessions_per_run = 20', 'max_sessions_per_run = 501'),
    ('max_run_seconds = 3600', 'max_run_seconds = 0'),
    ('retry_minutes = 15', 'retry_minutes = 1.5'),
    ('enabled = false', 'enabled = "false"'),
])
def test_invalid_workflow_configuration_rejected_without_runtime_write(project, old, new):
    path = project / 'configs/default.toml'
    path.write_text(path.read_text().replace(old, new, 1))
    before = tree_state(project)
    with pytest.raises(ValueError):
        load_settings(project)
    assert tree_state(project) == before
