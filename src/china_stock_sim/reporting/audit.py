"""Read-only public projection of frozen decisions; never select stocks or load model secrets."""

import hashlib
import json
import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from html import escape
from pathlib import Path
from urllib.parse import quote

from china_stock_sim.common.execution_views import order_projection, result_summary
from china_stock_sim.config import local_path
from china_stock_sim.ledger import decision_evidence
from china_stock_sim.ledger.decision_evidence import read_business_document, verify_plan_decision
from china_stock_sim.ledger.kernel import LedgerError, canonical, digest, reject_credentials
from china_stock_sim.ledger.plan_storage import FORMAT, _header_identity, resolve_plan
from china_stock_sim.market.storage import atomic_text

IDENTITY_FIELDS = ('strategy_id', 'strategy_version', 'provenance_mode', 'decision_id',
                   'account_policy_id', 'account_policy_hash', 'strategy_implementation_hash',
                   'input_hash', 'parameters', 'effective_from', 'effective_to',
                   'program_revision', 'prompt_revision', 'strategy_name', 'uses_model', 'model')
PRIVATE_FIELDS = {'url', 'baseurl', 'endpoint', 'headers', 'requestheaders', 'responseheaders',
                  'apibase', 'apikey', 'authorization', 'password', 'credential', 'credentials',
                  'clientsecret', 'secret', 'accesstoken', 'refreshtoken', 'authtoken', 'token',
                  'cookie', 'cookies', 'setcookie', 'host', 'hostname', 'privatehost',
                  'transporthost', 'endpointurl', 'connectionstring', 'privatekey', 'auth'}


def _public(value):
    """Project evidence for display, excluding transport details, not modifying the original."""
    if isinstance(value, dict):
        return {key: _public(item) for key, item in value.items()
                if re.sub(r'[-_\s]', '', key).lower() not in PRIVATE_FIELDS}
    if isinstance(value, list):
        return [_public(item) for item in value]
    if isinstance(value, str):
        return re.sub(r'https?://[^\s<>"\']+', '[来源地址已隐藏]', value, flags=re.I)
    return value


def _safe(value):
    reject_credentials(value)
    return _public(deepcopy(value))


def _compact_calculation(value, reference):
    """Keep ordinary visible calculation; bound unforeseen large attachments by original references."""
    raw = canonical(value).encode()
    if len(raw) <= 256 * 1024:
        return _safe(value)
    projected, budget = {}, 128 * 1024
    for key, child in list(value.items())[:64]:
        size = len(canonical(child).encode())
        if size <= min(64 * 1024, budget):
            projected[key] = _safe(child)
            budget -= size
        else:
            projected[key] = {'projection': 'REFERENCE_ONLY', 'contained_in': _safe(reference),
                'json_pointer': '/calculation/' + key.replace('~', '~0').replace('/', '~1'),
                'content_hash': digest(child), 'bytes': size}
    if len(value) > 64:
        projected['_remaining_fields'] = {'projection': 'REFERENCE_ONLY',
            'contained_in': _safe(reference), 'json_pointer': '/calculation',
            'content_hash': hashlib.sha256(raw).hexdigest(), 'field_count': len(value) - 64}
    return projected


def _text(value):
    if not isinstance(value, str):
        return None
    try:
        return _safe(value)
    except ValueError:
        return None


def _identity(plan):
    value = dict.fromkeys(IDENTITY_FIELDS)
    for key in ('strategy_id', 'strategy_version', 'provenance_mode'):
        value[key] = _text(plan.get(key))
    value['provenance_mode'] = value['provenance_mode'] or 'MANUAL'
    return value


def _generation_parameters(parameters):
    result = {}
    for key in ('temperature', 'top_p', 'max_output_tokens', 'timeout_seconds',
                'seed', 'json_mode', 'enable_thinking'):
        if key not in parameters:
            continue
        value = parameters[key]
        if key in {'json_mode', 'enable_thinking'}:
            valid = type(value) is bool
        elif key in {'temperature', 'top_p'}:
            try:
                valid = type(value) in (int, str) and Decimal(value).is_finite()
            except (InvalidOperation, ValueError):
                valid = False
        else:
            valid = type(value) is int and value >= 0
        if not valid:
            raise ValueError('冻结模型生成参数类型无效')
        result[key] = value
    return result


