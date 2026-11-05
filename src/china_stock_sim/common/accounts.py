"""Deterministic selection from persistent accounts, without configuration side effects."""

from china_stock_sim.common.fees import ExecutionSupportError


def select_account(accounts, preferred, explicit=None):
    identifiers = sorted(row['account_id'] for row in accounts)
    if not identifiers:
        raise ExecutionSupportError('INVALID_INPUT', '账本没有已登记账户，请先初始化')
    if explicit is not None:
        if explicit not in identifiers:
            raise ExecutionSupportError('INVALID_INPUT', '未知账户，请使用 account-list 查看已登记账户')
        return {'account_id': explicit, 'selection_reason': '显式指定账户', 'fallback': False}
    if preferred in identifiers:
        return {'account_id': preferred, 'selection_reason': '配置的默认账户', 'fallback': False}
    return {'account_id': identifiers[0], 'selection_reason': '配置的默认账户未登记，已选择按ID排序的首账户',
            'fallback': True}
