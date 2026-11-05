"""Explicit macOS-only OS acceptance probe; never collected by normal pytest.

This entry point creates only a disposable project below the current project's
.cache, substitutes a no-network journal probe, and always unloads its own label.
It does not install the production workflow or use a production ledger.
"""

import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from china_stock_sim.config import load_settings
from china_stock_sim.scheduler import launchd
from china_stock_sim.scheduler.state import WorkflowJournal

ROOT = Path(__file__).resolve().parents[1]
PROBE = '''from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import os
import socket
from china_stock_sim.config import load_settings
from china_stock_sim.scheduler.state import WorkflowJournal
from china_stock_sim.scheduler.workspace import writer_lock
socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('NETWORK_FORBIDDEN'))
root = Path(__file__).resolve().parent
settings = load_settings(root)
journal = WorkflowJournal(settings)
now = lambda: datetime.now(ZoneInfo('Asia/Shanghai'))
with writer_lock(settings):
    journal.start('launchd-probe', now=now(), metadata={'isolated_no_network_probe': True})
    marker = root / 'interrupt-once'
    if marker.exists():
        marker.unlink()
        (root / 'interrupted').write_text('process exited before finish')
        os._exit(7)
    if not journal.get_task('safe-counter'):
        (root / 'counter').write_text('1')
        journal.save_task('safe-counter', now=now(), state='COMPLETE', result={'counter': 1})
    journal.step('launchd-probe', 'safe-counter', now=now(), state='COMPLETE', result={'counter': 1})
    journal.finish('launchd-probe', now=now(), state='COMPLETE', result={'counter': 1})
'''


def hashes():
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [ROOT / 'data/state/ledger.sqlite3', ROOT / 'data/state/market.sqlite3']
            if p.exists()}


def wait_for(predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError('isolated launchd probe did not reach expected state within deadline')


def main():
    before = hashes()
    cache = ROOT / '.cache'
    cache.mkdir(exist_ok=True)
    isolated = Path(tempfile.mkdtemp(prefix='scheduler-launchd-acceptance-', dir=cache)).resolve()
    record = {'record_type': 'ISOLATED_LAUNCHD_ACCEPTANCE', 'state': 'RUNNING',
              'started_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
              'isolated_root': str(isolated), 'production_database_before': before,
              'production_scheduler_installed': False, 'real_collection': False,
              'real_model': False, 'physical_sleep_or_reboot_tested': False,
              'probe_sha256': hashlib.sha256(PROBE.encode()).hexdigest(),
              'isolated_probe_throttle_seconds': 1,
              'production_throttle_seconds': 60,
              'prior_attempts': ['logs/workflow-implementation-2026-11-01-launchd-attempt-1.json',
                                 'logs/workflow-implementation-2026-11-01-launchd-attempt-2.json']}
    original_render = launchd.render
    settings, options = None, None
    try:
        (isolated / 'configs').mkdir()
        shutil.copyfile(ROOT / 'configs/default.toml', isolated / 'configs/default.toml')
        (isolated / '.venv/bin').mkdir(parents=True)
        (isolated / '.venv/bin/python').symlink_to(ROOT / '.venv/bin/python')
        (isolated / 'probe.py').write_text(PROBE)
        settings = load_settings(isolated)
        raw = deepcopy(settings.raw)
        raw['schedule']['enabled'] = True
        settings = replace(settings, raw=raw)

        def probe_render(value):
            rendered = original_render(value)
            plist = deepcopy(rendered['plist'])
            plist['ProgramArguments'] = [str(ROOT / '.venv/bin/python'), str(isolated / 'probe.py')]
            plist['ThrottleInterval'] = 1  # short isolated probe only; production remains 60
            content = plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=True)
            return {**rendered, 'plist': plist, 'content': content.decode(),
                    'sha256': hashlib.sha256(content).hexdigest()}

        launchd.render = probe_render
        options = {'agent_directory': isolated / 'deploy/launchd', 'test_mode': True}
        installation = launchd.install(settings, **options)
        record.update(label=installation['label'], installation=installation)
        assert installation['state'] == 'INSTALLED', installation
        journal = WorkflowJournal(settings)

        def complete(count):
            row = journal.read()['runs'].get('launchd-probe')
            return row and len(row['attempts']) == count and row['state'] == 'COMPLETE'

        wait_for(lambda: complete(1))
        record['run_at_load_passed'] = True
        service = f'gui/{os.getuid()}/{installation["label"]}'

        def trigger():
            result = subprocess.run(['/bin/launchctl', 'kickstart', service],
                capture_output=True, text=True, timeout=15, check=False)
            assert result.returncode == 0, 'isolated launchctl kickstart failed'

        trigger()
        wait_for(lambda: complete(2))
        assert (isolated / 'counter').read_text() == '1'
        assert len(journal.get_task('safe-counter')['history']) == 1
        record['repeated_trigger_durable_task_not_reapplied'] = True
        (isolated / 'interrupt-once').write_text('interrupt')
        trigger()
        wait_for(lambda: (isolated / 'interrupted').exists())
        wait_for(lambda: len(journal.read()['runs']['launchd-probe']['attempts']) == 3)
        # The exited probe leaves RUNNING durably. The next actual launch recovers it.
        assert journal.read()['state'] == 'RUNNING'
        trigger()
        wait_for(lambda: complete(4))
        attempts = journal.read()['runs']['launchd-probe']['attempts']
        assert [a['state'] for a in attempts] == ['COMPLETE', 'COMPLETE', 'INTERRUPTED', 'COMPLETE']
        assert (isolated / 'counter').read_text() == '1'
        record.update(interrupted_attempt_recovery_passed=True,
                      attempt_states=[a['state'] for a in attempts],
                      process_ids=[a['pid'] for a in attempts], durable_counter=1,
                      journal_sha256=hashlib.sha256(journal.path.read_bytes()).hexdigest(), state='PASS')
    except Exception as exc:
        record.update(state='FAILED', error_type=type(exc).__name__, reason=str(exc))
    finally:
        if settings is not None and options is not None:
            try:
                removal = launchd.uninstall(settings, **options)
                record['uninstallation'] = removal
                assert removal['loaded'] is False and not removal['installed'], removal
                record['isolated_service_unloaded'] = True
            except Exception as exc:
                record.update(state='FAILED', isolated_service_unloaded=False,
                              cleanup_error_type=type(exc).__name__, cleanup_reason=str(exc))
        launchd.render = original_render
        record['production_database_after'] = hashes()
        record['production_database_unchanged'] = record['production_database_after'] == before
        if not record['production_database_unchanged']:
            record['state'] = 'FAILED'
        if record.get('isolated_service_unloaded'):
            shutil.rmtree(isolated)
            record['isolated_root_removed'] = True
        record['tested_scheduler_sources'] = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((ROOT / 'src/china_stock_sim/scheduler').glob('*.py'))}
        record['acceptance_script_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        record['finished_at'] = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
        output = ROOT / 'logs/workflow-implementation-2026-11-01-launchd-verification.json'
        output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({'state': record['state'], 'record': str(output),
                          'isolated_service_unloaded': record.get('isolated_service_unloaded'),
                          'production_database_unchanged': record['production_database_unchanged']},
                         ensure_ascii=False))
    return 0 if record['state'] == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
