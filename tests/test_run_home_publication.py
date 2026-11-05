"""Homepage publication, safe rendering and independent service contracts."""

import fcntl
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from test_run_home_status import project_status
from test_scheduler import LaunchRunner, launch_settings, options

from china_stock_sim.reporting.run_home import render_home
from china_stock_sim.scheduler import dashboard, dashboard_launchd, launchd
from china_stock_sim.scheduler.process import probe_process
from china_stock_sim.scheduler.workspace import writer_lock

assert launch_settings


@pytest.fixture
def configured(settings):
    source = Path(__file__).resolve().parents[1] / 'configs/dashboard.toml'
    (settings.root / 'configs/dashboard.toml').write_bytes(source.read_bytes())
    return settings


def embedded(html):
    return json.loads(re.search(r'id="run-home-data">(.*?)</script>', html, re.S)[1])['status']


def test_publication_is_same_generation_and_does_not_take_business_lock(configured):
    value = project_status(configured)
    with writer_lock(configured):
        result = dashboard.refresh_dashboard(configured, builder=lambda settings: value)
    page = Path(result['home']).read_text()
    assert embedded(page) == json.loads(Path(result['status']).read_text())
    assert Path(result['status']).stem == embedded(page)['publication_id']
    assert not configured.database.exists()
    assert not (configured.root / 'reports/status.json').exists()
    assert 'forward_current/index.html' in page and 'content="60"' in page


def test_commit_failure_preserves_old_page_and_its_snapshot(configured, monkeypatch):
    old = dashboard.refresh_dashboard(configured, builder=lambda s: project_status(s))
    before = Path(old['home']).read_bytes(), Path(old['status']).read_bytes()
    atomic = dashboard.atomic_text
    def fail(path, text):
        if path.name == 'index.html':
            raise OSError('disk failure')
        return atomic(path, text)
    monkeypatch.setattr(dashboard, 'atomic_text', fail)
    with pytest.raises(OSError):
        dashboard.refresh_dashboard(configured, builder=lambda s: project_status(
            s, now='2026-11-02T10:15:00+08:00'))
    assert before == (Path(old['home']).read_bytes(), Path(old['status']).read_bytes())


def test_old_sample_cannot_replace_newer_publication(configured):
    first = dashboard.refresh_dashboard(configured, builder=lambda s: project_status(s))
    original = Path(first['home']).read_bytes()
    result = dashboard.refresh_dashboard(configured, builder=lambda s: project_status(
        s, now='2026-11-02T09:59:59+08:00'))
    assert result['reason'] == 'OLDER_SAMPLE'
    assert Path(first['home']).read_bytes() == original


def test_refresh_lock_prevents_second_publisher(configured):
    path = configured.root / 'data/state/dashboard-refresh.lock'
    path.parent.mkdir(parents=True)
    with path.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert dashboard.refresh_dashboard(configured)['state'] == 'BUSY'
    assert not (configured.root / 'reports/index.html').exists()


@pytest.mark.parametrize('relative', ['reports', 'reports/index.html',
                                     'data/state/dashboard-refresh.lock'])
def test_publisher_rejects_even_in_project_symlinks(configured, relative):
    target = configured.root / 'target'
    target.mkdir()
    link = configured.root / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)
    with pytest.raises(ValueError, match='符号链接'):
        dashboard.refresh_dashboard(configured)


def test_failure_can_open_retained_complete_page_without_business_work(configured, monkeypatch):
    first = dashboard.refresh_dashboard(configured, builder=lambda s: project_status(s))
    calls = []
    monkeypatch.setattr(dashboard, '_open', lambda path: calls.append(path) or True)
    result = dashboard.refresh_dashboard(configured, open_page=True,
        builder=lambda s: (_ for _ in ()).throw(ValueError('bad source')))
    assert result['state'] == 'REFRESH_FAILED' and result['opened']
    assert calls == [Path(first['home'])] and not configured.database.exists()


