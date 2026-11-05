"""Shared display helpers; identities are supplied by the frozen audit projection."""

import json
from decimal import Decimal
from html import escape

from china_stock_sim.config import local_path


def money(value):
    return '待核' if value is None else f'{Decimal(value) / 100:,.2f}'


def report_path(settings, path):
    """Resolve every write at publication time, including symlink containment."""
    resolved = local_path(settings.root, str(path.relative_to(settings.root)))
    if not resolved.is_relative_to((settings.root / 'reports').resolve()):
        raise ValueError('报告输出必须位于项目 reports 目录')
    return resolved


def csv_cell(value):
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
        return "'" + value
    if isinstance(value, str) and value.startswith(('\t', '\r', '\n')):
        return "'" + value
    return value


def json_details(label, value):
    if value is None:
        return f'<p>{escape(label)}：未记录</p>'
    return '<details><summary>' + escape(label) + '</summary><pre>' + escape(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)) + '</pre></details>'


def strategy_label(identity):
    if not identity or not any(identity.get(key) for key in ('strategy_id', 'strategy_version', 'provenance_mode')):
        return '当日无已冻结计划'
    strategy = identity.get('strategy_name') or identity.get('strategy_id') or '未记录策略'
    revision = identity.get('strategy_version') or '未记录'
    source = '人工来源' if identity.get('provenance_mode') == 'MANUAL' else '策略来源'
    return f'{strategy} · 修订 {revision} · {source}'


def identity_html(identity):
    if not identity:
        return '<p>尚无已冻结计划，暂无计划关联的策略修订和参数快照。</p>'
    result = '<p><strong>' + escape(strategy_label(identity)) + '</strong></p>'
    fields = [('strategy_id', '策略 ID'), ('account_policy_id', '账户参数修订'),
              ('effective_from', '参数生效起点'), ('effective_to', '参数生效终点')]
    result += '<p>' + '<br>'.join(
        escape(label) + '：' + escape(str(identity.get(key) if identity.get(key) is not None else '未记录'))
        for key, label in fields) + '</p>'
    result += json_details('当时账户参数', identity.get('parameters'))
    if identity.get('uses_model') is True:
        model = identity.get('model') or {}
        result += '<p>当时模型：' + escape(str(model.get('model') or '未记录')) + '</p>'
        result += json_details('当时模型公开配置', model)
    metadata = {key: identity.get(key) for key in (
        'decision_id', 'account_policy_hash', 'strategy_implementation_hash', 'input_hash',
        'program_revision', 'prompt_revision')}
    result += json_details('程序、提示词及审计标识', metadata)
    return result


def overview_html(account, audit):
    name = account.get('display_name') or account['account_id']
    result = '<h2>账户与策略</h2><p>账户名称：' + escape(str(name))
    result += '<br>账户 ID：' + escape(str(account['account_id']))
    result += '<br>持久登记策略：' + escape(str(account.get('strategy_id') or '未记录'))
    result += '<br>观察起点：' + escape(str(account.get('simulation_start_session') or '尚未启用')) + '</p>'
    plans = sorted(audit['plans'], key=lambda row: (row.get('target_session') or '', row['plan_id'] or ''))
    if plans:
        latest = plans[-1]
        result += '<h3>最近已冻结计划身份</h3><p>目标交易日：' + escape(str(latest.get('target_session')))
        result += '。这是该计划当时的记录，不代表当前或未来生效政策。</p>'
        result += identity_html(latest['identity'])
    else:
        result += identity_html(None)
    result += '<p class="muted">审计范围：已冻结计划及关联订单、成交；未封存决策可通过strategy-audit查询，当前政策可通过account-policy查询。</p>'
    return result