def _core_identity(plan, core):
    identity = _identity(plan)
    ref, policy = plan['decision_ref'], core['account_policy']
    for key in ('decision_id', 'account_policy_id', 'account_policy_hash',
                'strategy_implementation_hash', 'input_hash'):
        identity[key] = ref[key]
    identity.update({key: _safe(policy[key]) for key in ('parameters', 'effective_from', 'effective_to')})
    metadata = core['calculation'].get('identity', {})
    if not isinstance(metadata, dict):
        raise ValueError('冻结展示身份必须为对象')
    for key in ('program_revision', 'prompt_revision', 'strategy_name'):
        if key in metadata and (not isinstance(metadata[key], str) or not metadata[key].strip()):
            raise ValueError('冻结展示修订必须为非空字符串')
        identity[key] = _text(metadata.get(key))
    if 'uses_model' in metadata and type(metadata['uses_model']) is not bool:
        raise ValueError('冻结模型能力标志必须为布尔')
    identity['uses_model'] = metadata.get('uses_model')
    if identity['uses_model'] is True:
        model = metadata.get('model')
        if model is not None:
            if not isinstance(model, dict):
                raise ValueError('冻结模型身份必须为对象')
            safe_model = {key: _text(model.get(key)) for key in ('model', 'provider')}
            parameters = model.get('generation_parameters', {})
            if not isinstance(parameters, dict):
                raise ValueError('冻结模型生成参数必须为对象')
            safe_model['generation_parameters'] = _generation_parameters(parameters)
            identity['model'] = safe_model
    return identity


def _summary_identity(plan, account_id):
    # Only strict stored headers qualify. Reducing an arbitrary legacy payload
    # to these fields would conceal modifications outside the projection.
    if plan.get('storage_format') != FORMAT:
        return None
    header = _header_identity(plan)
    projection = {'schema': 1, 'source': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  'private_fields': sorted(PRIVATE_FIELDS), 'identity_fields': IDENTITY_FIELDS}
    return digest({'account_id': account_id, 'header_identity': header,
                   'stored_header': plan, 'projection': projection})


def _validated_proof(root, kind, identity):
    """Read details only after the existing validator checks every dependency.

    Keep this adapter local: altering the financial validator merely to expose
    its existing proof would invalidate unrelated financial/backup receipts.
    """
    if not decision_evidence.cached_validation(root, kind, identity):
        return None
    return decision_evidence._VALIDATIONS.get(
        decision_evidence._validation_key(root, kind, identity))


def _cached_summary(root, identity):
    proof = _validated_proof(root, 'public_plan_summary', identity)
    details = (proof or {}).get('details')
    summary = details.get('summary') if isinstance(details, dict) else None
    if (not isinstance(summary, dict) or not proof.get('dependencies')
            or summary.get('audit_state') not in {'MANUAL', 'COMPLETE'}
            or summary.get('orders') != [] or summary.get('fills') != []):
        return None
    return deepcopy(summary)


def _plan_audit(root, plan, account_id, *, materialize=True):
    item = {'plan_id': _text(plan.get('plan_id')), 'target_session': _text(plan.get('target_session')),
            'status': _text(plan.get('status')), 'empty_reason': None, 'identity': _identity(plan),
            'audit_state': 'AUDIT_UNAVAILABLE', 'audit_issue': None, 'calculation': None,
            'plan': None, 'orders': [], 'fills': [], 'evidence_files': [], 'input_bundle': None}
    try:
        summary_identity = None if materialize else _summary_identity(plan, account_id)
        if summary_identity:
            cached = _cached_summary(root, summary_identity)
            if cached is not None:
                return cached
        stored = plan
        plan = resolve_plan(root, plan)
        reject_credentials(plan)
        if plan.get('account_id') != account_id:
            raise ValueError('冻结计划所属账户不符')
        info = None
        if summary_identity:
            # resolve_plan just read the original and compared every header
            # field. Reuse that same verified dependency, not a fresh stat.
            proof = _validated_proof(root, 'frozen_plan_header', _header_identity(stored))
            if proof:
                info = next(({key: value for key, value in dependency.items() if key != 'file_identity'}
                    for dependency in proof['dependencies'] if dependency['path'] == plan['document']['path']), None)
        if info is None:
            original, info = read_business_document(root, plan['document'])
            if canonical(original) != canonical({k: v for k, v in plan.items() if k not in {'document', 'id'}}):
                raise ValueError('冻结原件与账本不符')
        verified = verify_plan_decision(root, plan, materialize=materialize)
        item['plan'] = _safe(plan if materialize else {key: plan[key] for key in (
            'plan_id', 'account_id', 'target_session', 'plan_hash', 'document', 'orders',
            'frozen_at', 'input_cutoff', 'market_as_of', 'fee_policy_hash', 'rule_policy_hash',
            'ruleset_hash', 'decision_ref', 'status', 'no_order_reason') if key in plan})
        item['empty_reason'] = _text(plan.get('no_order_reason'))
        item['evidence_files'] = [info]
        if verified is None:
            item['audit_state'] = 'MANUAL'
        else:
            core = verified['core']
            item['identity'] = _core_identity(plan, core)
            item['calculation'] = (_safe(core['calculation']) if materialize else
                _compact_calculation(core['calculation'], plan['decision_ref']['decision_core']))
            # Render only the public identity contract, even if a producer retained transport extras.
            if 'identity' in item['calculation']:
                item['calculation']['identity'] = {key: item['identity'][key] for key in (
                    'strategy_name', 'program_revision', 'prompt_revision', 'uses_model', 'model')}
            item['input_bundle'] = (_safe(core['input_bundle']) if materialize else {
                'content_hash': plan['decision_ref']['input_hash'],
                'contained_in': _safe(plan['decision_ref']['decision_core']),
                'projection': 'REFERENCE_ONLY'})
            item['evidence_files'].extend(verified['closure']['files'])
            item['referenced_documents'] = (_safe(verified['closure']['bodies'])
                if materialize else {row['path']: {key: row[key] for key in (
                    'path', 'sha256', 'content_hash', 'kind') if key in row}
                    for row in verified['closure']['files']})
            # The verified core itself is represented above. Do not duplicate unfiltered metadata.
            item['referenced_documents'].pop(plan['decision_ref']['decision_core']['path'], None)
            item['audit_state'] = 'COMPLETE'
        if summary_identity and len(canonical(item).encode()) <= 1_000_000:
            dependencies = [info, *info.get('storage_dependencies', [])]
            if verified is not None:
                dependencies.extend(verified['closure']['files'])
            decision_evidence.remember_validation(root, 'public_plan_summary', summary_identity,
                dependencies, details={'summary': deepcopy(item)})
    except (ValueError, TypeError, KeyError, OSError, IndexError):
        item.update(audit_state='AUDIT_UNAVAILABLE',
                    audit_issue='冻结原件缺失、损坏或身份不一致；未使用当前配置补齐',
                    identity=_identity(plan), plan=None, calculation=None, input_bundle=None,
                    evidence_files=[], referenced_documents={})
    return item


