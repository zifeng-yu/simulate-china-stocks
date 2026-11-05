"""Read-only execution checks. No collection, model credentials, or booking."""

import argparse
import json
import os
import tempfile
from pathlib import Path

from china_stock_sim.application.execution import ExecutionApplication
from china_stock_sim.common.accounts import select_account
from china_stock_sim.common.fees import ExecutionSupportError
from china_stock_sim.config import project_root
from china_stock_sim.execution.utils import reject_credentials, to_wire

COMMANDS = {'execution-validate', 'execution-match'}


def register_commands(commands):
    validate = commands.add_parser('execution-validate', help='只读检查盘前策略意图，不封存计划')
    validate.add_argument('--input', type=Path, required=True, help='本项目内策略意图JSON文件')
    match = commands.add_parser('execution-match', help='只读计算原冻结计划，不提交成交')
    match.add_argument('--account', help='已登记账户ID；省略时使用默认账户')
    match.add_argument('--plan-id', required=True)
    match.add_argument('--knowledge-cutoff', help='带时区证据截止，不得晚于当前真实时间')
    for parser in (validate, match):
        parser.add_argument('--output', type=Path, help='显式原子保存至reports/execution/*.json')


def _invalid_number(_):
    raise ValueError('金额和价格应使用整数分或十进制字符串')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('JSON对象存在重复字段')
        result[key] = value
    return result


def read_proposal(root, given):
    path = (root / given).resolve() if not given.is_absolute() else given.resolve()
    if not path.is_relative_to(root) or path.suffix.lower() != '.json' or path.stat().st_size > 2_000_000:
        raise ValueError('意图须为项目内不超过2MB的JSON文件')
    value = json.loads(path.read_text(), parse_float=_invalid_number,
                       parse_constant=_invalid_number, object_pairs_hook=_unique_object)
    if not isinstance(value, dict):
        raise ValueError('意图须为JSON对象')
    reject_credentials(value)
    return value


def write_output(root, given, result):
    path = (root / given).resolve() if not given.is_absolute() else given.resolve()
    report_dir = root / 'reports/execution'
    if (not report_dir.resolve().is_relative_to(root)
            or not path.is_relative_to(report_dir.resolve()) or path.suffix.lower() != '.json'):
        raise ValueError('执行报告只能保存到本项目reports/execution中的JSON文件')
    value = to_wire(result)
    reject_credentials(value)
    encoded = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.execution-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return str(path.relative_to(root))


def execute(args, root):
    root = Path(root).resolve()
    try:
        app = ExecutionApplication(root)
        if args.command == 'execution-validate':
            result = app.validate(read_proposal(root, args.input))
        else:
            selection = select_account(app.ledger.list_accounts(), app.settings.default_account, args.account)
            result = app.match(selection['account_id'], args.plan_id,
                               knowledge_cutoff=args.knowledge_cutoff)
            result['account_selection'] = selection
        result = to_wire(result)
        reject_credentials(result)
        if args.output:
            # Preserve calculation even if publishing fails; no recomputation or booking.
            try:
                result['output'] = write_output(root, args.output, result)
            except (ValueError, OSError) as exc:
                result['output_error'] = {'state': 'REPORT_ERROR', 'error_type': type(exc).__name__}
        status = result.get('status', result.get('state'))
        success = status in {'VALID', 'CALCULATED'} and result.get('calculation_status') != 'PARTIAL'
        return result, 0 if success and 'output_error' not in result else 2
    except (ValueError, TypeError, KeyError, OSError) as exc:
        code = exc.code if isinstance(exc, ExecutionSupportError) else 'INVALID_INPUT'
        return {'state': code, 'reason': '执行检查输入或本地读取失败',
                'error_type': type(exc).__name__}, 2


def main(argv=None):
    parser = argparse.ArgumentParser(description='独立规则与模拟执行检查：默认只读')
    parser.add_argument('--root', type=Path)
    register_commands(parser.add_subparsers(dest='command', required=True))
    args = parser.parse_args(argv)
    result, code = execute(args, project_root(args.root))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code
