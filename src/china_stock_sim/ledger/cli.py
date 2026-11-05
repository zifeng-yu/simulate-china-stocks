"""Independent local ledger commands; no model settings or collection required."""

import json
from pathlib import Path

from china_stock_sim.common.accounts import select_account
from china_stock_sim.ledger.service import LedgerService

ACTIONS = ('activate_simulation', 'freeze_plan', 'record_no_plan', 'apply_execution_results',
           'register_entitlements', 'recognize_entitlements', 'settle_entitlements', 'record_tax',
           'resolve_tax_holding_rule', 'resolve_event_identity',
           'close_session', 'save_rebalance_task', 'claim_model_attempt', 'finish_model_attempt',
           'record_step_result', 'apply_correction', 'schedule_account_policy',
           'prepare_strategy_decision', 'save_strategy_decision')
COMMANDS = {'ledger-status', 'ledger-query', 'ledger-audit', 'ledger-migrate',
            'ledger-backup', 'ledger-restore', 'ledger-apply',
            'account-list', 'account-profile', 'account-audit', 'account-create',
            'account-policy-set', 'account-policy', 'strategy-decision'}


def register_commands(commands):
    commands.add_parser('account-list', help='只读列出持久登记账户，不开户')
    for name in ('account-profile', 'account-audit'):
        command = commands.add_parser(name, help='只读账户登记信息或已冻结策略审计')
        command.add_argument('--account', help='已登记账户ID；省略时按配置默认或稳定排序选择')
        if name == 'account-audit':
            command.add_argument('--output', type=Path, help='显式导出 reports/strategies/*.json')
    create = commands.add_parser('account-create', help='按项目内JSON显式原子开户和唯一注资，不启用观察')
    create.add_argument('--input', type=Path, required=True, help='含account_id及账户政策的JSON对象')
    create.add_argument('--operation-id', required=True)
    policy_set = commands.add_parser('account-policy-set', help='显式登记未来生效的账户政策，不改变旧记录')
    policy_set.add_argument('--account', required=True)
    policy_set.add_argument('--input', type=Path, required=True, help='含明确effective_session的JSON对象')
    policy_set.add_argument('--operation-id', required=True)
    policy_set.add_argument('--expected-revision', type=int, required=True)
    policy = commands.add_parser('account-policy', help='只读查询账户目标日适用的持久政策')
    policy.add_argument('--account', help='已登记账户ID；省略时按配置默认或稳定排序选择')
    policy.add_argument('--session', required=True, help='政策适用目标日')
    decision = commands.add_parser('strategy-decision', help='只读查询持久策略决策及审计状态，不生成计划')
    decision.add_argument('--account', help='已登记账户ID；省略时按配置默认或稳定排序选择')
    identity = decision.add_mutually_exclusive_group(required=True)
    identity.add_argument('--decision-id')
    identity.add_argument('--session', help='决策目标日')
    status = commands.add_parser('ledger-status', help='只读账户、待办和数据需求，不采集')
    status.add_argument('--account', help='已登记账户ID；省略时使用默认账户')
    status.add_argument('--session')
    query = commands.add_parser('ledger-query', help='只读查询完整账本')
    query.add_argument('--account', help='已登记账户ID；省略时使用默认账户')
    query.add_argument('--kind', choices=['report', 'history', 'orders', 'fills', 'cash_entries',
        'lots', 'tax_items', 'cases', 'pending', 'execution'], default='report')
    query.add_argument('--session')
    query.add_argument('--plan-id', help='execution查询所需冻结计划ID')
    query.add_argument('--start')
    query.add_argument('--end')
    query.add_argument('--offset', type=int, default=0)
    query.add_argument('--limit', type=int, default=1000)
    commands.add_parser('ledger-audit', help='只读检查账务及引用证据')
    commands.add_parser('ledger-migrate', help='先备份，再显式迁移当前账本结构；不启动交易')
    backup = commands.add_parser('ledger-backup', help='SQLite一致备份及完整证据清单')
    backup.add_argument('--label', default='daily')
    restore = commands.add_parser('ledger-restore', help='恢复到新的隔离项目目录并核验')
    restore.add_argument('--backup-id', required=True)
    restore.add_argument('--target-root', type=Path, required=True)
    apply = commands.add_parser('ledger-apply', help='提交完整账务DTO；不会采集或调用模型')
    apply.add_argument('--action', choices=ACTIONS, required=True)
    apply.add_argument('--account', required=True)
    apply.add_argument('--input', type=Path, required=True)
    apply.add_argument('--operation-id', required=True)
    apply.add_argument('--expected-revision', type=int, required=True)


