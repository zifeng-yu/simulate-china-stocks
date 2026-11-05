"""Pure LLM input preparation and strict response semantics; no I/O or retries."""

import json
import re
from copy import deepcopy
from decimal import ROUND_FLOOR, Decimal, DecimalException, localcontext

from china_stock_sim.common.fees import ExecutionSupportError, money, session_date
from china_stock_sim.common.wire import to_wire
from china_stock_sim.strategies.base import StrategyDecision
from china_stock_sim.strategies.features import sample_volatility
from china_stock_sim.strategies.llm_prompt import (
    PROMPT_REVISION as PROMPT_REVISION,
)
from china_stock_sim.strategies.llm_prompt import (
    PROMPT_SHA256 as PROMPT_SHA256,
)
from china_stock_sim.strategies.llm_prompt import (
    build_messages as build_messages,
)
from china_stock_sim.strategies.llm_prompt import (
    response_schema,
)

_FEATURE_FIELDS = ('ret_5d', 'ret_20d', 'volatility_20d', 'avg_amount_20d',
                   'close', 'volume', 'amount', 'current_weight', 'sellable_quantity',
                   'quantity', 'cost_fen', 'market_value_fen')
_BASE_KEYS = {'code', 'side', 'reason', 'basis_fields', 'order_index'}
_MAX_RESPONSE_BYTES = 2_000_000


def _issue(path, code, **details):
    return {'path': path, 'code': code, **details}


