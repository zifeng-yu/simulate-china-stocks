"""Separate read-only status, page publication, and explicit service installation."""

from china_stock_sim.config import load_settings, local_path
from china_stock_sim.runtime.archive import archive_due
from china_stock_sim.runtime.protection import maintain_runtime
from china_stock_sim.runtime.recording import emit_event, stage
from china_stock_sim.scheduler import dashboard_launchd, launchd
from china_stock_sim.scheduler.dashboard import refresh_dashboard
from china_stock_sim.scheduler.status import build_status

COMMANDS = {'dashboard-status', 'dashboard-refresh', 'dashboard-service-status',
            'dashboard-render', 'dashboard-install', 'dashboard-uninstall'}


def register_commands(commands):
    commands.add_parser('dashboard-status', help='只读查看每日运行节点，不执行交易流程')
    refresh = commands.add_parser('dashboard-refresh', help='轻量更新运行首页，不采集、交易或调用模型')
    refresh.add_argument('--open', action='store_true', help='更新后打开本机首页')
    refresh.add_argument('--scheduled', action='store_true', help='标记独立后台调用并检查运行归档')
    commands.add_parser('dashboard-service-status', help='只读查看独立首页刷新任务')
    renderer = commands.add_parser('dashboard-render', help='渲染首页刷新LaunchAgent预览')
    renderer.add_argument('--output', help='显式写入本项目deploy/launchd下的plist')
    commands.add_parser('dashboard-install', help='显式安装每15分钟刷新的独立首页任务')
    commands.add_parser('dashboard-uninstall', help='卸载首页刷新任务，保留交易调度')


def execute(args, root):
    settings = load_settings(root)
    if args.command == 'dashboard-status':
        result = build_status(settings)
    elif args.command == 'dashboard-refresh':
        result = stage('homepage.refresh', lambda: refresh_dashboard(settings, open_page=args.open))
        if args.scheduled and result.get('state') == 'DASHBOARD_PUBLISHED':
            try:
                archived = stage('runtime.archive', lambda: archive_due(settings.root))
            except Exception:
                # Operational archiving must never invalidate a committed homepage.
                archived = {'state': 'FAILED', 'reason': 'RUNTIME_ARCHIVE_FAILED'}
            emit_event('archive', status=archived.get('state'), error_code=archived.get('reason'))
            result = {**result, 'runtime_archive': archived}
            try:
                maintained = stage('runtime.maintenance', lambda: maintain_runtime(settings.root))
            except Exception:
                maintained = {'state': 'WAITING', 'reason': 'RUNTIME_MAINTENANCE_UNAVAILABLE'}
            emit_event('maintenance', status=maintained.get('state'), error_code=maintained.get('reason'))
            result = {**result, 'runtime_maintenance': maintained}
    elif args.command == 'dashboard-service-status':
        result = dashboard_launchd.status(settings)
    elif args.command == 'dashboard-render':
        result = dashboard_launchd.render(settings)
        if args.output:
            path = local_path(settings.root, args.output)
            if not path.is_relative_to(local_path(settings.root, 'deploy/launchd')) or path.suffix != '.plist':
                raise ValueError('首页调度预览只能写入本项目deploy/launchd下的plist')
            launchd._atomic(path, result['content'].encode())
            result['output_path'] = str(path)
    elif args.command == 'dashboard-install':
        result = dashboard_launchd.install(settings)
    else:
        result = dashboard_launchd.uninstall(settings)
    failed = result.get('state') in {'INSTALL_FAILED', 'UNINSTALL_FAILED', 'STATUS_UNAVAILABLE',
                                    'UNSUPPORTED_PLATFORM', 'CONFIG_DRIFT'}
    return result, 2 if failed else 0
