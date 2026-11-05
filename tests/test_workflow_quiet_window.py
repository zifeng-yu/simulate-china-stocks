"""Automatic trigger deferral must never consume business work or a plan window."""

import json
from datetime import datetime

import pytest

from china_stock_sim.application.workflow import WorkflowApplication
from china_stock_sim.cli import main
from china_stock_sim.config import load_settings
from china_stock_sim.scheduler.planning import automatic_window, plan_schedule

WINDOW = {'quiet_start': '17:00', 'quiet_end': '19:00'}


@pytest.mark.parametrize('instant,quiet', [
    ('2026-11-03T16:59:59+08:00', False),
    ('2026-11-03T17:00:00+08:00', True),
    ('2026-11-03T18:10:00+08:00', True),
    ('2026-11-03T18:59:59+08:00', True),
    ('2026-11-03T19:00:00+08:00', False),
    ('2026-11-03T19:10:00+08:00', False),
    ('2026-11-04T00:00:00+08:00', False),
    ('2026-11-03T10:10:00+00:00', True),
    ('2026-11-03T11:00:00+00:00', False),
])
def test_shanghai_half_open_window(instant, quiet):
    value = automatic_window(instant, WINDOW)
    assert value['quiet'] is quiet
    if quiet:
        assert value['next_allowed_at'] == '2026-11-03T19:00:00+08:00'


def test_quiet_trigger_never_reads_business_or_acquires_lock(settings, monkeypatch):
    settings.raw['schedule']['enabled'] = True
    now = [datetime.fromisoformat('2026-11-03T17:00:00+08:00')]
    app = WorkflowApplication(settings, clock=lambda: now[0], test_mode=True)
    def forbidden(*args, **kwargs):
        pytest.fail('quiet trigger attempted business work')
    monkeypatch.setattr('china_stock_sim.application.workflow.writer_lock', forbidden)
    monkeypatch.setattr(app, '_accounts', forbidden)
    monkeypatch.setattr(app, '_schedule', forbidden)
    monkeypatch.setattr(app, '_run', forbidden)
    before = {str(p.relative_to(settings.root)): p.read_bytes()
              for p in settings.root.rglob('*') if p.is_file()}
    for at in ('17:00:00', '18:10:00', '18:59:59'):
        now[0] = datetime.fromisoformat('2026-11-03T' + at + '+08:00')
        assert app.run(download=True, scheduled=True) == {
            'state': 'SKIPPED', 'reason': 'SCHEDULE_QUIET_WINDOW',
            'next_retry_at': '2026-11-03T19:00:00+08:00', 'changed': False}
    assert {str(p.relative_to(settings.root)): p.read_bytes()
            for p in settings.root.rglob('*') if p.is_file()} == before


def test_skipped_cli_has_zero_exit_and_records_reason_only(settings, monkeypatch, capsys):
    settings.raw['schedule']['enabled'] = True
    app = WorkflowApplication(settings,
        clock=lambda: datetime.fromisoformat('2026-11-03T18:10:00+08:00'), test_mode=True)
    monkeypatch.setattr('china_stock_sim.application.workflow_cli.WorkflowApplication', lambda _: app)
    assert main(['--root', str(settings.root), 'workflow-run', '--download', '--scheduled']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['reason'] == 'SCHEDULE_QUIET_WINDOW'
    assert not settings.database.exists()
    assert not app.journal.path.exists()
    records = list((settings.root / 'logs/runtime').glob('*/*.jsonl'))
    assert len(records) == 1
    end = json.loads(records[0].read_text().splitlines()[-1])
    assert end['result_state'] == 'SKIPPED' and end['exit_code'] == 0
    assert end['result']['reason'] == 'SCHEDULE_QUIET_WINDOW'
    assert end['result']['next_retry_at'] == '2026-11-03T19:00:00+08:00'


@pytest.mark.parametrize('at,scheduled', [('19:00:00', True), ('19:10:00', True),
                                        ('18:10:00', False)])
def test_resume_and_explicit_maintenance_preserve_business_day(settings, monkeypatch, at, scheduled):
    settings.raw['schedule']['enabled'] = settings.raw['market']['enabled'] = True
    now = datetime.fromisoformat('2026-11-03T' + at + '+08:00')
    app = WorkflowApplication(settings, clock=lambda: now, test_mode=True)
    calendar = [{'date': f'2026-11-0{i}', 'is_trading_day': '1'} for i in range(2, 6)]
    calls = []
    def run(download):
        calls.append(download)
        plan = plan_schedule(now, calendar)
        assert plan['completed_session'] == '2026-11-03'
        assert plan['planning_target'] == '2026-11-04' and plan['plan_allowed']
        return {'state': 'COMPLETE'}
    monkeypatch.setattr(app, '_run', run)
    assert app.run(download=True, scheduled=scheduled)['state'] == 'COMPLETE'
    assert calls == [True]
    assert app.journal.read_current()['task_count'] == 0


@pytest.mark.parametrize('replacement', ['quiet_start = "19:00"', 'quiet_start = "9:00"',
                                         'quiet_start = true'])
def test_invalid_quiet_configuration_fails_before_writes(project, replacement):
    path = project / 'configs/default.toml'
    path.write_text(path.read_text().replace('quiet_start = "17:00"', replacement))
    with pytest.raises(ValueError):
        load_settings(project)
    assert not (project / 'data').exists()
