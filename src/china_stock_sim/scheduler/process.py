"""Bounded, read-only process identity checks for operational display."""

import os
import subprocess


def probe_process(pid):
    """Do not confuse a reused PID with the original workflow process."""
    if type(pid) is not int or pid <= 0:
        return {'state': 'UNKNOWN', 'birth': None}
    try:
        reply = subprocess.run(
            ['/bin/ps', '-p', str(pid), '-o', 'lstart='], capture_output=True,
            text=True, timeout=3, check=False,
            env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'TZ': 'UTC'},
        )
    except (OSError, subprocess.SubprocessError):
        return {'state': 'UNKNOWN', 'birth': None}
    birth = ' '.join(reply.stdout.split())
    if reply.returncode == 0 and birth and len(birth) <= 80:
        return {'state': 'ALIVE', 'birth': birth}
    if reply.returncode == 1 and not birth:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return {'state': 'DEAD', 'birth': None}
        except (PermissionError, OSError):
            pass
    return {'state': 'UNKNOWN', 'birth': None}
