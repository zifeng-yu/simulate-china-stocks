"""Read-only runtime queries and explicit local evidence archiving."""

import json
import sqlite3
from pathlib import Path

from .archive import archive_runtime, verify_archive
from .history import query_history
from .recording import owned, run_observed
from .retention import apply_retention, plan_retention, rotate_stdio

COMMANDS = {'runtime-history', 'runtime-archive', 'runtime-archive-verify',
            'runtime-retention-plan', 'runtime-retention-apply', 'runtime-log-rotate'}


def execute_observed(handler, args, root):
    """Only execution entrypoints write observations; every query stays read-only."""
    task = {'workflow-run': 'workflow', 'dashboard-refresh': 'homepage',
            'dashboard-sync': 'upload'}.get(args.command)
    if task is None:
        return handler(args, root)
    options = {name: getattr(args, name) for name in ('download', 'republish')
               if hasattr(args, name)}
    return run_observed(root, task, lambda: handler(args, root),
                        scheduled=getattr(args, 'scheduled', False), options=options,
                        handled_exceptions=(OSError, ValueError, KeyError, TypeError,
                                            RuntimeError, sqlite3.Error))


def register_commands(commands):
    query = commands.add_parser('runtime-history', help='只读查询任务运行、性能与记录覆盖')
    query.add_argument('--date', help='北京时间日期YYYY-MM-DD；默认近期有界扫描')
    query.add_argument('--task', choices=['workflow', 'homepage', 'upload'])
    for key in ('invocation-id', 'run-id', 'account-id', 'decision-id'):
        query.add_argument('--' + key)
    query.add_argument('--limit', type=int, default=20, help='返回条数，1至100')
    archive = commands.add_parser('runtime-archive', help='显式归档指定日期运行记录及身份原件')
    archive.add_argument('--date', required=True, help='北京时间日期YYYY-MM-DD')
    verify = commands.add_parser('runtime-archive-verify', help='只读验证运行归档，不解压或覆盖文件')
    verify.add_argument('--archive', required=True, help='data/backups/runtime下的归档路径')
    for command in ('runtime-retention-plan', 'runtime-retention-apply'):
        retention = commands.add_parser(command, help='运行资料留存计划或按摘要受控清理')
        retention.add_argument('--protection-manifest', help='项目内完整保护引用与故障解决声明JSON')
        if command.endswith('-apply'):
            retention.add_argument('--expected-plan', required=True, help='已审阅dry-run的plan_id')
    rotation = commands.add_parser('runtime-log-rotate', help='仅对明确已停任务轮换stdout/stderr')
    rotation.add_argument('--stopped-task', action='append', required=True,
                          choices=['workflow', 'homepage', 'upload'])


def _protection(root, name):
    if name is None:
        return {}
    path = Path(name)
    if path.is_absolute():
        path = path.relative_to(Path(root).resolve())
    path = owned(root, str(path))
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('PROTECTION_MANIFEST_TOO_LARGE')
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict) or set(value) - {
            'protected_references', 'reference_inventory_complete', 'resolved_failures'}:
        raise ValueError('PROTECTION_MANIFEST_INVALID')
    return value


def execute(args, root):
    try:
        if args.command == 'runtime-history':
            result = query_history(root, date=args.date, task=args.task,
                invocation_id=args.invocation_id, run_id=args.run_id,
                account_id=args.account_id, decision_id=args.decision_id, limit=args.limit)
        elif args.command == 'runtime-archive':
            result = archive_runtime(root, date=args.date)
        elif args.command == 'runtime-archive-verify':
            result = verify_archive(root, args.archive)
        elif args.command == 'runtime-log-rotate':
            result = rotate_stdio(root, stopped_tasks=args.stopped_task)
        else:
            options = _protection(root, args.protection_manifest)
            result = (plan_retention(root, **options) if args.command == 'runtime-retention-plan'
                      else apply_retention(root, expected_plan=args.expected_plan, **options))
    except (OSError, ValueError, RuntimeError, TypeError):
        # Never print an exception containing a local path, endpoint or credential.
        return {'state': 'FAILED', 'error_code': 'RUNTIME_COMMAND_FAILED'}, 2
    failed = {'FAILED', 'INTEGRITY_ERROR', 'UNAVAILABLE', 'INVALID_INPUT', 'PARTIAL',
              'BUSY', 'WAITING_MANUAL', 'WAITING', 'REJECTED'}
    return result, 2 if result.get('state') in failed else 0