def audit_account(root, report, *, materialize=True):
    """Use one report_snapshot and its immutable documents; scope excludes unsealed decisions."""
    account = report['account']
    account_id = account['account_id']
    plans = sorted((_plan_audit(root, row, account_id, materialize=materialize)
                    for row in report.get('plans', [])),
                   key=lambda row: (row['target_session'] or '', row['plan_id'] or ''))
    by_id = {row['plan_id']: row for row in plans}
    issues = []
    result = {'account_id': account_id, 'account_revision': report.get('account_revision', 0),
              'report_scope': report.get('report_scope', {'mode': 'full'}),
              'report_revision': report.get('report_revision', 0),
              'scope': 'FROZEN_PLANS_ONLY',
              'scope_note': '本页仅查询已封存计划；未封存决策和当前政策可通过strategy-audit与account-policy查询。',
              'verification_scope': '冻结计划和递归业务原件；未重新采集或重验整个市场库。',
              'projection_note': '公开审计投影隐藏认证、连接地址等内部字段，原件及摘要保持原样。',
              'account': _safe({key: account.get(key) for key in (
                  'account_id', 'display_name', 'strategy_id', 'initial_cash_fen',
                  'simulation_start_session', 'cash_fen')}),
              'plans': plans, 'orders': [], 'fills': [], 'no_plan_records': [], 'issues': issues}
    result['projection_scope'] = 'FULL_AUDIT' if materialize else 'SUMMARY_WITH_EVIDENCE_REFERENCES'
    if report.get('report_scope', {}).get('mode') == 'recent':
        result['scope_note'] += (' 当前页面仅显示最近交易日及未决关联计划；完整历史须按计划或日期范围显式审计。'
                                f" 已显示{report['report_scope']['returned_plans']}份，"
                                f"全部{report['report_scope']['total_plans']}份。")
    if not materialize:
        result['projection_note'] = '审计页保存决策解释、经济字段及原件摘要索引；完整原件由本机显式审计入口按引用展开。'
    for kind in ('orders', 'fills'):
        for row in report.get(kind, []):
            try:
                copied = _safe(row if materialize else
                    order_projection(row) if kind == 'orders' else result_summary(row))
                plan = by_id.get(row.get('plan_id'))
                copied['strategy_identity'] = deepcopy(plan['identity']) if plan else dict.fromkeys(IDENTITY_FIELDS)
                copied['audit_state'] = plan['audit_state'] if plan else 'AUDIT_UNAVAILABLE'
                if plan:
                    plan[kind].append(copied)
                else:
                    issues.append({'kind': 'MISSING_PLAN', 'record_kind': kind})
                result[kind].append(copied)
            except ValueError:
                issues.append({'kind': 'UNSAFE_RECORD', 'record_kind': kind})
    for row in report.get('steps', []):
        if row.get('kind') == 'NO_PLAN':
            try:
                result['no_plan_records'].append(_safe(row))
            except ValueError:
                issues.append({'kind': 'UNSAFE_RECORD', 'record_kind': 'no_plan_records'})
    result['state'] = 'PARTIAL' if issues or any(p['audit_state'] == 'AUDIT_UNAVAILABLE' for p in plans) else 'COMPLETE'
    return result


