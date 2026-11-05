"""Frozen plans, reservations and atomic booking of externally computed executions."""

from datetime import datetime, time
from decimal import ROUND_HALF_UP, Decimal

from china_stock_sim.common.policies import MAX_ORDER_SHARES, daily_symbol_limit
from china_stock_sim.ledger.decision_evidence import verify_plan_decision
from china_stock_sim.ledger.evidence import SHANGHAI, document, fee_breakdown, integer, number
from china_stock_sim.ledger.execution_evidence import (
    bind_account_snapshot,
    bind_policy_bundle,
    capture_security_facts,
    validate_account_view_reference,
    validate_result_contract,
    validate_terminal_proof,
)
from china_stock_sim.ledger.execution_support import (
    apply_execution_batch,
    assert_execution_guards,
    capture_original_resources,
)
from china_stock_sim.ledger.kernel import LedgerError, canonical, digest, identifier
from china_stock_sim.market.contracts import aware_time, session_date
from china_stock_sim.market.quality import normalize_code


class TradingMixin:
    def activate_simulation(self, account_id, payload, *, operation_id, expected_revision):
        payload = self._payload(payload)
        target = payload.get('target_session', payload.get('effective_date'))

        def apply(tx):
            self._session(target)
            self._before_cutoff(target)
            existing = tx.state.get('simulation_start_session')
            if existing:
                if existing != target:
                    raise LedgerError('CONFLICT', '观察起点已固定，不可重新启动账户')
                return {'state': 'ALREADY_APPLIED', 'simulation_start_session': existing}
            tx.state['simulation_start_session'] = target
            return {'simulation_start_session': target}
        return self._mutate(account_id, operation_id, {'action': 'activate', **payload},
                            expected_revision, target, apply)

    def freeze_plan(self, account_id, payload, *, operation_id, expected_revision):
        payload = self._payload(payload)
        target = payload.get('target_session')
        # This check also protects an idempotent operation replay, which returns
        # before the transaction callback. Recheck inside the transaction below.
        try:
            decision = verify_plan_decision(self.root, payload, market=self.market, materialize=False)
            self._read(account_id, lambda tx: self._verify_registered_decision_tx(tx, payload, decision))
        except LedgerError as exc:
            return exc.as_dict()

        def apply(tx):
            decision = verify_plan_decision(self.root, payload, market=self.market, materialize=False)
            registered_decision = self._verify_registered_decision_tx(tx, payload, decision)
            plan_id = identifier(payload['plan_id'], '计划ID')
            plan_hash = digest({k: v for k, v in payload.items()
                                if k not in {'expected_revision', 'frozen_at'}})
            existing = tx.list('plans', target_session=target)
            if existing:
                if existing[0]['plan_hash'] == plan_hash:
                    return {'state': 'ALREADY_APPLIED', 'plan_id': existing[0]['plan_id']}
                raise LedgerError('CONFLICT', '目标交易日已有冻结计划，不可改写')
            self._session(target)
            self._before_cutoff(target)
            if not tx.state.get('simulation_start_session') or target < tx.state['simulation_start_session']:
                raise LedgerError('INVALID_INPUT', '账户尚未进入观察期')
            if payload.get('account_id', account_id) != account_id:
                raise LedgerError('INVALID_INPUT', '计划账户不匹配')
            if payload.get('strategy_id', tx.account['strategy_id']) != tx.account['strategy_id']:
                raise LedgerError('INVALID_INPUT', '计划策略与账户不匹配')
            previous = self._adjacent('previous_session', target, cutoff=payload['input_cutoff'])
            as_of = session_date(payload['market_as_of'])
            if as_of != previous:
                raise LedgerError('INVALID_INPUT', '当前前向计划须使用目标日前一交易日行情')
            cutoff = aware_time(payload['input_cutoff'])
            generated = aware_time(payload['generated_at'])
            if generated > self._now() or cutoff > generated:
                raise LedgerError('INVALID_INPUT', '输入截止/计划生成时点不合法')
            evidence = self._verify_evidence(payload, required=True)
            if decision is not None:
                strategy_evidence = self._verify_evidence({
                    'input_cutoff': payload['input_cutoff'],
                    'market_snapshot_ids': payload.get('strategy_market_snapshot_ids', []),
                    'data_evidence': payload.get('strategy_data_evidence', []),
                })
                for snap in strategy_evidence['snapshots']:
                    if snap['body']['content']['as_of'] > as_of:
                        raise LedgerError('INVALID_INPUT', '策略快照行情晚于声明的行情截止')
            for snap in evidence['snapshots']:
                content = snap['body']['content']
                if content['as_of'] > as_of:
                    raise LedgerError('INVALID_INPUT', '快照行情晚于声明的行情截止')
            account = self._account_state_tx(tx, target)
            snapshot = bind_account_snapshot(payload, account, self._now())
            if not account['can_plan']:
                raise LedgerError('WAITING_DATA', '账户状态不允许形成新计划',
                                  blocking_reasons=account['blocking_reasons'])
            intents = payload.get('orders', payload.get('intents', []))
            limit = daily_symbol_limit(payload.get('rule_policy', self.settings.raw['execution']),
                                       tx.account['strategy_id'])
            if not isinstance(intents, list) or limit is not None and len(intents) > limit:
                raise LedgerError('INVALID_INPUT', '每日交易代码数超过冻结政策上限')
            if not intents and not payload.get('no_order_reason'):
                raise LedgerError('INVALID_INPUT', '空计划必须有明确理由')
            codes = [normalize_code(order['code']) for order in intents]
            if len(set(codes)) != len(codes):
                raise LedgerError('INVALID_INPUT', '同一股票每日只能一个方向一个合并订单')
            fees = payload.get('fee_policy', self.settings.raw['fees'])
            rules = payload.get('rule_policy', self.settings.raw['execution'])
            if canonical(fees) != canonical(self.settings.raw['fees']):
                raise LedgerError('INVALID_INPUT', '冻结费用与当前有效配置不一致')
            if canonical(rules) != canonical(self.settings.raw['execution']):
                raise LedgerError('INVALID_INPUT', '冻结执行规则与当前配置不一致')
            ruleset = bind_policy_bundle(self, payload, target, payload['input_cutoff'], fees, rules)
            security_facts = capture_security_facts(self, codes, payload['input_cutoff'])
            event_facts = {code: {'method': 'events',
                'kwargs': {'code': code, 'knowledge_cutoff': payload['input_cutoff']},
                'result': self.market.events(code, knowledge_cutoff=payload['input_cutoff']).as_dict()}
                for code in codes}
            if any(q['result']['state'] == 'CORRUPT' for q in event_facts.values()):
                raise LedgerError('INTEGRITY_ERROR', '盘前事项来源原件损坏')
            # The policy fixes submission order at freeze, not during replay.
            paired = sorted(zip(intents, codes), key=lambda pair: pair[0]['side'] != 'SELL')
            orders, reservations, total = [], [], 0
            for index, (order, code) in enumerate(paired):
                self._evidence_for(evidence, code, as_of)
                side = order['side']
                quantity = integer(order['quantity'], minimum=1)
                price = number(order['limit_price'], positive=True)
                if quantity > MAX_ORDER_SHARES:
                    raise LedgerError('INVALID_INPUT', '单笔普通主板限价单不得超过100万股')
                if price % Decimal(rules['price_tick_yuan']):
                    raise LedgerError('INVALID_INPUT', '限价须为0.01元整数倍')
                if side not in {'BUY', 'SELL'} or (side == 'BUY' and quantity % rules['buy_lot_size']):
                    raise LedgerError('INVALID_INPUT', '方向或买入整手数量无效')
                order_id = identifier(order.get('order_id', f'{plan_id}:{code}'), '订单ID')
                reserve = {'order_id': order_id, 'code': code, 'side': side,
                           'cash_fen': 0, 'shares': 0, 'status': 'ACTIVE', 'lot_allocations': []}
                if side == 'BUY':
                    expense = fee_breakdown(price, quantity, side, fees, target)
                    reserve['cash_fen'] = -expense['net_cash_fen']
                    if order.get('reserved_cash_fen') is not None and order['reserved_cash_fen'] != reserve['cash_fen']:
                        raise LedgerError('INVALID_INPUT', '执行层预留需求与冻结费用复算不符')
                    total += reserve['cash_fen']
                else:
                    available = next((p['sellable_shares'] for p in account['positions']
                                      if p['code'] == code), 0)
                    if quantity > available or (quantity % 100 and quantity % 100 != available % 100):
                        raise LedgerError('INVALID_INPUT', '卖出数量超过可卖量或未一次处理零股余额')
                    reserve['shares'] = quantity
                    if order.get('reserved_shares') is not None and order['reserved_shares'] != quantity:
                        raise LedgerError('INVALID_INPUT', '卖单预留股数与原数量不符')
                    reserve['lot_allocations'] = self._fifo_allocations(tx, code, quantity, target)
                reservations.append(reserve)
                orders.append({'order_id': order_id, 'plan_id': plan_id, 'code': code,
                    'side': side, 'quantity': quantity, 'limit_price': format(price, 'f'),
                    'target_session': target, 'order_index': index,
                    'reserved_cash_fen': reserve['cash_fen'], 'reserved_shares': reserve['shares'],
                    'status': 'PENDING', 'reason': order.get('reason', '')})
            if total > account['available_cash_fen']:
                raise LedgerError('INVALID_INPUT', '买单预留合计超过确定可用现金')
            task_id = payload.get('task_id')
            if task_id:
                task = tx.get('rebalance_tasks', task_id)
                if not task or target not in task['sessions']:
                    raise LedgerError('INVALID_INPUT', '调仓子计划不在父任务交易日窗口内')
                if task.get('policy_hash') or payload.get('provenance_mode') == 'STRATEGY':
                    transition = self._rebalance_policy_tx(tx, task, target)
                    if transition is not None and decision is not None:
                        frozen_parent = decision['core']['input_bundle']['context']['parent_task']
                        if (frozen_parent is None or frozen_parent.get('task_id') != task_id
                                or frozen_parent.get('policy_transition') != transition):
                            raise LedgerError('INTEGRITY_ERROR', '冻结输入缺少原父任务的已验证政策衔接')
                for order in orders:
                    goal = next((g for g in task['targets'] if g['code'] == order['code']
                                 and g['side'] == order['side']), None)
                    key = f'{order["code"]}:{order["side"]}'
                    pending = sum(o['quantity'] for o in tx.list('orders')
                                  if o['plan_id'] in task.get('child_plan_ids', [])
                                  and o['code'] == order['code'] and o['side'] == order['side']
                                  and o['status'] in {'PENDING', 'WAITING_DATA'})
                    done = task.get('completed_quantities', {}).get(key, 0)
                    if not goal or order['quantity'] + pending + done > goal['quantity']:
                        raise LedgerError('INVALID_INPUT', '子计划超出父任务剩余调仓目标')
            plan = {**payload, 'plan_id': plan_id, 'account_id': account_id,
                    'provenance_mode': payload.get('provenance_mode', 'MANUAL'),
                    'decision_ref': payload.get('decision_ref'),
                    'strategy_id': tx.account['strategy_id'], 'plan_hash': plan_hash,
                    'frozen_at': tx.now, 'market_as_of': as_of, 'target_session': target,
                    'account_revision_at_freeze': tx.state['account_revision'],
                    'buy_budget_fen': total, 'order_ids': [o['order_id'] for o in orders],
                    'status': 'FROZEN' if orders else 'EMPTY', 'fee_policy': fees,
                    'rule_policy': rules, 'fee_policy_hash': digest(fees),
                    'rule_policy_hash': digest(rules), 'orders': orders,
                    'account_snapshot': snapshot, 'security_facts': security_facts, 'event_facts': event_facts,
                    'ruleset': ruleset, 'ruleset_hash': ruleset['ruleset_hash'],
                    'reservations': reservations,
                    'original_resources': capture_original_resources(tx, snapshot, orders, reservations)}
            plan['document'] = document(self.root, 'frozen', plan)
            from china_stock_sim.ledger.plan_storage import plan_header
            tx.append('plans', plan_id, plan_header(plan))
            self._freeze_registered_decision_tx(tx, registered_decision, plan)
            for order, reserve in zip(orders, reservations):
                tx.append('orders', order['order_id'], order)
                tx.append('reservations', order['order_id'], reserve)
            if task_id:
                task['child_plan_ids'] = [*task.get('child_plan_ids', []), plan_id]
                tx.save('rebalance_tasks', task_id, task)
            return {'plan_id': plan_id, 'plan_hash': plan_hash, 'buy_budget_fen': total,
                    'frozen_at': tx.now, 'order_ids': plan['order_ids']}
        return self._mutate(account_id, operation_id, {'action': 'freeze_plan', **payload},
                            expected_revision, target, apply)

    def record_no_plan(self, account_id, payload, *, operation_id, expected_revision):
        payload = self._payload(payload)
        target = payload.get('target_session', payload.get('effective_date'))

        def apply(tx):
            self._session(target)
            if tx.list('plans', target_session=target):
                raise LedgerError('CONFLICT', '已有冻结计划不能标记为无计划')
            reason = identifier(payload['reason'], '无计划原因')
            record = {**payload, 'kind': 'NO_PLAN', 'target_session': target,
                      'status': reason, 'effective_date': target, 'processed_at': tx.now}
            tx.save('job_runs', f'no-plan:{target}', record)
            return {'target_session': target, 'plan_status': reason}
        return self._mutate(account_id, operation_id, {'action': 'no_plan', **payload},
                            expected_revision, target, apply, financial=False)

    def _fifo_allocations(self, tx, code, quantity, session):
        reserved = {}
        for row in tx.list('reservations', status='ACTIVE'):
            for allocation in row.get('lot_allocations', []):
                reserved[allocation['lot_id']] = reserved.get(allocation['lot_id'], 0) + allocation['quantity']
        lots = sorted((lot for lot in tx.list('position_lots', code=code)
                       if lot['quantity'] > 0 and lot.get('sellable_date') and lot['sellable_date'] <= session),
                      key=lambda item: (item['acquired_date'], item['lot_id']))
        needed, allocations = quantity, []
        for lot in lots:
            take = min(needed, max(0, lot['quantity'] - reserved.get(lot['lot_id'], 0)))
            if take:
                allocations.append({'lot_id': lot['lot_id'], 'quantity': take})
                needed -= take
            if not needed:
                break
        if needed:
            raise LedgerError('WAITING_DATA', '可卖批次不足或可卖日期未知')
        return allocations

    def apply_execution_results(self, account_id, payload, *, operation_id, expected_revision):
        payload = self._payload(payload)
        results = payload.get('results')
        if results is None:
            return self._apply_execution(account_id, payload, operation_id=operation_id,
                                         expected_revision=expected_revision)
        if not isinstance(results, list) or not results:
            return {'state': 'INVALID_INPUT', 'reason': '批量结果须为非空列表'}
        return apply_execution_batch(self, account_id, payload, operation_id=operation_id,
                                     expected_revision=expected_revision)

    def _apply_execution(self, account_id, payload, *, operation_id, expected_revision):
        target = payload.get('target_session', payload.get('effective_date'))
        business = {k: v for k, v in payload.items() if k not in {'computed_at', 'processed_at'}}

        def apply(tx):
            order = tx.get('orders', payload['order_id'])
            if not order:
                raise LedgerError('INVALID_INPUT', '订单不存在')
            from china_stock_sim.ledger.plan_storage import resolve_plan
            plan = resolve_plan(self.root, tx.get('plans', order['plan_id']))
            for field in ('plan_id', 'code', 'side', 'target_session'):
                if payload.get(field) != order[field]:
                    raise LedgerError('INVALID_INPUT', '结果与原冻结订单不一致', field=field)
            if payload.get('account_id') != account_id:
                raise LedgerError('INVALID_INPUT', '结果缺账户或跨账户')
            self._session(target)
            if target > self._now().astimezone(self.settings.timezone).date().isoformat():
                raise LedgerError('NOT_DUE', '目标交易日尚未到达')
            validate_result_contract(plan, payload)
            state = payload['state']
            if state not in {'SIMULATED_FILL', 'NOT_FILLED', 'WAITING_DATA'}:
                raise LedgerError('INVALID_INPUT', '执行状态无效')
            result_hash = digest(business)
            if order['status'] in {'FILLED', 'NOT_FILLED'}:
                if order.get('result_hash') == result_hash:
                    return {'state': 'ALREADY_APPLIED', 'order_id': order['order_id']}
                raise LedgerError('CONFLICT', '订单已终结，矛盾结果须显式更正')
            verify_plan_decision(self.root, plan, market=self.market, materialize=False)
            result_id = identifier(payload['result_id'], '结果ID')
            computed = aware_time(payload.get('computed_at', tx.now))
            if computed > self._now() or computed < aware_time(plan['frozen_at']):
                raise LedgerError('INVALID_INPUT', '计算时点须介于计划封存和实际处理时点之间')
            if payload.get('knowledge_cutoff') and aware_time(payload['knowledge_cutoff']) > computed:
                raise LedgerError('INVALID_INPUT', '计算时点早于证据知识截止')
            if state != 'WAITING_DATA':
                deadline = datetime.combine(datetime.fromisoformat(target).date(), time(18, 10), SHANGHAI)
                if self._now() < deadline:
                    raise LedgerError('NOT_DUE', '目标日18:10全日数据处理时点尚未到达')
                if computed < deadline:
                    raise LedgerError('INVALID_INPUT', '全日终态不能在目标日18:10之前计算')
                if not payload.get('knowledge_cutoff') or aware_time(payload['knowledge_cutoff']) < deadline:
                    raise LedgerError('INVALID_INPUT', '全日终态知识截止不得早于目标日18:10')
            validate_account_view_reference(tx, plan, payload)
            evidence = self._verify_evidence(payload, required=state != 'WAITING_DATA')
            if state != 'WAITING_DATA':
                validate_terminal_proof(self, plan, order, payload, evidence)
            if state == 'SIMULATED_FILL':
                self._evidence_for(evidence, order['code'], target, exact=True)
            if state == 'SIMULATED_FILL':
                self._execution_evidence(evidence, payload, order['code'], target)
            reserve = tx.get('reservations', order['order_id'])
            if not reserve or reserve['status'] != 'ACTIVE':
                raise LedgerError('INTEGRITY_ERROR', '原单预留丢失')
            fees_names = ('amount_fen', 'commission_fen', 'stamp_fen', 'transfer_fen', 'fees_fen', 'net_cash_fen')
            from china_stock_sim.common.execution_views import stored_result
            saved_result = stored_result(self.root, payload)
            if state == 'WAITING_DATA':
                if payload.get('quantity') is not None or payload.get('price') is not None or any(
                        payload.get(name) is not None for name in fees_names):
                    raise LedgerError('INVALID_INPUT', '待数据结果的成交数量、价格和费用须为null')
                order.update(status='WAITING_DATA', last_attempt=saved_result, reason=payload.get('reason', 'WAITING_DATA'))
                tx.save('orders', order['order_id'], order)
                return {'state': 'WAITING_DATA', 'order_id': order['order_id'], 'reservation_retained': True}
            if state == 'NOT_FILLED':
                if type(payload['quantity']) is not int or payload['quantity'] != 0 or payload['price'] is not None or any(
                        type(payload.get(name)) is not int or payload[name] != 0 for name in fees_names):
                    raise LedgerError('INVALID_INPUT', '未成交数量/费用必须为0，价格null')
                order.update(status='NOT_FILLED', result_hash=result_hash, result=saved_result,
                             reason=payload.get('reason', 'NOT_FILLED'))
                tx.save('orders', order['order_id'], order)
                reserve['status'] = 'RELEASED'
                tx.save('reservations', order['order_id'], reserve)
                return {'order_id': order['order_id'], 'execution_state': 'NOT_FILLED'}
            if integer(payload['quantity'], minimum=1) != order['quantity']:
                raise LedgerError('INVALID_INPUT', '当前模型只接受原单全量成交')
            price = number(payload['price'], positive=True)
            limit = Decimal(order['limit_price'])
            if price % Decimal('0.01') or (order['side'] == 'BUY' and price > limit) or (
                    order['side'] == 'SELL' and price < limit):
                raise LedgerError('INVALID_INPUT', '成交价格违反限价或价位')
            calculated = fee_breakdown(price, order['quantity'], order['side'], plan['fee_policy'], target)
            if any(type(payload.get(key)) is not int or payload[key] != value
                   for key, value in calculated.items()):
                raise LedgerError('INVALID_INPUT', '成交金额、费用分项或现金变化复算不符')
            for key, expected in [('fee_policy_hash', plan['fee_policy_hash']),
                                  ('rule_policy_hash', plan['rule_policy_hash'])]:
                if payload.get(key) != expected:
                    raise LedgerError('INVALID_INPUT', '结果缺少或不匹配冻结政策摘要', field=key)
            if payload.get('execution_model') != plan['rule_policy']['model']:
                raise LedgerError('INVALID_INPUT', '执行模型与冻结规则不符')
            guard = assert_execution_guards(self, tx, plan, order,
                expense_fen=-calculated['net_cash_fen'] if order['side'] == 'BUY' else None)
            fill = {**saved_result, **calculated, 'commit_guard_hash': guard['view_hash'], 'fill_id': order['order_id'], 'result_id': result_id,
                    'effective_date': target, 'processed_at': tx.now, 'lot_allocations': [],
                    'cost_fen': 0, 'realized_pnl_fen': 0}
            if order['side'] == 'BUY':
                expense = -calculated['net_cash_fen']
                spent = sum(-f['net_cash_fen'] for f in tx.list('fills', plan_id=plan['plan_id'], side='BUY'))
                other_reserved = sum(r['cash_fen'] for r in tx.list('reservations', status='ACTIVE')
                                     if r['order_id'] != order['order_id'])
                tax_items = [t for t in tx.list('tax_items') if t.get('status') != 'NOT_TRIGGERED']
                if any(t.get('assessed_fen') is None for t in tax_items):
                    raise LedgerError('WAITING_DATA', '新增未知税负影响原买单现金，保留原单待核')
                tax_payable = sum(max(0, t['assessed_fen'] - t.get('paid_fen', 0)) for t in tax_items)
                if expense > reserve['cash_fen'] or spent + expense > plan['buy_budget_fen']:
                    raise LedgerError('CONFLICT', '成交超出冻结买入预算，不能使用同期开盘卖款')
                if expense > tx.balance() - other_reserved - tax_payable:
                    raise LedgerError('WAITING_DATA', '现金不足，保留原单待核')
                sellable = self._adjacent('next_session', target)
                lot_id = f'buy:{order["order_id"]}'
                lot = {'lot_id': lot_id, 'code': order['code'], 'quantity': order['quantity'],
                       'cost_fen': expense, 'original_quantity': order['quantity'], 'original_cost_fen': expense,
                       'acquired_date': target, 'sellable_date': sellable,
                       'tax_start_date': target, 'tax_start_status': 'KNOWN',
                       'source_order_id': order['order_id']}
                tx.append('position_lots', lot_id, lot)
                tx.append('lot_movements', f'buy:{order["order_id"]}', {
                    'movement_id': f'buy:{order["order_id"]}', 'lot_id': lot_id, 'code': order['code'],
                    'effective_date': target, 'quantity_delta': order['quantity'],
                    'cost_delta_fen': expense, 'kind': 'BUY', 'reference_id': order['order_id']})
            else:
                cost, allocations = 0, []
                for allocation in reserve['lot_allocations']:
                    lot = tx.get('position_lots', allocation['lot_id'])
                    quantity = allocation['quantity']
                    if not lot or lot['quantity'] < quantity or lot['sellable_date'] > target:
                        raise LedgerError('CONFLICT', '冻结可卖批次已不满足原单')
                    assigned = lot['cost_fen'] if quantity == lot['quantity'] else int(
                        (Decimal(lot['cost_fen']) * quantity / lot['quantity']).quantize(
                            Decimal('1'), rounding=ROUND_HALF_UP))
                    lot['quantity'] -= quantity
                    lot['cost_fen'] -= assigned
                    tx.save('position_lots', lot['lot_id'], lot)
                    movement_id = f'sell:{order["order_id"]}:{lot["lot_id"]}'
                    tx.append('lot_movements', movement_id, {
                        'movement_id': movement_id, 'lot_id': lot['lot_id'], 'code': order['code'],
                        'effective_date': target, 'quantity_delta': -quantity, 'cost_delta_fen': -assigned,
                        'kind': 'SELL', 'reference_id': order['order_id'],
                        'acquired_date': lot['acquired_date'], 'tax_start_date': lot.get('tax_start_date'),
                        'tax_start_status': lot.get('tax_start_status', 'RULE_PENDING')})
                    allocations.append({'lot_id': lot['lot_id'], 'quantity': quantity, 'cost_fen': assigned,
                        'acquired_date': lot['acquired_date'], 'tax_start_date': lot.get('tax_start_date'),
                        'tax_start_status': lot.get('tax_start_status', 'RULE_PENDING')})
                    cost += assigned
                fill.update(cost_fen=cost, realized_pnl_fen=calculated['net_cash_fen'] - cost,
                            lot_allocations=allocations)
            direction = -1 if order['side'] == 'BUY' else 1
            tx.cash(f'trade:{order["order_id"]}', order['side'], direction * calculated['amount_fen'],
                    target, order['order_id'])
            for name in ('commission_fen', 'stamp_fen', 'transfer_fen'):
                if calculated[name]:
                    tx.cash(f'{name}:{order["order_id"]}', name.upper(), -calculated[name], target,
                            order['order_id'])
            tx.append('fills', order['order_id'], fill)
            reserve['status'] = 'RELEASED'
            tx.save('reservations', order['order_id'], reserve)
            order.update(status='FILLED', result_hash=result_hash, result=saved_result, reason=payload.get('reason', 'FILLED'))
            tx.save('orders', order['order_id'], order)
            if order['side'] == 'SELL':
                self._on_sale(tx, fill)
            task_id = plan.get('task_id')
            if task_id:
                task = tx.get('rebalance_tasks', task_id)
                completed = task.setdefault('completed_quantities', {})
                key = f'{order["code"]}:{order["side"]}'
                completed[key] = completed.get(key, 0) + order['quantity']
                tx.save('rebalance_tasks', task_id, task)
            return {'order_id': order['order_id'], 'fill_id': order['order_id'],
                    'execution_state': 'SIMULATED_FILL', 'cash_fen': tx.balance(),
                    'commit_guard_hash': guard['view_hash'],
                    'commit_guard_revision': guard['account_revision']}
        return self._mutate(account_id, operation_id, {'action': 'execution_result', **business},
                            expected_revision, target, apply)
