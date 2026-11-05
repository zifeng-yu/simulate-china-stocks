"""Deterministic project accounting policies; never infer missing source facts."""

import tomllib
from calendar import monthrange
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import urlparse

from china_stock_sim.config import local_path


def holding_rule(root, rule_id):
    """Read an explicitly maintained rule, never choose a default or fetch the web."""
    path = local_path(root, 'configs/tax_holding_rules.toml')
    if not path.is_file():
        return None
    with path.open('rb') as stream:
        config = tomllib.load(stream)
    if config.get('schema_version') != 1 or config.get('default_state') != 'UNKNOWN':
        raise ValueError('税务起点规则配置必须使用 schema_version=1、default_state=UNKNOWN')
    rules = config.get('rules', [])
    if not isinstance(rules, list) or any(not isinstance(rule, dict) for rule in rules):
        raise ValueError('税务起点规则 rules 必须是对象列表')
    ids = [rule.get('rule_id') for rule in rules]
    if any(not isinstance(identity, str) or not identity for identity in ids) or len(ids) != len(set(ids)):
        raise ValueError('税务起点规则 ID 必须唯一且非空')
    selected = next((rule for rule in rules if rule['rule_id'] == rule_id), None)
    if selected is None:
        return None
    if selected.get('start_basis') not in {'ORIGINAL_ACQUISITION', 'SHARE_DELIVERY'}:
        raise ValueError('税务起点只允许原股份取得日或股份交付日')
    start = date.fromisoformat(selected['effective_from'])
    end = date.fromisoformat(selected.get('effective_to', '9999-12-31'))
    if end < start:
        raise ValueError('税务起点规则生效区间倒置')
    authorities = selected.get('authorities')
    if not isinstance(authorities, list) or not authorities:
        raise ValueError('税务起点规则必须明确官方依据')
    for source in authorities:
        if not isinstance(source, dict) or not source.get('title') or not source.get('reference'):
            raise ValueError('官方依据需要 title、reference 和 HTTPS url')
        url = urlparse(source.get('url', ''))
        host = url.hostname or ''
        if (url.scheme != 'https' or url.username or url.password or
                not (host.endswith('.gov.cn') or any(host == name or host.endswith('.' + name)
                     for name in ('sse.com.cn', 'szse.cn', 'chinaclear.cn')))):
            raise ValueError('税务起点依据必须来自政府、交易所或中国结算官方 HTTPS 页面')
    return selected


def decimal(value):
    if isinstance(value, (bool, float)):
        raise ValueError('Financial values must use integers or decimal strings')
    number = Decimal(value)
    if not number.is_finite():
        raise ValueError('Non-finite financial value')
    return number


def money_fen(yuan):
    return int((decimal(yuan) * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def allocate_fen(total, weights):
    """Allocate an integer amount exactly; stable last positive weight owns tail."""
    if type(total) is not int or total < 0:
        raise ValueError('Allocation must be a nonnegative integer amount')
    weights = [decimal(w) for w in weights]
    if any(w < 0 for w in weights):
        raise ValueError('Negative allocation weight')
    if not sum(weights):
        if total:
            raise ValueError('Cannot allocate nonzero amount to no units')
        return [0] * len(weights)
    result = [0] * len(weights)
    positive = [i for i, w in enumerate(weights) if w]
    remaining = total
    for i in positive[:-1]:
        # Floor allocation avoids a negative last remainder for tiny totals.
        result[i] = int(Decimal(total) * weights[i] / sum(weights))
        remaining -= result[i]
    result[positive[-1]] = remaining
    return result


def dividend_tax_rate(acquired, sold):
    """Natural month/year boundaries, sale at anniversary still in inclusive band."""
    acquired, sold = date.fromisoformat(acquired), date.fromisoformat(sold)
    if sold < acquired:
        raise ValueError('Sale predates acquisition')
    year, month = acquired.year + acquired.month // 12, acquired.month % 12 + 1
    monthly = date(year, month, min(acquired.day, monthrange(year, month)[1]))
    annual = date(acquired.year + 1, acquired.month,
                  min(acquired.day, monthrange(acquired.year + 1, acquired.month)[1]))
    return Decimal('0.20') if sold <= monthly else (
        Decimal('0.10') if sold <= annual else Decimal('0'))


def dividend_basis(event):
    """Yuan per qualified original share; missing required tax facts stay unknown."""
    cash = event.get('cash_per_share')
    bonus, capital = event.get('bonus_per_share'), event.get('capital_per_share')
    if any(value is None for value in (cash, bonus, capital)):
        return None, 'DISTRIBUTION_TERMS_UNKNOWN'
    cash, bonus, capital = map(decimal, (cash, bonus, capital))
    facts = event.get('tax_facts', {})
    face = facts.get('share_face_value', {})
    source = facts.get('capital_source', {})
    if bonus and face.get('state') != 'READY':
        return None, 'BONUS_FACE_VALUE_UNKNOWN'
    taxable = bonus
    if capital:
        if source.get('state') != 'READY':
            return None, 'CAPITAL_SOURCE_UNKNOWN'
        if source.get('value') == 'OTHER':
            if face.get('state') != 'READY':
                return None, 'CAPITAL_FACE_VALUE_UNKNOWN'
            taxable += capital
        elif source.get('value') != 'STOCK_PREMIUM':
            return None, 'CAPITAL_SOURCE_UNKNOWN'
    return cash + (taxable * decimal(face['value']) if taxable else 0), None
