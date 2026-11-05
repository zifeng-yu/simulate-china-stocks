"""Fail-open, bounded operational events around the three existing task invocations."""

import contextvars
import hashlib
import json
import math
import os
import platform
import re
import shutil
import sys
import threading
import time
import unicodedata
import uuid
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from china_stock_sim.scheduler.process import probe_process

try:
    import resource
except ImportError:  # Resource observations are optional, never an execution prerequisite.
    resource = None

SHANGHAI = ZoneInfo('Asia/Shanghai')
TASKS = frozenset({'workflow', 'homepage', 'upload'})
MAX_EVENT_BYTES = 16_384
_CURRENT = contextvars.ContextVar('runtime_recorder', default=None)
_IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z')
_CODE = re.compile(r'[A-Z][A-Z0-9_]{0,79}\Z')
_SENSITIVE = re.compile(r'password|secret|token|api.?key|authorization|credential|bearer', re.I)
_IDS = frozenset({'run_id', 'account_id', 'decision_id', 'publication_id', 'request_id',
                  'plan_id', 'backup_id', 'target_id', 'release_id'})
_TIMES = frozenset({'next_retry_at', 'server_retry_at', 'started_at', 'finished_at',
                    'observed_at', 'last_success_at', 'source_sampled_at', 'next_due_at',
                    'published_at', 'last_success_slot'})
_NUMBERS = frozenset({'attempts', 'retry_failures'})
_DATES = frozenset({'session', 'target_session', 'scheduled_for'})
_CODES = frozenset({'state', 'status', 'error_code', 'last_error', 'reason'})
_LABELS = frozenset({'source', 'category'})
_FLAGS = frozenset({'changed', 'recovered', 'republished', 'budget_exhausted'})
_EXCEPTION_SUMMARIES = {
    'OverflowError': 'NUMERIC_OR_RECORD_SIZE_LIMIT', 'MemoryError': 'MEMORY_LIMIT',
    'TimeoutError': 'OPERATION_TIMEOUT', 'PermissionError': 'PERMISSION_DENIED',
    'FileNotFoundError': 'FILE_NOT_FOUND', 'KeyboardInterrupt': 'USER_INTERRUPTED',
    'SystemExit': 'PROCESS_EXIT', 'ValueError': 'INVALID_VALUE',
    'OSError': 'OPERATING_SYSTEM_ERROR', 'RuntimeError': 'RUNTIME_ERROR',
}


