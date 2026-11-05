"""Explicit workflow commands; inspection never creates a lock or calls providers."""

from china_stock_sim.application.workflow import WorkflowApplication
from china_stock_sim.config import load_settings

COMMANDS = {'workflow-inspect', 'workflow-run'}


def register_commands(commands):
    commands.add_parser('workflow-inspect', help='只读查看真实日程、账户欠账与恢复状态')
    run = commands.add_parser('workflow-run', help='按真实时钟显式执行整日流程与恢复，遵守各层启用配置')
    run.add_argument('--download', action='store_true', help='允许数据层按已有来源规则补缺')
    run.add_argument('--scheduled', action='store_true', help='自动触发；要求启用且不在业务避让时段')


def execute(args, root):
    application = WorkflowApplication(load_settings(root))
    result = (application.inspect() if args.command == 'workflow-inspect' else
              application.run(download=args.download, scheduled=args.scheduled))
    return result, 0 if result['state'] in {'READY', 'COMPLETE', 'DISABLED', 'SKIPPED'} else 2
