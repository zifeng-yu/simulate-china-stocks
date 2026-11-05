"""Explicit native indicator maintenance, separate from shutdown-status probing."""

from china_stock_sim.desktop import indicator

COMMANDS = {'shutdown-indicator-build', 'shutdown-indicator-install',
            'shutdown-indicator-status', 'shutdown-indicator-uninstall'}


def register_commands(commands):
    commands.add_parser('shutdown-indicator-build', help='构建当前原生关机指示器；不安装或启动')
    commands.add_parser('shutdown-indicator-install', help='显式安装登录菜单栏指示器；不改变原三任务')
    commands.add_parser('shutdown-indicator-status', help='只读查看第四个菜单栏显示进程及安装状态')
    commands.add_parser('shutdown-indicator-uninstall', help='卸载菜单栏指示器；保留业务与原三任务')


def execute(args, root):
    operation = args.command.removeprefix('shutdown-indicator-')
    try:
        result = getattr(indicator, operation)(root)
    except indicator.IndicatorError as exc:
        return {'state': 'FAILED', 'error_code': str(exc), 'note': indicator.NOTE}, 2
    good = {'BUILT', 'INSTALLED', 'ALREADY_INSTALLED', 'UNINSTALLED', 'RUNNING',
            'LOADED', 'NOT_LOADED', 'NOT_INSTALLED', 'BUILD_REQUIRED'}
    return result, 0 if result.get('state') in good else 2
