"""Strict, provider-independent wire values for execution calculations."""

import re
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

from china_stock_sim.common.fees import ExecutionSupportError
from china_stock_sim.common.policies import policy_digest


def to_wire(value):
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, float):
        raise ExecutionSupportError('INVALID_INPUT', '执行输入不接受浮点数')
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ExecutionSupportError('INVALID_INPUT', '执行输入数值必须有限')
        return format(value, 'f')
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise ExecutionSupportError('INVALID_INPUT', '执行时间必须包含时区')
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        if any(not isinstance(k, str) for k in value):
            raise ExecutionSupportError('INVALID_INPUT', '执行字段名必须为字符串')
        return {k: to_wire(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_wire(v) for v in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise ExecutionSupportError('INVALID_INPUT', '执行输入类型不受支持')


def reject_credentials(value):
    """Reject before output as well as before persistence; never echo the value."""
    # Do not recursively re-copy every ancestor's whole subtree.
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ExecutionSupportError('INVALID_INPUT', '执行字段名必须为字符串')
            if re.sub(r'[-_\s]', '', key).lower() in {'apikey', 'authorization', 'password'}:
                raise ExecutionSupportError('INVALID_INPUT', '执行输入禁止包含凭据')
            reject_credentials(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            reject_credentials(item)
    elif isinstance(value, str) and (
        re.search(r'(sk-[A-Za-z0-9_.-]{12,}|Bearer\s+\S+)', value, re.I)
        or re.search(r'(?:api[-_ ]?key|authorization|password)\s*[\"\']?\s*[:=]', value, re.I)
    ):
        raise ExecutionSupportError('INVALID_INPUT', '执行输入禁止包含凭据')
    elif not isinstance(value, str):
        to_wire(value)


def input_digest(value):
    return policy_digest(to_wire(value))
