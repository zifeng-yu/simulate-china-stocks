"""Explicit strategy commands; read-only inspection/evaluation/audit by default."""

import csv
import io
import json
import os
import tempfile

from china_stock_sim.application.strategy import StrategyApplication
from china_stock_sim.common.wire import reject_credentials, to_wire
from china_stock_sim.config import load_settings, local_path

COMMANDS = {'strategy-inspect', 'strategy-evaluate', 'strategy-plan',
            'strategy-publish', 'strategy-recover', 'strategy-audit'}


def register_commands(commands):
    for name in sorted(COMMANDS):
        parser = commands.add_parser(name, help={
            'strategy-inspect': '只读检查目标日输入、政策与已有决策',
            'strategy-evaluate': '离线计算固定输入或解析已存模型响应；不请求模型',
            'strategy-plan': '显式保存策略输入和决策，模型策略必要时调用模型；不封存订单',
            'strategy-publish': '显式规划并验证、封存计划；不撮合或结算',
            'strategy-recover': '恢复同一目标日规划及发布，复用已持久模型响应',
            'strategy-audit': '只读查询策略决策、过程及引用，可显式导出',
        }[name])
        if name not in {'strategy-evaluate', 'strategy-audit'}:
            parser.add_argument('--account', required=True)
            parser.add_argument('--target-session', required=True)
            parser.add_argument('--known-at')
        if name == 'strategy-inspect':
            parser.add_argument('--include-input', action='store_true')
        elif name == 'strategy-evaluate':
            parser.add_argument('--input', required=True, help='项目内固定策略输入包JSON')
            parser.add_argument('--response', help='项目内已保存的模型响应JSON或文本')
        elif name == 'strategy-audit':
            parser.add_argument('--account')
            parser.add_argument('--strategy-id')
            parser.add_argument('--strategy-version')
            parser.add_argument('--policy-id')
            parser.add_argument('--start')
            parser.add_argument('--end')
        parser.add_argument('--output', help='显式输出到项目reports/strategies下')
        parser.add_argument('--format', choices=('json', 'csv'), default='json')


def _output_path(root, value):
    path = local_path(root, value)
    if not path.is_relative_to((root / 'reports/strategies').resolve()):
        raise ValueError('策略报告只能写到reports/strategies')
    if path.suffix not in {'.json', '.csv'}:
        raise ValueError('策略报告须为JSON或CSV文件')
    return path


def _read(root, path):
    path = local_path(root, path)
    if not path.is_file() or path.stat().st_size > 100_000_000:
        raise ValueError('输入必须为项目内普通文件且不超过100MB')
    if path.name.startswith('.env'):
        raise ValueError('不能把凭据配置作为策略输入')
    return path.read_text(encoding='utf-8')


def _csv(result):
    rows = result.get('decisions', [])
    fields = ['account_id', 'target_session', 'strategy_id', 'strategy_version',
              'policy_id', 'policy_hash', 'state', 'plan_id', 'reasons']
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        values = {}
        for key in fields:
            item = row.get(key, '')
            value = json.dumps(item, ensure_ascii=False) if isinstance(item, (dict, list)) else str(item or '')
            values[key] = "'" + value if value.startswith(('=', '+', '-', '@')) else value
        writer.writerow(values)
    return stream.getvalue()


def execute(args, root):
    settings = load_settings(root)
    output = _output_path(settings.root, args.output) if args.output else None
    if output and output.suffix != '.' + args.format:
        raise ValueError('输出扩展名必须与format相同')
    if args.format == 'csv' and args.command != 'strategy-audit':
        raise ValueError('CSV仅用于策略审计导出')
    application = StrategyApplication(settings)
    name = args.command
    if name == 'strategy-inspect':
        result = application.inspect(args.account, args.target_session,
            knowledge_cutoff=args.known_at, include_input=args.include_input)
    elif name == 'strategy-evaluate':
        bundle = json.loads(_read(root, args.input))
        if 'input_bundle' in bundle:
            bundle = bundle['input_bundle']
        response = _read(root, args.response) if args.response else None
        if response:
            try:
                saved = json.loads(response)
                if isinstance(saved, dict) and 'text' in saved:
                    response = saved['text']
            except ValueError:
                pass
        result = application.evaluate(bundle, model_response=response)
    elif name == 'strategy-audit':
        accounts = application.ledger.list_accounts()
        if args.account:
            accounts = [a for a in accounts if a['account_id'] == args.account]
            if not accounts:
                raise ValueError('账户不存在')
        if args.strategy_id:
            accounts = [a for a in accounts if a['strategy_id'] == args.strategy_id]
        decisions = []
        for account in accounts:
            decisions.extend(application.audit(account['account_id'], start=args.start,
                end=args.end, strategy_version=args.strategy_version,
                policy_id=args.policy_id)['decisions'])
        result = {'state': 'READY', 'decisions': decisions,
                  'accounts': [a['account_id'] for a in accounts]}
    else:
        method = {'strategy-plan': application.plan, 'strategy-publish': application.publish,
                  'strategy-recover': application.recover}[name]
        result = method(args.account, args.target_session, knowledge_cutoff=args.known_at)
    result = to_wire(result)
    reject_credentials(result)
    if output:
        text = _csv(result) if args.format == 'csv' else json.dumps(result, ensure_ascii=False, indent=2) + '\n'
        temporary = None
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix='.strategy-', dir=output.parent)
            with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
                stream.write(text)
            os.replace(temporary, output)
            result['output'] = str(output)
        except OSError:
            result['report_error'] = {'kind': 'REPORT_WRITE_FAILED',
                                      'reason': '策略结果已保留；修复报告路径后可重新导出'}
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
    success = result['state'] in {'READY', 'PREPARED', 'PROPOSED', 'FROZEN'}
    return result, 0 if success and not result.get('report_error') else 2