def _integer(value, *, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError('输入整数无效')
    return value


def _decimal(value, *, positive=False):
    if not isinstance(value, (str, Decimal)) or isinstance(value, bool):
        raise ValueError('输入须为十进制字符串')
    try:
        value = Decimal(value)
    except DecimalException:
        raise ValueError('输入十进制数无效') from None
    if not value.is_finite() or value < 0 or positive and value == 0:
        raise ValueError('输入十进制数范围无效')
    return value


def _response_decimal(value):
    # Model strings must be ordinary decimal literals. In particular, a short
    # exponent must not expand into an unbounded audit/DTO string after parsing.
    if not isinstance(value, str) or re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', value) is None:
        raise ValueError('响应须使用普通十进制字符串')
    return _decimal(value, positive=True)


def _features(code, item, as_of):
    prices, bars = item.get('adjusted_close'), item.get('raw_bars')
    if not isinstance(prices, (list, tuple)) or len(prices) != 21:
        raise ValueError('需要21个调整价格')
    if not isinstance(bars, (list, tuple)) or len(bars) != 21:
        raise ValueError('需要21个同期原始行情')
    adjusted = [_decimal(value, positive=True) for value in prices]
    dates = [session_date(bar['date']) for bar in bars]
    if dates != sorted(set(dates)) or dates[-1] != as_of:
        raise ValueError('行情必须按唯一日期排列并以D结束')
    for bar in bars:
        if bar.get('code', code) != code:
            raise ValueError('行情证券与输入不一致')
        _decimal(bar['close'], positive=True)
        _decimal(str(bar['volume']) if type(bar['volume']) is int else bar['volume'])
        _decimal(str(bar['amount']) if type(bar['amount']) is int else bar['amount'])
    close = _decimal(bars[-1]['close'], positive=True)
    if item.get('close') is not None and close != _decimal(item['close'], positive=True):
        raise ValueError('D收盘价不一致')
    amounts = [_decimal(str(b['amount']) if type(b['amount']) is int else b['amount'])
               for b in bars[-20:]]
    volume = _decimal(str(bars[-1]['volume']) if type(bars[-1]['volume']) is int
                      else bars[-1]['volume'])
    with localcontext() as ctx:
        ctx.prec = 34
        return {
            'code': code, 'name': item.get('name', code),
            'close': format(close, 'f'),
            'ret_5d': format(adjusted[20] / adjusted[15] - 1, 'f'),
            'ret_20d': format(adjusted[20] / adjusted[0] - 1, 'f'),
            'volatility_20d': format(sample_volatility(adjusted), 'f'),
            'avg_amount_20d': format(sum(amounts) / 20, 'f'),
            'volume': format(volume, 'f'), 'amount': format(amounts[-1], 'f'),
        }


def build_request(context):
    """Prepare fixed candidate/holding facts; unavailable holdings stop the account.

    The application supplies already evidenced, same-cutoff public query results.
    This function neither reads a market service nor persists/claims a model attempt.
    """
    p = context.policy['parameters']
    max_candidates = _integer(p['max_candidates'], minimum=1)
    max_holdings = _integer(p['max_holdings'], minimum=1)
    max_codes = _integer(p['max_daily_symbols'], minimum=1)
    single, total = _decimal(p['max_target_weight']), _decimal(p['max_total_target_weight'])
    if max_candidates > 20 or max_holdings > 100 or max_codes > 5 or not single <= total <= 1:
        raise ValueError('LLM参数超出已支持范围')
    if type(context.net_assets_fen) is not int:
        raise ValueError('净资产必须为整数分')
    cash = _integer(context.account['available_cash_fen'])
    positions = context.account['positions']
    if not isinstance(positions, (list, tuple)):
        raise ValueError('持仓输入无效')
    held = {}
    for position in positions:
        code = position['code']
        quantity = _integer(position['total_shares'])
        sellable = _integer(position['sellable_shares'])
        _integer(position['cost_fen'])
        if code in held or sellable > quantity:
            raise ValueError('持仓重复或可卖数超过持仓')
        if quantity:
            held[code] = position
    excluded, reasons, features = [], [], {}
    for code, item in sorted(context.securities.items()):
        if code not in held and not (item.get('in_pool') is True and item.get('eligibility') == 'READY'):
            excluded.append({'code': code, 'reason': item.get('eligibility', 'UNAVAILABLE'),
                             'issues': deepcopy(item.get('issues', []))})
            continue
        try:
            if item.get('code', code) != code:
                raise ValueError('证券身份不一致')
            if item.get('eligibility') == 'UNAVAILABLE':
                raise ValueError('持仓资格或来源输入待核')
            features[code] = _features(code, item, context.market_as_of)
        except (KeyError, TypeError, ValueError, DecimalException, ExecutionSupportError):
            issue = _issue(f'securities.{code}', 'INCOMPLETE_LLM_INPUT')
            excluded.append({'code': code, 'reason': 'UNAVAILABLE',
                             'issues': [*deepcopy(item.get('issues', [])), issue]})
            if code in held:
                reasons.append(issue)
    for code in sorted(set(held) - set(features)):
        if not any(i['path'] == f'securities.{code}' for i in reasons):
            reasons.append(_issue(f'securities.{code}', 'MISSING_HOLDING_INPUT'))
    eligible = [code for code in features if context.securities[code].get('in_pool') is True
                and context.securities[code].get('eligibility') == 'READY']
    selected = sorted(eligible, key=lambda code: (-Decimal(features[code]['avg_amount_20d']), code))[
        :max_candidates]
    # n=0 unknown candidates is a data wait; confirmed exclusions may yield a valid HOLD.
    if not features and not held and any(x['reason'] == 'UNAVAILABLE' for x in excluded):
        reasons.append(_issue('securities', 'NO_USABLE_CANDIDATES'))
    with localcontext() as decimal_context:
        decimal_context.prec = 34
        values = {code: money(Decimal(features[code]['close']) * position['total_shares'])
                  for code, position in held.items() if code in features}
        market_value = sum(values.values())
        equity = context.net_assets_fen
        total_cap = max(0, min(cash, int((equity * total).to_integral_value(
            rounding=ROUND_FLOOR)) - market_value)) if equity > 0 else 0
        slots = max(0, max_holdings - len(held))
        securities = []
        for code in sorted(set(selected) | set(held)):
            if code not in features:
                continue
            position = held.get(code)
            value = values.get(code, 0)
            cap = max(0, min(total_cap, int((equity * single).to_integral_value(
                rounding=ROUND_FLOOR)) - value)) if equity > 0 else 0
            actions = ['HOLD']
            if position and position['sellable_shares'] > 0:
                actions.insert(0, 'SELL')
            if code in selected and cap > 0 and (position or slots > 0):
                actions.insert(0, 'BUY')
            row = {**features[code], 'quantity': position['total_shares'] if position else 0,
                   'sellable_quantity': position['sellable_shares'] if position else 0,
                   'cost_fen': position['cost_fen'] if position else 0,
                   'market_value_fen': value,
                   'current_weight': format(Decimal(value) / equity, 'f') if equity > 0 else None,
                   'is_holding': bool(position), 'buy_candidate': code in selected,
                   'allowed_actions': actions, 'max_buy_budget_fen': cap}
            row['basis_fields'] = [k for k in _FEATURE_FIELDS if row.get(k) is not None]
            securities.append(row)
    request = {
        'state': 'WAITING_INPUT' if reasons else 'READY', 'reasons': reasons,
        'account_id': context.account_id, 'strategy_id': context.strategy_id,
        'target_session': context.target_session, 'market_as_of': context.market_as_of,
        'input_cutoff': context.input_cutoff, 'prompt_revision': PROMPT_REVISION,
        'prompt_sha256': PROMPT_SHA256,
        'units': {'prices': 'CNY per share; decimal strings', 'amount': 'CNY; decimal strings',
                  'volume': 'shares', 'cash_and_budget': 'integer fen, includes fees',
                  'returns_and_weights': 'decimal ratios, not percentages'},
        'resources': {'net_assets_fen': equity, 'available_cash_fen': cash,
                      'stock_market_value_fen': market_value, 'current_holdings': len(held),
                      'remaining_new_slots': slots, 'max_total_buy_budget_fen': total_cap,
                      'max_holdings': max_holdings, 'max_target_weight': format(single, 'f'),
                      'max_total_target_weight': format(total, 'f'),
                      'max_daily_symbols': max_codes, 'price_tick': '0.01',
                      'anticipated_sale_proceeds_allowed': False,
                      'anticipated_vacancies_allowed': False},
        'buy_candidates': selected, 'holdings': sorted(held), 'securities': securities,
        'excluded': excluded, 'fee_policy': deepcopy(context.fee_policy),
        'response_schema': response_schema(),
    }
    return to_wire(request)


def _invalid(errors):
    return StrategyDecision(state='INVALID', reasons=tuple(errors),
                            calculation={'response_validation': {'status': 'INVALID', 'errors': errors}})


def parse_response(text, request, *, finish_reason='stop'):
    """Validate a persisted, sanitized answer. Success still needs full execution validation."""
    if not isinstance(request, dict) or request.get('state') != 'READY':
        raise ValueError('仅可解析已就绪固定输入的响应')
    if finish_reason != 'stop':
        return _invalid([_issue('finish_reason', 'INCOMPLETE_RESPONSE', allowed_values=['stop'])])
    if not isinstance(text, str) or not text.strip():
        return _invalid([_issue('$', 'EMPTY_RESPONSE')])
    try:
        if len(text.encode('utf-8')) > _MAX_RESPONSE_BYTES:
            return _invalid([_issue('$', 'RESPONSE_TOO_LARGE')])
    except UnicodeError:
        return _invalid([_issue('$', 'INVALID_JSON')])
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('DUPLICATE_KEY')
            result[key] = value
        return result
    def forbidden(_):
        raise ValueError('NON_INTEGER_JSON_NUMBER')
    try:
        body = json.loads(text, object_pairs_hook=pairs, parse_float=forbidden,
                          parse_constant=forbidden)
        # JSON escape syntax can encode lone surrogates even when the original
        # response bytes were valid UTF-8. Such values cannot enter audit files.
        json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (ValueError, RecursionError, UnicodeError) as exc:
        code = str(exc) if str(exc) in {'DUPLICATE_KEY', 'NON_INTEGER_JSON_NUMBER'} else 'INVALID_JSON'
        return _invalid([_issue('$', code)])
    if not isinstance(body, dict) or set(body) != {'target_session', 'ordered_intents', 'no_order_reason'}:
        return _invalid([_issue('$', 'RESPONSE_FIELDS')])
    errors = []
    if body['target_session'] != request['target_session']:
        errors.append(_issue('target_session', 'WRONG_TARGET_SESSION',
                             allowed_values=[request['target_session']]))
    reason = body['no_order_reason']
    if reason is not None and (not isinstance(reason, str) or not reason.strip()):
        errors.append(_issue('no_order_reason', 'INVALID_REASON'))
    intents = body['ordered_intents']
    if not isinstance(intents, list):
        return _invalid([*errors, _issue('ordered_intents', 'EXPECTED_ARRAY')])
    universe = {row['code']: row for row in request['securities']}
    if len(intents) > len(universe):
        errors.append(_issue('ordered_intents', 'TOO_MANY_INTENTS'))
    seen, trades, new_buys, spent = set(), 0, 0, 0
    output, basis_evidence = [], []
    for index, item in enumerate(intents):
        path = f'ordered_intents[{index}]'
        if not isinstance(item, dict):
            errors.append(_issue(path, 'EXPECTED_OBJECT'))
            continue
        side = item.get('side')
        if side not in ('BUY', 'SELL', 'HOLD'):
            errors.append(_issue(path + '.side', 'INVALID_SIDE', allowed_values=['BUY', 'SELL', 'HOLD']))
            continue
        required = _BASE_KEYS | ({'limit_price', 'budget_fen'} if side == 'BUY' else
                                {'limit_price', 'sell_fraction'} if side == 'SELL' else set())
        if set(item) != required:
            errors.append(_issue(path, 'INTENT_FIELDS', allowed_values=sorted(required)))
            continue
        code = item['code']
        if not isinstance(code, str) or code not in universe:
            errors.append(_issue(path + '.code', 'UNKNOWN_SECURITY'))
            continue
        if code in seen:
            errors.append(_issue(path + '.code', 'DUPLICATE_SECURITY'))
        seen.add(code)
        row = universe[code]
        if side not in row['allowed_actions']:
            errors.append(_issue(path + '.side', 'ACTION_NOT_ALLOWED', allowed_values=row['allowed_actions']))
        if type(item['order_index']) is not int or item['order_index'] != index:
            errors.append(_issue(path + '.order_index', 'ORDER_INDEX_MISMATCH'))
        if not isinstance(item['reason'], str) or not item['reason'].strip():
            errors.append(_issue(path + '.reason', 'INVALID_REASON'))
        basis = item['basis_fields']
        if (not isinstance(basis, list) or any(not isinstance(v, str) for v in basis)
                or len(basis) != len(set(basis)) or not (0 if side == 'HOLD' else 1) <= len(basis) <= 3
                or any(v not in row['basis_fields'] or row.get(v) is None for v in basis)):
            errors.append(_issue(path + '.basis_fields', 'INVALID_BASIS', allowed_values=row['basis_fields']))
            basis = []
        intent = {'code': code, 'side': side, 'reason': item['reason'],
                  'order_index': index, 'limit_price': None}
        if side != 'HOLD':
            trades += 1
            try:
                price = _response_decimal(item['limit_price'])
                with localcontext() as ctx:
                    ctx.prec = 34
                    if price % Decimal(request['resources']['price_tick']):
                        raise ValueError('non-tick')
                intent['limit_price'] = format(price, 'f')
            except (ValueError, DecimalException):
                errors.append(_issue(path + '.limit_price', 'INVALID_TICK_PRICE'))
        if side == 'BUY':
            budget = item['budget_fen']
            if type(budget) is not int or budget <= 0:
                errors.append(_issue(path + '.budget_fen', 'INVALID_BUDGET'))
            else:
                spent += budget
                if budget > row['max_buy_budget_fen']:
                    errors.append(_issue(path + '.budget_fen', 'SYMBOL_BUDGET_EXCEEDED',
                                         maximum=row['max_buy_budget_fen']))
            intent['budget_fen'] = budget
            if not row['is_holding']:
                new_buys += 1
        elif side == 'SELL':
            try:
                fraction = _response_decimal(item['sell_fraction'])
                if fraction > 1:
                    raise ValueError('fraction')
                intent['sell_fraction'] = format(fraction, 'f')
            except (ValueError, DecimalException):
                errors.append(_issue(path + '.sell_fraction', 'INVALID_SELL_FRACTION'))
        output.append(intent)
        basis_evidence.append({'code': code, 'order_index': index, 'basis_fields': basis,
                               'basis_values': {key: deepcopy(row[key]) for key in basis},
                               'model_reason': item['reason']})
    resources = request['resources']
    if trades > resources['max_daily_symbols']:
        errors.append(_issue('ordered_intents', 'TOO_MANY_TRADE_CODES', maximum=resources['max_daily_symbols']))
    if spent > resources['max_total_buy_budget_fen'] or spent > resources['available_cash_fen']:
        errors.append(_issue('ordered_intents', 'TOTAL_BUY_BUDGET_EXCEEDED',
                             maximum=resources['max_total_buy_budget_fen']))
    if new_buys > resources['remaining_new_slots']:
        errors.append(_issue('ordered_intents', 'NEW_HOLDING_SLOTS_EXCEEDED',
                             maximum=resources['remaining_new_slots']))
    if trades == 0 and (not isinstance(reason, str) or not reason.strip()):
        errors.append(_issue('no_order_reason', 'NO_ORDER_REASON_REQUIRED'))
    if errors:
        return _invalid(errors)
    return StrategyDecision(
        state='READY', ordered_intents=tuple(output), no_order_reason=reason,
        calculation={'prompt_revision': request['prompt_revision'],
                     'prompt_sha256': request['prompt_sha256'], 'model_basis': basis_evidence,
                     'candidate_order': list(request['buy_candidates']),
                     'resources': deepcopy(resources),
                     'response_validation': {'status': 'SEMANTICALLY_VALID',
                                             'full_execution_validation_required': True}},
    )