def test_renderer_escapes_all_contexts_and_has_portable_responsive_navigation(configured):
    value = project_status(configured)
    payload = '</script><img src=x onerror=alert(1)>"'
    value['summary']['label'] = payload
    value['nodes'][0]['id'] = payload
    value['nodes'][0]['deadline_at'] = '2026-11-02T09:00:00+08:00'
    html = render_home(value)
    assert '<img src=x' not in html and '\\u003c/script' in html
    assert embedded(html)['summary']['label'] == payload
    assert '截止' in html and 'max-width:560px' in html
    assert 'D旧池维护' in html and '月末选池' in html and 'N新池' in html
    assert 'width=device-width' in html and 'focus-visible' in html
    assert '.account-row>div{min-width:0}' in html and 'strong{font-size:12px;overflow-wrap:anywhere}' in html
    cloud = render_home(value, scope='cloud', cloud_publication={
        'next_due_at': '2026-11-03T09:00:00+08:00'})
    assert 'href="trading.html"' in cloud and '云端每日 09:00、21:00 同步' in cloud
    assert 'sessionStorage' in cloud and 'fresh.scope===\'cloud\'' in cloud
    with pytest.raises(ValueError):
        render_home({**value, 'nodes': [{}] * 31})


def test_homepage_publication_text_has_both_times_and_latest_only_recovery(configured):
    value = project_status(configured)
    value['publication'] = {'enabled': True, 'configured': True,
                            'next_due_at': '2026-11-02T21:00:00+08:00'}
    local = render_home(value)
    cloud = render_home(value, scope='cloud', cloud_publication=value['publication'])
    assert '每日 09:00、21:00 自动推送' in local
    assert '每日 09:00、21:00 由本机上传' in cloud
    for page in (local, cloud):
        assert '只补最新已到时点一次' in page
        assert '2026-11-02T21:00:00+08:00' in page
        assert '今日计划截止' in page and '<div class="metric-value">09:00</div>' in page
        assert '补传当天一次' not in page


def test_monitor_launch_agent_is_independent_and_runtime_is_whitelisted(launch_settings):
    settings = launch_settings
    (settings.root / 'configs/dashboard.toml').write_bytes(
        (Path(__file__).resolve().parents[1] / 'configs/dashboard.toml').read_bytes())
    preview = dashboard_launchd.render(settings)
    assert preview['label'] != launchd.job_label(settings)
    assert 'StartInterval' not in preview['plist'] and preview['plist']['RunAtLoad']
    assert preview['plist']['StartCalendarInterval'] == [
        {'Minute': 0}, {'Minute': 15}, {'Minute': 30}, {'Minute': 45}]
    assert preview['plist']['ProgramArguments'][-2:] == ['dashboard-refresh', '--scheduled']
    runner = LaunchRunner()
    installed = dashboard_launchd.install(settings, **options(settings, runner))
    assert installed['installed'] and installed['loaded']
    assert dashboard_launchd.uninstall(settings, **options(settings, runner))['state'] == 'UNINSTALLED'
    fields = launchd.runtime_fields(subprocess.CompletedProcess([], 0, stdout=
        'state = not running\nlast exit code = 0\nenvironment = {SECRET=private}\n'))
    assert fields == {'state': 'IDLE', 'pid': None, 'last_exit_code': 0}


def test_calendar_and_long_stage_start_are_visible_and_cloud_elapsed_is_snapshot_time(configured):
    value = project_status(configured)
    value['current_run'] = {'state': 'RUNNING', 'phase': 'backup',
        'started_at': '2026-11-02T08:30:00+08:00', 'phase_started_at': '2026-11-02T09:45:00+08:00'}
    html = render_home(value, scope='cloud')
    assert '最近可处理交易日 D：2026-10-30' in html
    assert '对应计划日 N：2026-11-02' in html
    assert '当前阶段开始' in html and '当前阶段已持续' in html
    assert 'data-elapsed-from="2026-11-02T09:45:00+08:00"' in html
    assert '采样时运行' in html and "measureTime=fresh.scope==='cloud'?Date.parse(fresh.sampled_at):now" in html


def test_real_process_identity_and_exit():
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])
    try:
        live = probe_process(child.pid)
        assert live['state'] == 'ALIVE' and live['birth']
    finally:
        child.terminate()
        child.wait(timeout=5)
    assert probe_process(child.pid)['state'] == 'DEAD'

