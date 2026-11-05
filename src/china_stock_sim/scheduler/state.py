"""Durable orchestration attempts; the ledger remains financial authority."""

import hashlib
import json
import os
import re
import tempfile
from copy import deepcopy

from china_stock_sim.common.fees import aware_time
from china_stock_sim.common.wire import reject_credentials, to_wire
from china_stock_sim.config import local_path

from .workspace import writer_lock

STEP_STATES = {"RUNNING", "COMPLETE", "WAITING", "FAILED", "INTERRUPTED", "SKIPPED"}
FINAL_STATES = (STEP_STATES - {"RUNNING"}) | {"PARTIAL"}
PHASES = {'calendar', 'data', 'month_end', 'input_review', 'recovery', 'settlement',
          'entitlements_before', 'execution', 'entitlements_after', 'close',
          'report_daily', 'plan', 'report_plan', 'backup'}


def _identity(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError("流程身份须为不超过512字符的非空字符串")
    return value


def _time(now):
    return aware_time(now).isoformat()


def _not_backwards(value, timestamp):
    if value.get("updated_at") and aware_time(timestamp) < aware_time(value["updated_at"]):
        raise ValueError("流程时钟早于已有记录，不倒填运行状态")


class WorkflowJournal:
    def __init__(self, settings):
        self.settings = settings
        self.path = local_path(settings.root, "data/state/workflow/journal.json")

    def _read_live(self):
        if not self.path.is_file():
            return {"schema_version": 1, "state": "NOT_STARTED", "runs": {},
                    "current": None, "tasks": {}}
        if self.path.stat().st_size > 100_000_000:
            raise ValueError("流程日志超出支持大小，不自动重置")
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError('流程日志结构无效')
        if value.get('storage_version', 1) not in {1, 2}:
            raise ValueError('流程日志存储版本无效')
        if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
                or value.get("schema_version") != 1
                or not isinstance(value.get("runs"), dict)
                or not isinstance(value.get("tasks"), dict)
                or value.get("state") not in STEP_STATES | {"PARTIAL", "NOT_STARTED"}
                or value.get("current") is not None and value["current"] not in value["runs"]):
            raise ValueError("流程日志结构或版本无效，不自动重置")
        for key, row in value["runs"].items():
            if (not isinstance(row, dict) or row.get("run_id") != key
                    or row.get("state") not in STEP_STATES | {"PARTIAL"}
                    or not isinstance(row.get("attempts"), list) or not row["attempts"]):
                raise ValueError("流程尝试记录损坏，不自动重置")
            for index, attempt in enumerate(row["attempts"], 1):
                if (not isinstance(attempt, dict) or attempt.get("attempt") != index
                        or attempt.get("state") not in STEP_STATES | {"PARTIAL"}
                        or not isinstance(attempt.get("steps"), list)):
                    raise ValueError("流程尝试历史损坏，不自动重置")
                if 'phases' in attempt:
                    if not isinstance(attempt['phases'], list):
                        raise ValueError('流程阶段历史损坏')
                    for phase in attempt['phases']:
                        if (not isinstance(phase, dict) or phase.get('phase') not in PHASES
                                or phase.get('state') not in STEP_STATES
                                or not isinstance(phase.get('result'), dict)):
                            raise ValueError('流程阶段记录损坏')
            if row["attempts"][-1]["state"] != row["state"]:
                raise ValueError("流程最新状态与尝试不一致")
        for key, task in value["tasks"].items():
            if (not isinstance(task, dict) or task.get("key") != key
                    or task.get("state") not in STEP_STATES | {"PARTIAL"}
                    or not isinstance(task.get("history"), list) or not task["history"]):
                raise ValueError("流程任务历史损坏，不自动重置")
        reject_credentials(value)
        return value

    def _object_path(self, reference):
        if (not isinstance(reference, dict) or set(reference) != {'path', 'sha256'}
                or not re.fullmatch(r'[0-9a-f]{64}', str(reference.get('sha256', '')))
                or reference['path'] != 'data/state/workflow/history/' + reference['sha256'] + '.json'):
            raise ValueError('流程历史引用无效')
        path = local_path(self.settings.root, reference['path'])
        current = self.settings.root
        for part in reference['path'].split('/'):
            current /= part
            if current.is_symlink():
                raise ValueError('流程历史不接受符号链接')
        return path

    def _put_object(self, value=None, *, raw=None):
        if raw is None:
            reject_credentials(value)
            raw = (json.dumps(to_wire(value), ensure_ascii=False, sort_keys=True,
                              separators=(',', ':')) + '\n').encode()
        identity = hashlib.sha256(raw).hexdigest()
        reference = {'path': 'data/state/workflow/history/' + identity + '.json', 'sha256': identity}
        path = self._object_path(reference)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != identity:
                raise ValueError('流程历史原件损坏')
            return reference
        descriptor, temporary = tempfile.mkstemp(prefix='.history-', dir=path.parent)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if hashlib.sha256(path.read_bytes()).hexdigest() != identity:
                    raise ValueError('流程历史原件损坏') from None
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return reference

    def _get_object(self, reference):
        path = self._object_path(reference)
        if not path.is_file() or path.stat().st_size > 100_000_000:
            raise ValueError('流程历史原件缺失或过大')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != reference['sha256']:
            raise ValueError('流程历史原件摘要不符')
        return json.loads(raw)

    def _task_history(self, task):
        if not task.get('history_ref'):
            return task['history']
        result, seen = [], set()
        reference = task['history_ref']
        while reference is not None:
            if reference['sha256'] in seen or len(seen) >= 1_000_000:
                raise ValueError('流程任务历史循环或超出范围')
            seen.add(reference['sha256'])
            node = self._get_object(reference)
            if node.get('kind') != 'task_history' or node.get('key') != task['key']:
                raise ValueError('流程任务历史身份不符')
            result.append(node['record'])
            reference = node['previous']
        if len(result) != task.get('history_count'):
            raise ValueError('流程任务历史数量不符')
        return list(reversed(result))

    def read(self):
        """Explicit complete audit read, including archived runs and task attempts."""
        value = self._read_live()
        if value.get('storage_version') == 2:
            for key, reference in value.get('run_refs', {}).items():
                if key not in value['runs']:
                    row = self._get_object(reference)
                    if row.get('run_id') != key:
                        raise ValueError('流程历史身份不符')
                    value['runs'][key] = row
            for task in value['tasks'].values():
                task['history'] = self._task_history(task)
        return value

    def read_current(self):
        """Current and recent observations, without expanding archived history."""
        value = self._read_live()
        return {**value, 'run_count': len(set(value['runs']) | set(value.get('run_refs', {}))),
                'task_count': len(value['tasks']), 'history_scope': 'RECENT_RUNTIME_RECORDS',
                'run_refs': {}}

    def summary(self):
        """Bounded read-only display, not a queue of historical waiting tasks."""
        value = self.read_current()
        row = value['runs'].get(value['current'])
        current = None
        if row:
            attempt = row['attempts'][-1]
            current = {key: attempt.get(key) for key in (
                'attempt', 'pid', 'state', 'started_at', 'finished_at')}
            current.update(run_id=row['run_id'], attempt_count=len(row['attempts']),
                           step_count=len(attempt['steps']))
            if attempt['steps']:
                current['last_step'] = {key: attempt['steps'][-1].get(key)
                    for key in ('step_id', 'state', 'recorded_at')}
        recent = sorted(value['tasks'].values(),
                        key=lambda task: (aware_time(task['recorded_at']), task['key']), reverse=True)[:50]
        return {'schema_version': value['schema_version'], 'state': value['state'],
            'updated_at': value.get('updated_at'), 'current': value['current'],
            'run_count': value['run_count'], 'task_count': value['task_count'],
            'current_run': current,
            'recent_tasks': [{key: task.get(key) for key in (
                'key', 'state', 'recorded_at', 'next_retry_at')} for task in recent],
            'recent_tasks_truncated': len(value['tasks']) > 50,
            'task_scope': 'recent_runtime_records_not_authoritative_pending_work'}

    def _write(self, value):
        value = deepcopy(value)
        if value.get('storage_version') != 2:
            if self.path.is_file():
                value['legacy_original'] = self._put_object(raw=self.path.read_bytes())
            value['storage_version'] = 2
        references = value.setdefault('run_refs', {})
        # Keep enough recent phases for the operational page; full history remains
        # available through read(), never through ordinary tick/status operations.
        candidates = sorted(value['runs'], key=lambda key: (
            value['runs'][key].get('finished_at') or value['runs'][key].get('started_at') or '', key))
        keep = set(candidates[-50:]) | {value.get('current')}
        for key in candidates:
            if key not in keep:
                references[key] = self._put_object(value['runs'].pop(key))
        for task in value['tasks'].values():
            history = task['history']
            if task.get('history_ref') and len(history) == 1:
                continue
            previous = None
            for record in history:
                previous = self._put_object({'kind': 'task_history', 'key': task['key'],
                                             'record': record, 'previous': previous})
            task.update(history_ref=previous, history_count=len(history), history=history[-1:])
        reject_credentials(value)
        payload = json.dumps(to_wire(value), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".journal-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def start(self, run_id, *, now, metadata=None):
        run_id, timestamp = _identity(run_id), _time(now)
        with writer_lock(self.settings, reentrant=True):
            value = self._read_live()
            _not_backwards(value, timestamp)
            # An acquired process lock proves prior RUNNING attempts are no longer active.
            for previous in value["runs"].values():
                if previous["state"] == "RUNNING":
                    previous.update(state="INTERRUPTED", finished_at=timestamp,
                                    result={"reason": "PREVIOUS_PROCESS_INTERRUPTED"})
                    previous["attempts"][-1].update(state="INTERRUPTED", finished_at=timestamp,
                        result={"reason": "PREVIOUS_PROCESS_INTERRUPTED"})
            if run_id not in value['runs'] and run_id in value.get('run_refs', {}):
                value['runs'][run_id] = self._get_object(value['run_refs'][run_id])
            row = value["runs"].setdefault(run_id, {"run_id": run_id, "attempts": []})
            attempt = {"attempt": len(row["attempts"]) + 1, "pid": os.getpid(),
                       "state": "RUNNING", "started_at": timestamp, "finished_at": None,
                       "metadata": to_wire(metadata or {}), "steps": [], "phases": [], "result": None}
            row["attempts"].append(attempt)
            row.update(state="RUNNING", started_at=timestamp, finished_at=None, result=None)
            value.update(current=run_id, state="RUNNING", updated_at=timestamp)
            self._write(value)
            return deepcopy(attempt)

    def context(self, run_id, *, now, **metadata):
        if set(metadata) - {'business_session', 'target_session', 'process_birth'}:
            raise ValueError('未知流程上下文字段')
        timestamp = _time(now)
        with writer_lock(self.settings, reentrant=True):
            value = self._read_live()
            _not_backwards(value, timestamp)
            row = value['runs'].get(_identity(run_id))
            if row is None or row['state'] != 'RUNNING':
                raise ValueError('流程尚未开始或已结束')
            row['attempts'][-1].setdefault('metadata', {}).update(to_wire(metadata))
            value['updated_at'] = timestamp
            self._write(value)

    def phase(self, run_id, phase, *, now, state, session=None, target_session=None,
              account_id=None, result=None):
        if phase not in PHASES or state not in STEP_STATES:
            raise ValueError('流程阶段或状态无效')
        timestamp = _time(now)
        record = {'phase': phase, 'session': session, 'target_session': target_session,
                  'account_id': account_id, 'state': state, 'recorded_at': timestamp,
                  'result': to_wire(result or {})}
        if len(json.dumps(record, ensure_ascii=False)) > 20000:
            raise ValueError('流程阶段摘要过大')
        with writer_lock(self.settings, reentrant=True):
            value = self._read_live()
            _not_backwards(value, timestamp)
            row = value['runs'].get(_identity(run_id))
            if row is None or row['state'] != 'RUNNING':
                raise ValueError('流程尚未开始或已结束')
            row['attempts'][-1].setdefault('phases', []).append(record)
            value['updated_at'] = timestamp
            self._write(value)
        return deepcopy(record)

    def step(self, run_id, step_id, *, now, state, result=None):
        if state not in STEP_STATES:
            raise ValueError("流程步骤状态无效")
        run_id, step_id, timestamp = _identity(run_id), _identity(step_id), _time(now)
        with writer_lock(self.settings, reentrant=True):
            value = self._read_live()
            _not_backwards(value, timestamp)
            row = value["runs"].get(run_id)
            if row is None or row["state"] != "RUNNING":
                raise ValueError("流程尚未开始或已结束")
            record = {"step_id": step_id, "state": state, "recorded_at": timestamp,
                      "result": to_wire(result or {})}
            row["attempts"][-1]["steps"].append(record)
            value["updated_at"] = timestamp
            self._write(value)
            return deepcopy(record)

    def finish(self, run_id, *, now, state, result=None):
        if state not in FINAL_STATES:
            raise ValueError("流程结束状态无效")
        run_id, timestamp = _identity(run_id), _time(now)
        with writer_lock(self.settings, reentrant=True):
            value = self._read_live()
            _not_backwards(value, timestamp)
            row = value["runs"].get(run_id)
            if row is None or row["state"] != "RUNNING":
                raise ValueError("流程尚未开始或已结束")
            outcome = to_wire(result or {})
            row.update(state=state, finished_at=timestamp, result=outcome)
            row["attempts"][-1].update(state=state, finished_at=timestamp, result=outcome)
            value.update(state=state, updated_at=timestamp)
            self._write(value)
            return deepcopy(row)

    def get_task(self, key):
        return deepcopy(self._read_live()["tasks"].get(_identity(key)))

    def save_task(self, key, *, now, state, result, next_retry_at=None):
        if state not in STEP_STATES | {"PARTIAL"}:
            raise ValueError("流程任务状态无效")
        key, timestamp = _identity(key), _time(now)
        retry = _time(next_retry_at) if next_retry_at is not None else None
        if retry is not None and aware_time(retry) < aware_time(timestamp):
            raise ValueError("重试时间不能早于任务记录时点")
        with writer_lock(self.settings, reentrant=True):
            value = self._read_live()
            _not_backwards(value, timestamp)
            previous = value["tasks"].get(key, {})
            record = {"state": state, "recorded_at": timestamp,
                      "next_retry_at": retry, "result": to_wire(result)}
            if previous.get('history_ref'):
                reference = self._put_object({'kind': 'task_history', 'key': key,
                    'record': record, 'previous': previous['history_ref']})
                task = {'key': key, **record, 'history': [record], 'history_ref': reference,
                        'history_count': previous['history_count'] + 1}
            else:
                task = {"key": key, **record, "history": [*previous.get("history", []), record]}
            value["tasks"][key] = task
            value["updated_at"] = timestamp
            self._write(value)
            return deepcopy(task)