def safe_exception(value):
    """Validate a bounded diagnostic; never accept arbitrary messages or frame locals."""
    if not isinstance(value, dict) or not isinstance(value.get('chain'), list):
        return None
    chain, remaining = [], 24
    for item in value['chain'][:6]:
        if not isinstance(item, dict):
            continue
        name = item.get('type')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', name):
            name = 'Exception'
        if _SENSITIVE.search(name):
            name = 'Exception'
        frames = []
        supplied = item.get('frames', [])
        for frame in supplied[:24] if isinstance(supplied, list) else []:
            if not isinstance(frame, dict) or remaining <= 0:
                break
            file, function, line = (frame.get(k) for k in ('file', 'function', 'line'))
            if (not isinstance(file, str) or len(file) > 160
                    or not re.fullmatch(r'[A-Za-z0-9_./-]+\.py', file)
                    or Path(file).is_absolute() or '..' in Path(file).parts
                    or _SENSITIVE.search(file)
                    or not isinstance(function, str) or _SENSITIVE.search(function)
                    or not re.fullmatch(r'(?:[A-Za-z_][A-Za-z0-9_]{0,79}|<module>|<lambda>)', function)
                    or type(line) is not int or not 0 < line < 10_000_000):
                continue
            frames.append({'file': file, 'function': function, 'line': line})
            remaining -= 1
        chain.append({'type': name, 'summary': _EXCEPTION_SUMMARIES.get(name, 'EXCEPTION_RAISED'),
                      'relation': item.get('relation') if item.get('relation') in {
                          'raised', 'cause', 'context'} else 'raised', 'frames': frames})
    body = {'chain': chain, 'truncated': value.get('truncated') is True,
            'message_policy': 'TYPE_SUMMARY_ONLY_NO_ARGS_LOCALS_OR_SOURCE'}
    body['fingerprint'] = hashlib.sha256(json.dumps(body, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()
    return body


def _exception_details(exc, root):
    chain, seen, remaining = [], set(), 24
    relation, truncated = 'raised', False
    while isinstance(exc, BaseException) and id(exc) not in seen and len(chain) < 6:
        seen.add(id(exc))
        frames, trace = [], exc.__traceback__
        while trace is not None:
            if remaining <= 0:
                truncated = True
                break
            try:
                file = Path(trace.tb_frame.f_code.co_filename).resolve().relative_to(root).as_posix()
                frames.append({'file': file, 'function': trace.tb_frame.f_code.co_name,
                               'line': trace.tb_lineno})
                remaining -= 1
            except (OSError, ValueError):
                pass
            trace = trace.tb_next
        chain.append({'type': type(exc).__name__, 'relation': relation, 'frames': frames})
        if exc.__cause__ is not None:
            exc, relation = exc.__cause__, 'cause'
        elif not exc.__suppress_context__:
            exc, relation = exc.__context__, 'context'
        else:
            exc = None
    return safe_exception({'chain': chain, 'truncated': truncated or exc is not None})


def _exception(exc, root):
    try:
        return _exception_details(exc, root)
    except Exception:
        return safe_exception({'chain': [{'type': 'Exception', 'frames': []}], 'truncated': True})


def _now():
    return datetime.now(SHANGHAI).isoformat()


def _number(value):
    try:
        return (not isinstance(value, bool) and isinstance(value, (int, float))
                and math.isfinite(value) and value >= 0)
    except (OverflowError, ValueError):
        return False


def _identifier(value):
    return (isinstance(value, str) and _IDENTIFIER.fullmatch(value)
            and not _SENSITIVE.search(value))


def _account_identifier(value):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= 256
            and not _SENSITIVE.search(value)
            and all(unicodedata.category(char)[0] in {'L', 'N', 'M'}
                    or char in ' _.-:' for char in value))


def _field_identity(key, value):
    if key in {'target_id', 'release_id'}:
        return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value)
    return _account_identifier(value) if key == 'account_id' else _identifier(value)


def _code(value):
    return isinstance(value, str) and _CODE.fullmatch(value) and not _SENSITIVE.search(value)


def _instant(value):
    if not isinstance(value, str) or len(value) > 40:
        return False
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.tzinfo is not None and parsed.utcoffset() is not None
    except (ValueError, TypeError, OverflowError):
        return False