def _input_payload(path, root):
    path = path.resolve()
    if not path.is_relative_to(root) or path.stat().st_size > 2_000_000:
        raise ValueError('账务输入须为本项目内不超过2MB的JSON文件')
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise ValueError('账务输入必须为JSON对象')
    return payload


def execute(args, root):
    ledger = LedgerService(root)
    day = getattr(args, 'session', None) or ledger._now().astimezone(ledger.settings.timezone).date().isoformat()
    selection = None
    if hasattr(args, 'account'):
        selection = select_account(ledger.list_accounts(), ledger.settings.default_account, args.account)
        args.account = selection['account_id']
    if args.command == 'account-list':
        result = {'accounts': ledger.list_accounts(), 'source': 'ledger'}
    elif args.command == 'account-create':
        result = ledger.create_account(_input_payload(args.input, ledger.root), operation_id=args.operation_id)
    elif args.command == 'account-policy-set':
        payload = _input_payload(args.input, ledger.root)
        if not payload.get('effective_session'):
            raise ValueError('政策修改必须明确指定effective_session')
        result = ledger.schedule_account_policy(args.account, payload, operation_id=args.operation_id,
                                                expected_revision=args.expected_revision)
    elif args.command == 'account-policy':
        result = {'account_policy': ledger.get_account_policy(args.account, args.session)}
    elif args.command == 'strategy-decision':
        decision = ledger.get_strategy_decision(args.account, decision_id=args.decision_id,
                                                target_session=args.session)
        result = {'decision': decision, 'state': 'FOUND' if decision is not None else 'NOT_FOUND'}
    elif args.command == 'account-profile':
        result = {'account': ledger.get_account_profile(args.account)}
    elif args.command == 'account-audit':
        from china_stock_sim.reporting.audit import audit_account, write_audit_output

        result = audit_account(ledger.root, ledger.report_snapshot(args.account))
        if args.output:
            try:
                result['output'] = write_audit_output(ledger.root, args.output, result)
            except (OSError, ValueError) as exc:
                result['output_error'] = {'state': 'REPORT_ERROR', 'error_type': type(exc).__name__}
    elif args.command == 'ledger-status':
        result = {'account': ledger.account_state(args.account, day),
                  'pending': ledger.pending_work(args.account),
                  'required_market_inputs': ledger.required_market_inputs(args.account, day)}
    elif args.command == 'ledger-audit':
        result = ledger.audit_status()
    elif args.command == 'ledger-migrate':
        result = ledger.migrate()
    elif args.command == 'ledger-backup':
        result = ledger.backup(label=args.label)
    elif args.command == 'ledger-restore':
        result = ledger.restore_backup(args.backup_id, args.target_root)
    elif args.command == 'ledger-apply':
        payload = _input_payload(args.input, ledger.root)
        result = getattr(ledger, args.action)(args.account, payload, operation_id=args.operation_id,
                                              expected_revision=args.expected_revision)
    elif args.kind == 'execution':
        if not args.plan_id:
            raise ValueError('execution查询必须指定--plan-id')
        result = ledger.execution_view(args.account, args.plan_id)
    elif args.kind == 'report':
        result = ledger.report_snapshot(args.account, day)
    elif args.kind == 'cases':
        result = {'rows': ledger.corporate_action_cases(args.account)}
    elif args.kind == 'pending':
        result = ledger.pending_work(args.account)
    else:
        parameters = {'start': args.start, 'end': args.end, 'offset': args.offset, 'limit': args.limit}
        result = (ledger.history(args.account, **parameters) if args.kind == 'history'
                  else ledger.export_rows(args.account, args.kind, **parameters))
    if selection:
        result['account_selection'] = selection
    return result, 0 if 'output_error' not in result and result.get('state') not in {
        'INVALID_INPUT', 'INTEGRITY_ERROR', 'CONFLICT', 'STALE_ACCOUNT', 'ERROR',
        'WAITING_DATA', 'PARTIAL', 'AUDIT_PARTIAL', 'RESTORED_AUDIT_PARTIAL', 'NOT_DUE', 'NOT_FOUND'} else 2
