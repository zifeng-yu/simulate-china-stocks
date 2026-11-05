"""Publish the status homepage without entering the financial workflow."""

import fcntl
import hashlib
import json
import subprocess
import tomllib

from china_stock_sim.common.fees import aware_time
from china_stock_sim.config import local_path
from china_stock_sim.market.storage import atomic_text
from china_stock_sim.reporting.run_home import render_home


def _owned(settings, relative):
    path = settings.root / relative
    for candidate in (path, *path.parents):
        if candidate == settings.root:
            break
        if candidate.is_symlink():
            raise ValueError('首页发布路径不接受符号链接')
    return local_path(settings.root, relative)


def _open(home):
    try:
        return subprocess.run(['/usr/bin/open', str(home)], capture_output=True,
                              timeout=10, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def dashboard_settings(settings):
    path = _owned(settings, 'configs/dashboard.toml')
    with path.open('rb') as stream:
        raw = tomllib.load(stream)
    expected = {'schema_version', 'enabled', 'refresh_minutes',
                'browser_refresh_seconds', 'stale_after_seconds'}
    if set(raw) != expected or type(raw['enabled']) is not bool:
        raise ValueError('首页配置字段或启用类型无效')
    for key, value in {'schema_version': 1, 'refresh_minutes': 15,
                       'browser_refresh_seconds': 60, 'stale_after_seconds': 1800}.items():
        if type(raw[key]) is not int or raw[key] != value:
            raise ValueError('首页配置须符合现行刷新日程: ' + key)
    return raw


def refresh_dashboard(settings, *, open_page=False, builder=None):
    from china_stock_sim.scheduler.status import build_status

    config = dashboard_settings(settings)
    if not config['enabled']:
        return {'state': 'DISABLED', 'reason': 'DASHBOARD_DISABLED'}
    home = _owned(settings, 'reports/index.html')
    lock = _owned(settings, 'data/state/dashboard-refresh.lock')
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('a') as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'state': 'BUSY', 'reason': 'DASHBOARD_REFRESH_IN_PROGRESS'}
        try:
            value = (builder or build_status)(settings)
            sampled = aware_time(value['sampled_at'])
            if home.is_file():
                import re
                match = re.search(r'<script type="application/json" id="run-home-data">(.*?)</script>',
                                  home.read_text(), re.DOTALL)
                if match and aware_time(json.loads(match[1])['status']['sampled_at']) > sampled:
                    return {'state': 'SKIPPED', 'reason': 'OLDER_SAMPLE', 'home': str(home)}
            value = dict(value)
            value['publication_id'] = hashlib.sha256(json.dumps(value, sort_keys=True,
                ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
            payload = json.dumps(value, ensure_ascii=False, indent=2) + '\n'
            html = render_home(value)
            output = _owned(settings, 'reports/dashboard-snapshots/' + value['publication_id'] + '.json')
            # HTML is the single commit point. Its matching JSON is immutable and
            # already complete before this generation can become visible.
            atomic_text(output, payload)
            atomic_text(home, html)
        except Exception:
            if open_page and home.is_file():
                return {'state': 'REFRESH_FAILED', 'reason': 'LAST_COMPLETE_PAGE_RETAINED',
                        'opened': _open(home), 'home': str(home)}
            raise
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    result = {'state': 'DASHBOARD_PUBLISHED', 'sampled_at': value.get('sampled_at'),
              'home': str(home), 'status': str(output), 'bytes': home.stat().st_size,
              'business_writes': False, 'network_requests': False}
    if open_page:
        result['opened'] = _open(home)
    return result
