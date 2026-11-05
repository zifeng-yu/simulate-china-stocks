"""Read-only scheduler checks/rendering and explicit launchd installation controls."""

from china_stock_sim.config import load_settings, local_path

from . import launchd
from .state import WorkflowJournal

COMMANDS = {'scheduler-status', 'scheduler-render', 'scheduler-install', 'scheduler-uninstall'}


def register_commands(commands):
    commands.add_parser('scheduler-status', help='只读查看项目调度文件、launchd与耐久流程状态')
    render = commands.add_parser('scheduler-render', help='生成当前plist预览；默认不写文件或安装')
    render.add_argument('--output', help='显式写入本项目deploy/launchd下的plist文件；不安装')
    commands.add_parser('scheduler-install', help='显式安装并加载本项目用户LaunchAgent；不激活账户')
    commands.add_parser('scheduler-uninstall', help='显式卸载本项目LaunchAgent；保留账本及流程记录')


def execute(args, root):
    settings = load_settings(root)
    if args.command == 'scheduler-status':
        result = {**launchd.status(settings), 'workflow': WorkflowJournal(settings).summary()}
    elif args.command == 'scheduler-render':
        result = launchd.render(settings)
        if args.output:
            path = local_path(settings.root, args.output)
            directory = local_path(settings.root, 'deploy/launchd')
            if not path.is_relative_to(directory) or path.suffix != '.plist':
                raise ValueError('调度预览仅允许写入本项目deploy/launchd下的plist')
            launchd._atomic(path, result['content'].encode())
            result['output_path'] = str(path)
    elif args.command == 'scheduler-install':
        result = launchd.install(settings)
    else:
        result = launchd.uninstall(settings)
    return result, 2 if result['state'] in {'INSTALL_FAILED', 'UNINSTALL_FAILED',
        'STATUS_UNAVAILABLE', 'UNSUPPORTED_PLATFORM', 'CONFIG_DRIFT'} else 0
