"""Pure decimal fee arithmetic shared by execution and ledger consumers."""

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, DecimalException


class ExecutionSupportError(ValueError):
    """A public error whose code can be adapted at each business boundary."""

    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def session_date(value):
    if isinstance(value, datetime) or not isinstance(value, (date, str)):
        raise ExecutionSupportError('INVALID_INPUT', '交易日期须为date或ISO日期')
    try:
        day = date.fromisoformat(value) if isinstance(value, str) else value
    except ValueError as exc:
        raise ExecutionSupportError('INVALID_INPUT', '交易日期无效') from exc
    if isinstance(value, str) and day.isoformat() != value:
        raise ExecutionSupportError('INVALID_INPUT', '交易日期须为YYYY-MM-DD')
    return day.isoformat()


def aware_time(value):
    try:
        result = datetime.fromisoformat(value) if isinstance(value, str) else value
    except ValueError as exc:
        raise ExecutionSupportError('INVALID_INPUT', '知识时点无效') from exc
    if not isinstance(result, datetime) or result.tzinfo is None or result.utcoffset() is None:
        raise ExecutionSupportError('INVALID_INPUT', '知识时点须带时区')
    return result


def _decimal(value):
    if isinstance(value, (float, bool)):
        raise ExecutionSupportError('INVALID_INPUT', '价格/比例须使用十进制字符串或Decimal')
    try:
        result = Decimal(value)
    except (DecimalException, TypeError, ValueError) as exc:
        raise ExecutionSupportError('INVALID_INPUT', '无效十进制数') from exc
    if not result.is_finite():
        raise ExecutionSupportError('INVALID_INPUT', '数值须为有限数')
    return result


def number(value, *, positive=False):
    result = _decimal(value)
    if result < 0 or (positive and result == 0):
        raise ExecutionSupportError('INVALID_INPUT', '数值须为有限非负数')
    return result


def integer(value, *, minimum=0):
    if type(value) is not int or value < minimum:
        raise ExecutionSupportError('INVALID_INPUT', '数量/分金额必须为合法整数')
    return value


def money(value):
    try:
        return int((_decimal(value) * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    except DecimalException as exc:
        raise ExecutionSupportError('INVALID_INPUT', '金额超出支持精度') from exc


def validate_fee_policy(policy, session, *, side=None, knowledge_cutoff=None):
    """Validate the original fee dictionary without adding snapshot metadata to it."""
    if not isinstance(policy, dict):
        raise ExecutionSupportError('INVALID_INPUT', '费用政策须为字典')
    if side not in (None, 'BUY', 'SELL'):
        raise ExecutionSupportError('INVALID_INPUT', '买卖方向无效')
    required = {'version', 'commission_rate', 'commission_minimum_fen',
                'commission_includes_exchange_fees', 'sell_stamp_rate',
                'sell_stamp_effective_from', 'transfer_rate_both_sides',
                'transfer_effective_from', 'rounding'}
    if not required <= policy.keys():
        raise ExecutionSupportError('INVALID_INPUT', '费用政策缺少必要字段',
                                    missing=sorted(required - policy.keys()))
    if not isinstance(policy['version'], str) or not policy['version'].strip():
        raise ExecutionSupportError('INVALID_INPUT', '费用政策版本无效')
    if policy['rounding'] != 'ROUND_HALF_UP':
        raise ExecutionSupportError('INVALID_INPUT', '不支持的费用舍入政策')
    if policy['commission_includes_exchange_fees'] is not True:
        raise ExecutionSupportError('INVALID_INPUT', '当前只支持全佣且不重复计交易所费用')
    integer(policy['commission_minimum_fen'])
    for key in ('commission_rate', 'sell_stamp_rate', 'transfer_rate_both_sides'):
        if number(policy[key]) > 1:
            raise ExecutionSupportError('INVALID_INPUT', '费用比例不能超过1', field=key)
    day = session_date(session)
    prefixes = ['', 'commission_', 'transfer_']
    if side in (None, 'SELL'):
        prefixes.append('sell_stamp_')
    for prefix in prefixes:
        start, end = policy.get(prefix + 'effective_from'), policy.get(prefix + 'effective_to')
        if start is not None:
            start = session_date(start)
        if end is not None:
            end = session_date(end)
        if start and end and start > end:
            raise ExecutionSupportError('INVALID_INPUT', '费用政策生效区间倒置')
        if (start and day < start) or (end and day > end):
            raise ExecutionSupportError('WAITING_DATA', '目标日缺少有效费用政策')
    if policy.get('known_at') is not None:
        known = aware_time(policy['known_at'])
        if knowledge_cutoff is not None and known > aware_time(knowledge_cutoff):
            raise ExecutionSupportError('WAITING_DATA', '费用政策在知识截止后才可知')
    return policy


def fee_breakdown(price, quantity, side, policy, session):
    price, quantity = number(price, positive=True), integer(quantity, minimum=1)
    if side not in ('BUY', 'SELL'):
        raise ExecutionSupportError('INVALID_INPUT', '买卖方向无效')
    validate_fee_policy(policy, session, side=side)
    try:
        amount = money(price * quantity)
        commission = max(integer(policy['commission_minimum_fen']),
                         money(price * quantity * number(policy['commission_rate'])))
        stamp = money(price * quantity * number(policy['sell_stamp_rate'])) if side == 'SELL' else 0
        transfer = money(price * quantity * number(policy['transfer_rate_both_sides']))
    except DecimalException as exc:
        raise ExecutionSupportError('INVALID_INPUT', '费用金额超出支持精度') from exc
    fees = commission + stamp + transfer
    return {'amount_fen': amount, 'commission_fen': commission, 'stamp_fen': stamp,
            'transfer_fen': transfer, 'fees_fen': fees,
            'net_cash_fen': -amount - fees if side == 'BUY' else amount - fees}