def identity_for_session(audit, day):
    matches = [row for row in audit['plans'] if row['target_session'] == day]
    return deepcopy(matches[0]['identity']) if len(matches) == 1 else dict.fromkeys(IDENTITY_FIELDS)


def account_filename(identity):
    if identity in {'LLM', 'S1', 'S1-LT'}:
        return identity
    return 'account-' + hashlib.sha256(identity.encode()).hexdigest()


def _audit_path(root, given, suffix, *, direct=False):
    root = Path(root).resolve()
    path = Path(given)
    path = root / path if not path.is_absolute() else path
    if direct and path.is_symlink():
        raise LedgerError('INVALID_INPUT', '审计查看页及配套导出文件不能通过符号链接改名')
    path = path.resolve()
    allowed = local_path(root, 'reports/strategies')
    if (not path.is_relative_to(allowed) or path.suffix.lower() != suffix
            or not allowed.is_relative_to(root / 'reports')):
        raise LedgerError('INVALID_INPUT', f'审计导出仅允许本项目 reports/strategies/*{suffix}')
    return root, path


def _audit_view_text(path, public):
    account = escape(str(public['account_id']))
    download = escape(quote(path.with_suffix('.json').name, safe=''), quote=True)
    body = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>账户审计 · ''' + account + '''</title><style>
*{box-sizing:border-box}body{margin:0;background:#f3f6f5;color:#193b3a;font:16px/1.7 system-ui,sans-serif}main{max-width:1100px;margin:auto;padding:28px 20px;overflow-wrap:anywhere}h1{font-size:28px}pre{background:white;border:1px solid #dbe4df;border-radius:10px;padding:20px;white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.7 ui-monospace,monospace}.notice{background:#fff2cc;padding:14px}a{color:#17694f}@media(max-width:600px){main{padding:20px 12px}pre{padding:12px}}
</style></head><body><main><h1>账户审计 · ''' + account + '</h1>'
    body += '<p>账户 ID：' + account + ' · 审计状态：' + escape(str(public['state'])) + '</p>'
    body += ''.join('<p>' + escape(str(public[key])) + '</p>' for key in (
        'scope_note', 'verification_scope', 'projection_note'))
    body += f'<p><a href="{download}" download>下载审计 JSON</a></p>'
    title = ('审计摘要与原件索引' if public.get('projection_scope') ==
             'SUMMARY_WITH_EVIDENCE_REFERENCES' else '完整审计内容')
    body += '<h2>' + title + '</h2><p>以下内容与 JSON 导出一致；中文直接显示，字段名保留原样。以 _fen 结尾的金额单位为分。</p>'
    body += '<pre id="audit-data">' + escape(json.dumps(
        public, ensure_ascii=False, indent=2, allow_nan=False)) + '</pre>'
    return body + '<p>本页只展示已保存的审计记录，刷新不会采集数据或发起交易。</p></main></body></html>'


def write_audit_view(root, given, audit):
    """Publish a UTF-8 human view of the same credential-safe JSON projection."""
    root, path = _audit_path(root, given, '.html', direct=True)
    body = _audit_view_text(path, _safe(audit))
    atomic_text(path, body)
    return str(path.relative_to(root))


def write_audit_report(root, given, audit):
    """Validate and serialize both report artifacts before publishing either."""
    root, path = _audit_path(root, given, '.json', direct=True)
    _, view_path = _audit_path(root, path.with_suffix('.html'), '.html', direct=True)
    public = _safe(audit)
    encoded = json.dumps(public, ensure_ascii=True, indent=2, allow_nan=False) + '\n'
    body = _audit_view_text(view_path, public)
    atomic_text(path, encoded)
    atomic_text(view_path, body)


def write_audit_output(root, given, audit):
    root, path = _audit_path(root, given, '.json')
    # Local JSON viewers can guess a legacy encoding; escapes preserve Unicode without a BOM.
    encoded = json.dumps(_safe(audit), ensure_ascii=True, indent=2, allow_nan=False) + '\n'
    atomic_text(path, encoded)
    return str(path.relative_to(root))