def _day(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def safe_fields(fields, root=None):
    """Only public scalar identities/codes; never arbitrary result bodies or exception text."""
    result = {}
    known = _IDS | _TIMES | _NUMBERS | _DATES | _CODES | _LABELS | _FLAGS
    for key, value in fields.items():
        if key in known and value is None:
            result[key] = None  # Explicit unknown/not-applicable is not missing evidence.
        elif key in _IDS | _LABELS and _field_identity(key, value):
            result[key] = value
        elif key in _TIMES and _instant(value):
            result[key] = value
        elif key == 'scheduled_for' and _instant(value):
            # Current publishing uses a slot instant; historical events used a day.
            result[key] = value
        elif key in _DATES and _day(value):
            result[key] = value
        elif key in _NUMBERS and type(value) is int and 0 <= value <= 1_000_000_000:
            result[key] = value
        elif key in _FLAGS and type(value) is bool:
            result[key] = value
        elif key in _CODES and (value is None or _code(value)):
            result[key] = value
        elif key == 'status' and isinstance(value, str) and root is not None:
            # Homepage success links its immutable sample, never an arbitrary local path.
            path = Path(value)
            if path.is_absolute():
                try:
                    path = path.relative_to(root)
                except ValueError:
                    continue
            relative = path.as_posix()
            if re.fullmatch(r'reports/dashboard-snapshots/[0-9a-f]{64}\.json', relative):
                result['status'] = relative
    decision = fields.get('decision')
    if 'decision_id' not in result and isinstance(decision, dict):
        identity = decision.get('decision_id')
        if (identity is None and 'decision_id' in decision) or _identifier(identity):
            result['decision_id'] = identity
    omitted = {key for key in fields if key in known and key not in result}
    if isinstance(decision, dict) and 'decision_id' in decision and 'decision_id' not in result:
        omitted.add('decision_id')
    if isinstance(fields.get('omitted_fields'), list):
        omitted.update(key for key in fields['omitted_fields'] if isinstance(key, str) and key in known)
    if omitted:
        result.update(omitted_fields=sorted(omitted), omitted_field_count=len(omitted))
    return result


def _safe_metrics(value):
    if not isinstance(value, dict):
        return {}
    numeric = {'elapsed_seconds', 'cpu_user_seconds', 'cpu_system_seconds',
               'child_cpu_user_seconds', 'child_cpu_system_seconds', 'cpu_percent_one_core',
               'peak_rss_bytes', 'disk_free_bytes'}
    result = {key: v for key, v in value.items() if key in numeric
              and (v is None or _number(v))}
    result['peak_rss_scope'] = 'PROCESS_LIFETIME_NOT_INTERVAL_DELTA'
    return result


def _root(path):
    """Resolve the explicit project root, but never follow links below that boundary."""
    return Path(path).resolve(strict=True)


def owned(root, relative):
    root = _root(root)
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('RUNTIME_UNSAFE_PATH')
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('RUNTIME_UNSAFE_PATH')
    return current


def _error():
    try:
        sys.stderr.write('RUNTIME_RECORDING_UNAVAILABLE\n')
        sys.stderr.flush()
    except Exception:
        pass


def _rss_bytes(value, system):
    if not _number(value):
        return None
    if system == 'Darwin':
        return int(value)
    if system == 'Linux':
        return int(value * 1024)
    return None


def measurement():
    """Optional raw resource observation. No system-wide process or environment scan."""
    result = {'monotonic': None, 'cpu_user': None, 'cpu_system': None,
              'child_cpu_user': None, 'child_cpu_system': None,
              'peak_rss_bytes': None, 'disk_free_bytes': None}
    try:
        result['monotonic'] = time.monotonic()
    except Exception:
        pass
    if resource is not None:
        try:
            usage = resource.getrusage(resource.RUSAGE_SELF)
            result.update(cpu_user=usage.ru_utime, cpu_system=usage.ru_stime,
                          peak_rss_bytes=_rss_bytes(usage.ru_maxrss, platform.system()))
        except Exception:
            pass
        try:
            children = resource.getrusage(resource.RUSAGE_CHILDREN)
            result.update(child_cpu_user=children.ru_utime, child_cpu_system=children.ru_stime)
        except Exception:
            pass
    current = _CURRENT.get()
    if current is not None:
        try:
            result['disk_free_bytes'] = shutil.disk_usage(current.root).free
        except Exception:
            pass
    return result


def measure_since(start):
    start = start if isinstance(start, dict) else {}
    end = measurement()

    def delta(key):
        left, right = start.get(key), end.get(key)
        return round(right - left, 6) if _number(left) and _number(right) and right >= left else None

    elapsed, user, system = delta('monotonic'), delta('cpu_user'), delta('cpu_system')
    return _safe_metrics({'elapsed_seconds': elapsed, 'cpu_user_seconds': user,
        'cpu_system_seconds': system, 'child_cpu_user_seconds': delta('child_cpu_user'),
        'child_cpu_system_seconds': delta('child_cpu_system'),
        'cpu_percent_one_core': round(100 * (user + system) / elapsed, 3)
        if elapsed and user is not None and system is not None else None,
        'peak_rss_bytes': end.get('peak_rss_bytes'), 'disk_free_bytes': end.get('disk_free_bytes')})


def _runtime_info():
    result = {}
    for key, getter in [('python', platform.python_version), ('os', platform.system),
                        ('os_release', platform.release), ('architecture', platform.machine)]:
        try:
            value = getter()
            result[key] = value if isinstance(value, str) and re.fullmatch(
                r'[A-Za-z0-9_.+-]{1,80}', value) else None
        except Exception:
            result[key] = None
    try:
        result['cpu_count'] = os.cpu_count()
    except Exception:
        result['cpu_count'] = None
    return result


def _identity(root):
    try:
        from .identity import capture_identity

        value = capture_identity(root)
        identity = value.get('identity_id')
        manifest = value.get('manifest_path')
        if (value.get('state') != 'READY' or not isinstance(identity, str)
                or not re.fullmatch(r'[0-9a-f]{64}', identity)
                or manifest != f'data/normalized/runtime-evidence/identities/{identity}.json'):
            raise ValueError('INVALID_IDENTITY')
        result = {'state': 'READY', 'identity_id': identity, 'manifest_path': manifest}
        for key in ('source_file_count', 'configuration_file_count'):
            if type(value.get(key)) is int and 0 <= value[key] <= 100_000:
                result[key] = value[key]
        return result
    except Exception:
        _error()
        return {'state': 'UNAVAILABLE', 'reason': 'IDENTITY_CAPTURE_FAILED'}


class _Recorder:
    def __init__(self, root, task, scheduled, options):
        self.root, self.task = _root(root), task
        if task not in TASKS:
            raise ValueError('RUNTIME_INVALID_TASK')
        self.invocation_id = uuid.uuid4().hex
        self.started_at = _now()
        self.relative = f'logs/runtime/{self.started_at[:10]}/{self.invocation_id}.jsonl'
        self.lock = threading.RLock()
        self.fd, self.sequence, self.dropped = None, 0, 0
        self.reported_failure = False
        self.scheduled = bool(scheduled)
        self.options = {k: v for k, v in (options or {}).items()
                        if k in {'download', 'republish'} and type(v) is bool}

    def failed(self):
        self.dropped += 1
        if not self.reported_failure:
            self.reported_failure = True
            _error()

    def begin(self):
        try:
            path = owned(self.root, self.relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            owned(self.root, self.relative)
            self.fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND
                              | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        except Exception:
            self.failed()
            return
        try:
            birth = probe_process(os.getpid()).get('birth')
        except Exception:
            birth = None
        if not isinstance(birth, str) or not re.fullmatch(r'[A-Za-z0-9 :+\-]{1,80}', birth):
            birth = None
        self.write('START', pid=os.getpid(), process_birth=birth,
                   scheduled=self.scheduled, trigger='SCHEDULED' if self.scheduled else 'MANUAL',
                   options=self.options, identity=_identity(self.root), runtime_info=_runtime_info())

    def write(self, event, **fields):
        with self.lock:
            if self.fd is None:
                self.failed()
                return
            self.sequence += 1
            try:
                row = {'schema_version': 1, 'event': event, 'invocation_id': self.invocation_id,
                       'task': self.task, 'recorded_at': self.started_at if event == 'START' else _now(),
                       'sequence': self.sequence, **fields}
                raw = (json.dumps(row, ensure_ascii=False, sort_keys=True,
                                  allow_nan=False, separators=(',', ':')) + '\n').encode()
                if len(raw) > MAX_EVENT_BYTES:
                    raise ValueError('EVENT_TOO_LARGE')
                remaining = memoryview(raw)
                while remaining:
                    count = os.write(self.fd, remaining)
                    if count <= 0:
                        raise OSError('WRITE_FAILED')
                    remaining = remaining[count:]
                os.fsync(self.fd)
            except Exception:
                self.failed()

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except Exception:
                self.failed()
            self.fd = None


def _record(recorder, event, **fields):
    try:
        recorder.write(event, **fields)
    except Exception:
        recorder.failed()


def _summary(recorder, value):
    try:
        return safe_fields(value, recorder.root) if isinstance(value, dict) else {}
    except Exception:
        recorder.failed()
        return {}


def emit_event(kind, **fields):
    """Append a safe scalar event in an invocation, otherwise perform no IO."""
    recorder = _CURRENT.get()
    if recorder is None:
        return
    try:
        if (not isinstance(kind, str) or not re.fullmatch(r'[a-z][a-z0-9_.]{0,63}', kind)
                or _SENSITIVE.search(kind)):
            recorder.failed()
            return
        _record(recorder, 'EVENT', kind=kind, **safe_fields(fields, recorder.root))
    except Exception:
        recorder.failed()


def record_exception(exc, **identity):
    """Record a caught exception in the active invocation; no context means no IO."""
    recorder = _CURRENT.get()
    if recorder is None:
        return None
    try:
        diagnostic = _exception(exc, recorder.root)
        _record(recorder, 'EVENT', kind='exception', exception=diagnostic,
                **safe_fields(identity, recorder.root))
        return diagnostic['fingerprint']
    except Exception:
        recorder.failed()
        return None


def stage(name, callback, **identity):
    """Measure a stage while preserving the callback value or original exception object."""
    recorder = _CURRENT.get()
    if recorder is None:
        return callback()
    valid = (isinstance(name, str) and re.fullmatch(r'[a-z][a-z0-9_.]{0,63}', name)
             and not _SENSITIVE.search(name))
    if not valid:
        recorder.failed()
        return callback()
    stage_id = uuid.uuid4().hex
    fields = {k: v for k, v in _summary(recorder, identity).items()
              if k in _IDS | _DATES | {'omitted_fields', 'omitted_field_count'}}
    start = measurement()
    _record(recorder, 'STAGE_START', stage=name, stage_id=stage_id, **fields)
    try:
        result = callback()
    except BaseException as exc:
        _record(recorder, 'STAGE_ERROR', stage=name, stage_id=stage_id, **fields,
                error_code='STAGE_CALLBACK_RAISED', metrics=measure_since(start),
                exception=_exception(exc, recorder.root))
        raise
    summary = _summary(recorder, result)
    _record(recorder, 'STAGE_END', stage=name, stage_id=stage_id, **fields,
            result=summary, metrics=measure_since(start))
    return result


def run_observed(root, task, callback, *, scheduled=False, options=None, handled_exceptions=()):
    """Observe one invocation without changing its return, exception, or business locks."""
    try:
        recorder = _Recorder(root, task, scheduled, options)
    except Exception:
        _error()
        return callback()
    token = _CURRENT.set(recorder)
    start = measurement()
    try:
        try:
            recorder.begin()
        except Exception:
            recorder.failed()
        try:
            value = callback()
        except BaseException as exc:
            exit_code = None
            if isinstance(exc, SystemExit):
                exit_code = exc.code if type(exc.code) is int else 0 if exc.code is None else 1
            elif isinstance(exc, KeyboardInterrupt):
                exit_code = 130
            else:
                try:
                    if isinstance(exc, handled_exceptions):
                        exit_code = 2  # Only the caller's explicit catch/exit contract proves this.
                except TypeError:
                    recorder.failed()
            _record(recorder, 'END', result_state='EXCEPTION', exit_code=exit_code,
                    error_code='CALLBACK_RAISED', metrics=measure_since(start),
                    dropped_events=recorder.dropped, exception=_exception(exc, recorder.root))
            raise
        result, exit_code = value if isinstance(value, tuple) and len(value) == 2 else ({}, None)
        summary = _summary(recorder, result)
        _record(recorder, 'END', result_state=summary.get('state') or 'UNKNOWN',
                exit_code=exit_code if type(exit_code) is int else None,
                result=summary, metrics=measure_since(start), dropped_events=recorder.dropped)
        return value
    finally:
        recorder.close()
        _CURRENT.reset(token)
