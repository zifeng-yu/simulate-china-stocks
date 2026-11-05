"""Account corporate-action lifecycle using only public, evidenced market facts."""

from copy import deepcopy
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from china_stock_sim.ledger.evidence import document
from china_stock_sim.ledger.kernel import LedgerError, digest
from china_stock_sim.ledger.tax import (
    allocate_fen,
    decimal,
    dividend_basis,
    dividend_tax_rate,
    holding_rule,
    money_fen,
)
from china_stock_sim.market.contracts import aware_time, session_date
from china_stock_sim.market.quality import normalize_code


def _key(*parts):
    return ':'.join(str(p) for p in parts)


def _aliases(event):
    return sorted({event['event_id'], *(e['event_id'] for e in event.get('evidence', []))})


def _compatible(old, new):
    for field in ('record_date', 'ex_date', 'payment_date', 'share_listing_date',
                  'cash_per_share', 'bonus_per_share', 'capital_per_share'):
        before, after = old.get(field), new.get(field)
        if before is None:
            continue
        if after is None:
            return False
        if field.endswith('_per_share'):
            if decimal(before) != decimal(after):
                return False
        elif before != after:
            return False
    return True


class CorporateActionsMixin:
    @staticmethod
    def _mark_restatement(tx, effective_date):
        if tx.state.get('settled_through') and effective_date <= tx.state['settled_through']:
            tx.state['restate_from'] = min(tx.state.get('restate_from') or effective_date, effective_date)
            sessions = [r.get('session', r.get('target_session', '')) for r in tx.list('daily_nav')]
            tx.state['restate_through'] = max(tx.state.get('restate_through') or '',
                                             tx.state['settled_through'], *sessions)

    def _corporate_day(self, payload):
        day = session_date(payload['effective_date'])
        if day > self._now().astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat():
            raise LedgerError('INVALID_INPUT', '不能提前确认未来权益或扣税')
        return day

    def _action_fact(self, payload, operation, case=None):
        verified = self._verify_evidence(payload, required=True)
        day = self._corporate_day(payload)
        code = normalize_code(case['code'] if case else payload['code'])
        cutoff = payload.get('knowledge_cutoff') or payload.get('input_cutoff')
        if not cutoff or aware_time(cutoff) > self._now():
            raise LedgerError('INVALID_INPUT', '权益事实需要不晚于实际处理时间的知识截止')
        identities = payload.get('event_ids') or (case['event_ids'] if case else None)
        result = self.market.event_settlement(code, day, knowledge_cutoff=cutoff,
                                              event_ids=identities, operation=operation)
        supported_ids = {identity for snapshot in verified.get('snapshots', [])
                         for identity in snapshot['body']['observation_ids']}
        supported_ids.update(identity for query in verified.get('query_results', [])
                             for identity in query.get('observation_ids', []))
        if set(result.observation_ids) - supported_ids:
            raise LedgerError('WAITING_DATA', '权益动作的事实未包含在所提供证据中')
        if result.state == 'CORRUPT':
            raise LedgerError('INTEGRITY_ERROR', '权益来源证据损坏', issues=result.issues)
        return result

    def register_entitlements(self, account_id, payload, *, operation_id, expected_revision):
        """Register historical record-date holdings; registration itself creates no asset."""
        facts = self._action_fact(payload, 'eligibility')
        day = self._corporate_day(payload)

        def apply(tx):
            results = []
            for event in facts.data:
                aliases = _aliases(event)
                cases = [c for c in tx.list('corporate_action_cases', code=event['code'])
                         if c.get('status') != 'MERGED']
                candidates = [c for c in cases if set(c['event_ids']) & set(aliases)]
                if not candidates:
                    candidates = [c for c in cases if event.get('proposal_date') and
                                  c.get('proposal_date') == event['proposal_date']]
                uncertain_identity = False
                if not candidates:
                    candidates = [c for c in cases if not event.get('proposal_date') and (
                        (event.get('record_date') and c.get('record_date') == event['record_date']) or
                        (event.get('ex_date') and c['event'].get('ex_date') == event['ex_date']) or
                        (not c.get('record_date') and not c['event'].get('ex_date')))]
                    uncertain_identity = bool(candidates)
                withdrawn = candidates and candidates[0]['event'].get('status') == 'IMPLEMENTED' and event.get('status') != 'IMPLEMENTED'
                if uncertain_identity or withdrawn or len(candidates) > 1 or (candidates and not _compatible(candidates[0]['event'], event)):
                    for case in candidates:
                        case['status'] = 'IDENTITY_PENDING' if uncertain_identity or len(candidates) > 1 else 'REVISION_PENDING'
                        case['revision_candidate'] = event
                        case['reason'] = 'EVENT_IDENTITY_OR_TERMS_CONFLICT'
                        tx.save('corporate_action_cases', case['case_id'], case)
                    results.append({'state': 'CONFLICT', 'reason': 'EVENT_IDENTITY_OR_TERMS_CONFLICT'})
                    continue
                case = candidates[0] if candidates else {
                    'case_id': 'case:' + digest([account_id, event['code'], event['event_id']])[:24],
                    'code': event['code'], 'event_ids': [], 'eligibility': [], 'actions': {},
                    'proposal_date': event.get('proposal_date'), 'record_date': event.get('record_date'),
                }
                case['event_ids'] = sorted(set(case['event_ids']) | set(aliases))
                case['event'] = event
                case['record_date'] = event.get('record_date')
                case['evidence'] = deepcopy(payload)
                check = event['settlement_readiness']
                if case['actions'].get('eligibility', {}).get('state') == 'APPLIED':
                    if check['state'] == 'READY' and event['operation_date'] <= day:
                        if case.get('status') in {'WAITING_DATA', 'IDENTITY_PENDING', 'REVISION_PENDING'}:
                            self._mark_restatement(tx, case['record_date'])
                        case['status'] = 'REGISTERED' if case['eligibility'] else 'NO_ENTITLEMENT'
                        case.pop('reason', None)
                        case.pop('revision_candidate', None)
                    tx.save('corporate_action_cases', case['case_id'], case)
                    results.append({'case_id': case['case_id'], 'state': 'ALREADY_APPLIED'
                                    if check['state'] == 'READY' else 'WAITING_DATA'})
                    continue
                if check['state'] != 'READY':
                    state, reason = 'WAITING_DATA', check['issues']
                elif not event.get('operation_applicable', True):
                    state, reason = 'NOT_APPLICABLE', 'NO_DISTRIBUTION'
                elif event['operation_date'] > day:
                    state, reason = 'NOT_DUE', event['operation_date']
                else:
                    unresolved = [o for o in tx.list('orders', code=case['code']) if
                                  o['target_session'] <= case['record_date'] and
                                  o.get('state', o.get('status')) in {'PENDING', 'WAITING_DATA'}]
                    if unresolved:
                        state, reason = 'WAITING_DATA', 'RECORD_DATE_TRADES_UNRESOLVED'
                    else:
                        allocations = []
                        for lot in tx.list('position_lots', code=case['code']):
                            moves = [m for m in tx.list('lot_movements', lot_id=lot['lot_id'])
                                     if m['effective_date'] <= case['record_date']]
                            qty = sum(m['quantity_delta'] for m in moves)
                            cost = sum(m['cost_delta_fen'] for m in moves)
                            if qty > 0:
                                allocations.append({k: lot.get(k) for k in
                                                    ('lot_id', 'acquired_date', 'tax_start_date',
                                                     'tax_start_status')} | {'quantity': qty, 'cost_fen': cost})
                        case['eligibility'] = sorted(allocations, key=lambda a: (a['acquired_date'], a['lot_id']))
                        state, reason = 'APPLIED', None
                case['actions']['eligibility'] = {'state': state, 'reason': reason,
                                                   'operation_date': event.get('operation_date')}
                case['status'] = ('REGISTERED' if case['eligibility'] else 'NO_ENTITLEMENT') if state in {
                    'APPLIED', 'NOT_APPLICABLE'} else 'WAITING_DATA'
                tx.save('corporate_action_cases', case['case_id'], case)
                results.append({'case_id': case['case_id'], 'state': state, 'reason': reason})
            return {'state': 'APPLIED' if facts.ready and all(r['state'] in {
                'APPLIED', 'ALREADY_APPLIED', 'NOT_APPLICABLE'} for r in results) else 'WAITING_DATA',
                'cases': results, 'issues': list(facts.issues)}
        return self._mutate(account_id, operation_id, payload, expected_revision, day, apply)

    @staticmethod
    def _case_has_economics(tx, case_id):
        return (any(row.get('case_id') == case_id for row in tx.list('entitlements')) or
                any(row.get('source_case_id') == case_id for row in tx.list('position_lots')) or
                any(row.get('reference_id') == case_id for row in tx.list('cash_entries')) or
                any(row.get('reference_id') == case_id for row in tx.list('lot_movements')) or
                any(row.get('case_id') == case_id and (row.get('paid_fen') or
                    row.get('assessed_fen') not in (None, 0)) for row in tx.list('tax_items')))

    def resolve_event_identity(self, account_id, payload, *, operation_id, expected_revision):
        """Merge only empty aliases into one stable case, with current public evidence."""
        day = self._corporate_day(payload)

        def apply(tx):
            case = tx.get('corporate_action_cases', payload['case_id'])
            if not case or case.get('status') == 'MERGED':
                raise LedgerError('INVALID_INPUT', '必须选择仍有效的稳定权益案')
            if payload.get('code') and normalize_code(payload['code']) != case['code']:
                raise LedgerError('INVALID_INPUT', '身份恢复证券与稳定案不符')
            merge_ids = payload.get('merge_case_ids', [])
            if (not isinstance(merge_ids, list) or any(not isinstance(x, str) for x in merge_ids)
                    or len(merge_ids) != len(set(merge_ids)) or case['case_id'] in merge_ids):
                raise LedgerError('INVALID_INPUT', 'merge_case_ids 必须是无重复的其他权益案 ID')
            identities = payload.get('event_ids')
            if not isinstance(identities, list) or not identities:
                raise LedgerError('INVALID_INPUT', '身份恢复需要明确源 event_ids')
            facts = self._action_fact(payload, 'eligibility', case)
            selected = [event for event in facts.data if set(identities) <= set(_aliases(event))]
            if len(selected) != 1 or selected[0]['settlement_readiness']['state'] != 'READY':
                return {'state': 'WAITING_DATA', 'reason': 'UNIQUE_EVENT_IDENTITY_EVIDENCE_REQUIRED'}
            event = selected[0]
            if event['operation_date'] > day:
                return {'state': 'NOT_DUE', 'operation_date': event['operation_date']}
            others = [tx.get('corporate_action_cases', identity) for identity in merge_ids]
            if any(item is None for item in others):
                raise LedgerError('INVALID_INPUT', '拟归并权益案不存在')
            for other in others:
                if other.get('status') == 'MERGED' and other.get('merged_into') != case['case_id']:
                    raise LedgerError('CONFLICT', '权益案已经归并到其他身份')
            active = [other for other in others if other.get('status') != 'MERGED']
            for item in [case, *active]:
                linked = (set(item['event_ids']) & set(_aliases(event)) or
                          (event.get('proposal_date') and item.get('proposal_date') == event['proposal_date']))
                if item['code'] != event['code'] or not linked or not _compatible(item['event'], event):
                    raise LedgerError('CONFLICT', '源证据不能证明这些权益案具有相同身份与条款')
            if any(self._case_has_economics(tx, other['case_id']) for other in active):
                raise LedgerError('CONFLICT', '不能归并已有经济记录的权益案；稳定案必须保留经济身份')
            # A public event cannot be attached to an unmentioned live case as well.
            involved = {case['case_id'], *merge_ids}
            for other in tx.list('corporate_action_cases', code=case['code']):
                if other['case_id'] not in involved and other.get('status') != 'MERGED' and (
                        set(other['event_ids']) & set(_aliases(event)) or
                        (event.get('proposal_date') and other.get('proposal_date') == event['proposal_date'])):
                    raise LedgerError('CONFLICT', '仍有未列入身份核验的疑似权益案')
            proof = document(self.root, 'event-identity', {'payload': payload, 'event': event})
            case['event_ids'] = sorted({*case['event_ids'], *_aliases(event),
                                       *(alias for other in active for alias in other['event_ids'])})
            case.update(event=event, evidence=deepcopy(payload), identity_document=proof)
            case.pop('reason', None)
            case.pop('revision_candidate', None)
            if case['actions'].get('eligibility', {}).get('state') == 'APPLIED':
                case['status'] = 'REGISTERED' if case['eligibility'] else 'NO_ENTITLEMENT'
            else:
                case.update(status='WAITING_DATA', reason='ELIGIBILITY_REGISTRATION_REQUIRED')
            tx.save('corporate_action_cases', case['case_id'], case)
            for other in active:
                other.update(status='MERGED', merged_into=case['case_id'], identity_document=proof,
                             merged_at=tx.now)
                tx.save('corporate_action_cases', other['case_id'], other)
            correction_id = _key('identity-resolution', operation_id)
            tx.append('corrections', correction_id, {'correction_id': correction_id,
                'kind': 'EVENT_IDENTITY_RESOLUTION', 'case_id': case['case_id'],
                'merge_case_ids': merge_ids, 'event_ids': case['event_ids'],
                'effective_date': day, 'document': proof})
            if case.get('record_date'):
                self._mark_restatement(tx, case['record_date'])
            return {'state': 'APPLIED', 'case_id': case['case_id'], 'status': case['status'],
                    'merged_case_ids': merge_ids, 'document': proof}
        return self._mutate(account_id, operation_id, payload, expected_revision, day, apply)

    def resolve_tax_holding_rule(self, account_id, payload, *, operation_id, expected_revision):
        """Resolve pending share tax starts using a maintained, immutable rule document."""
        day = self._corporate_day(payload)

        def apply(tx):
            if any(key in payload for key in ('tax_start_date', 'start_date', 'start_basis', 'rule')):
                raise LedgerError('INVALID_INPUT', '税务起点只能由本项目规则配置推导')
            ids = payload.get('lot_ids')
            if (not isinstance(ids, list) or not ids or any(not isinstance(x, str) for x in ids)
                    or len(ids) != len(set(ids)) or not isinstance(payload.get('rule_id'), str)):
                raise LedgerError('INVALID_INPUT', '需要明确 rule_id 及无重复的 lot_ids')
            rule = holding_rule(self.root, payload['rule_id'])
            if rule is None:
                return {'state': 'WAITING_DATA', 'reason': 'TAX_HOLDING_RULE_UNKNOWN', 'lot_ids': ids}
            changes = []
            for identity in ids:
                lot = tx.get('position_lots', identity)
                if not lot or not lot.get('source_case_id'):
                    raise LedgerError('INVALID_INPUT', '仅支持有股份交付来源的批次')
                delivered = lot['acquired_date']
                if not rule['effective_from'] <= delivered <= rule.get('effective_to', '9999-12-31'):
                    raise LedgerError('CONFLICT', '规则生效区间不覆盖该批次股份交付日')
                if rule['start_basis'] == 'SHARE_DELIVERY':
                    start = delivered
                else:
                    original = tx.get('position_lots', lot.get('source_lot_id', ''))
                    if not original or not original.get('acquired_date'):
                        return {'state': 'WAITING_DATA', 'reason': 'ORIGINAL_SHARE_ACQUISITION_UNKNOWN',
                                'lot_id': identity}
                    start = original['acquired_date']
                session_date(start)
                if start > delivered:
                    raise LedgerError('CONFLICT', '税务起点不得晚于股份交付日')
                if lot.get('tax_start_status') == 'KNOWN':
                    if lot.get('tax_start_date') != start or lot.get('tax_holding_rule_hash') != digest(rule):
                        raise LedgerError('CONFLICT', '已确认税务起点或已应用规则不得静默改写')
                    continue
                if lot.get('tax_start_status') != 'RULE_PENDING':
                    raise LedgerError('CONFLICT', '该批次并非待核税务起点')
                changes.append((lot, start))
            if not changes:
                return {'state': 'ALREADY_APPLIED', 'lot_ids': ids}
            known_before = {item['tax_id']: item for item in tx.list('tax_items')
                            if item.get('rate') is not None and item.get('assessed_fen') is not None}
            proof = document(self.root, 'tax-holding-rules', rule)
            for lot, start in changes:
                lot.update(tax_start_date=start, tax_start_status='KNOWN', tax_holding_rule_id=rule['rule_id'],
                           tax_holding_rule_hash=digest(rule), tax_holding_rule_document=proof)
                tx.save('position_lots', lot['lot_id'], lot)
                for case in tx.list('corporate_action_cases', code=lot['code']):
                    matching = [item for item in case.get('eligibility', []) if item['lot_id'] == lot['lot_id']]
                    if matching:
                        for eligible in matching:
                            eligible.update(tax_start_date=start, tax_start_status='KNOWN',
                                            tax_holding_rule_document=proof)
                        tx.save('corporate_action_cases', case['case_id'], case)
                self._mark_restatement(tx, lot['acquired_date'])
            self._refresh_dividend_tax(tx, None, day)
            for identity, before in known_before.items():
                after = tx.get('tax_items', identity)
                if (not after or any(allocation not in after.get('allocations', [])
                                     for allocation in before.get('allocations', [])) or
                        after.get('assessed_fen', 0) < before['assessed_fen'] or
                        (after.get('allocations') == before.get('allocations') and
                         after.get('assessed_fen') != before['assessed_fen'])):
                    raise LedgerError('CONFLICT', '恢复规则不能改变已经确认的税款')
            correction_id = _key('tax-rule-resolution', operation_id)
            tx.append('corrections', correction_id, {'correction_id': correction_id,
                'kind': 'TAX_HOLDING_RULE_RESOLUTION', 'effective_date': day,
                'rule_id': rule['rule_id'], 'rule_hash': digest(rule), 'document': proof,
                'changes': [{'lot_id': lot['lot_id'], 'tax_start_date': start} for lot, start in changes]})
            return {'state': 'APPLIED', 'lot_ids': ids, 'rule_id': rule['rule_id'], 'document': proof}
        return self._mutate(account_id, operation_id, payload, expected_revision, day, apply)

    def _case_action(self, tx, payload, operation):
        case = tx.get('corporate_action_cases', payload['case_id'])
        if case is None:
            raise LedgerError('INVALID_INPUT', '未知权益case_id')
        if case.get('status') == 'MERGED':
            raise LedgerError('CONFLICT', '权益案已经归并，请使用稳定 case_id', case_id=case['merged_into'])
        prior = case['actions'].get(operation, {})
        if prior.get('state') in {'APPLIED', 'NOT_APPLICABLE'}:
            return case, None, {'state': 'ALREADY_APPLIED', 'operation': operation}
        if case['status'] in {'IDENTITY_PENDING', 'REVISION_PENDING'}:
            return case, None, {'state': 'WAITING_DATA', 'reason': case['status']}
        if case['status'] == 'NO_ENTITLEMENT':
            return case, None, {'state': 'NOT_APPLICABLE', 'reason': 'NO_ELIGIBLE_HOLDINGS'}
        if case['actions'].get('eligibility', {}).get('state') != 'APPLIED':
            return case, None, {'state': 'WAITING_DATA', 'reason': 'ELIGIBILITY_UNKNOWN'}
        result = self._action_fact(payload, operation, case)
        selected = [e for e in result.data if set(_aliases(e)) & set(case['event_ids'])]
        if len(selected) != 1:
            return case, None, {'state': 'WAITING_DATA', 'reason': 'EVENT_IDENTITY_UNKNOWN'}
        event = selected[0]
        if not _compatible(case['event'], event):
            case.update(status='REVISION_PENDING', revision_candidate=event)
            tx.save('corporate_action_cases', case['case_id'], case)
            return case, None, {'state': 'CONFLICT', 'reason': 'EVENT_TERMS_CHANGED'}
        check = event['settlement_readiness']
        if check['state'] != 'READY':
            outcome = {'state': 'WAITING_DATA', 'reason': check['issues']}
        elif event.get('operation_applicable') is False:
            outcome = {'state': 'NOT_APPLICABLE'}
        elif event['operation_date'] > payload['effective_date']:
            outcome = {'state': 'NOT_DUE', 'operation_date': event['operation_date']}
        else:
            case['event'] = event
            case['event_ids'] = sorted(set(case['event_ids']) | set(_aliases(event)))
            return case, event, None
        case['actions'][operation] = outcome
        tx.save('corporate_action_cases', case['case_id'], case)
        return case, None, outcome

    def recognize_entitlements(self, account_id, payload, *, operation_id, expected_revision):
        day = self._corporate_day(payload)
        operations = [payload['operation']] if payload.get('operation') else ['cash_accrual', 'share_accrual']
        if set(operations) - {'cash_accrual', 'share_accrual'}:
            raise LedgerError('INVALID_INPUT', '确认权益动作无效')

        def apply(tx):
            results = []
            for operation in operations:
                case, event, outcome = self._case_action(tx, payload, operation)
                if outcome:
                    results.append(outcome)
                    continue
                allocations = deepcopy(case['eligibility'])
                weights = [a['quantity'] for a in allocations]
                entitlement = {'case_id': case['case_id'], 'code': case['code'], 'status': 'RECEIVABLE',
                    'recognition_date': event['ex_date'], 'payment_date': event.get('payment_date'),
                    'listing_date': event.get('share_listing_date'), 'allocations': allocations,
                    'evidence': deepcopy(payload)}
                if operation == 'cash_accrual':
                    amount = money_fen(Decimal(sum(weights)) * decimal(event['cash_per_share']))
                    for allocation, fen in zip(allocations, allocate_fen(amount, weights), strict=True):
                        allocation['cash_fen'] = fen
                    entitlement.update(kind='CASH', amount_fen=amount, paid_fen=0, payments=[])
                else:
                    ratio = decimal(event['bonus_per_share']) + decimal(event['capital_per_share'])
                    total_quantity, total_cost = Decimal(0), 0
                    for allocation in allocations:
                        quantity = Decimal(allocation['quantity']) * ratio
                        original = allocation['cost_fen']
                        moved = int((Decimal(original) * ratio / (1 + ratio)).quantize(
                            Decimal('1'), rounding=ROUND_HALF_UP))
                        lot = tx.get('position_lots', allocation['lot_id'])
                        retained = min(lot['quantity'], allocation['quantity'])
                        current_move = allocate_fen(moved, [retained, allocation['quantity'] - retained])[0]
                        if current_move > lot['cost_fen']:
                            raise LedgerError('CONFLICT', '历史成本分配与当前成本不相容')
                        lot['cost_fen'] -= current_move
                        tx.save('position_lots', lot['lot_id'], lot)
                        movement = {'movement_id': _key(case['case_id'], lot['lot_id'], 'COST_TRANSFER'),
                            'lot_id': lot['lot_id'], 'code': lot['code'], 'effective_date': event['ex_date'],
                            'quantity_delta': 0, 'cost_delta_fen': -moved,
                            'kind': 'CORPORATE_COST_TRANSFER', 'reference_id': case['case_id']}
                        tx.append('lot_movements', movement['movement_id'], movement)
                        if moved != current_move:
                            sales = []
                            remaining_sold = allocation['quantity'] - retained
                            for fill in sorted(tx.list('fills', side='SELL'),
                                               key=lambda f: (f['effective_date'], f['order_id'])):
                                if fill['effective_date'] < event['ex_date']:
                                    continue
                                for sold in fill.get('lot_allocations', []):
                                    if sold['lot_id'] == lot['lot_id'] and remaining_sold:
                                        quantity_sold = min(remaining_sold, sold['quantity'])
                                        sales.append((fill, quantity_sold))
                                        remaining_sold -= quantity_sold
                            if remaining_sold:
                                raise LedgerError('CONFLICT', '迟到送转缺少已售批次成本证据')
                            corrections = allocate_fen(moved - current_move, [q for _, q in sales])
                            for (fill, _), correction in zip(sales, corrections, strict=True):
                                correction_id = _key(case['case_id'], lot['lot_id'], fill['order_id'],
                                                     'SOLD_COST_ADJUSTMENT')
                                tx.append('lot_movements', correction_id, {
                                    'movement_id': correction_id, 'lot_id': lot['lot_id'],
                                    'code': case['code'], 'effective_date': fill['effective_date'],
                                    'quantity_delta': 0, 'cost_delta_fen': correction,
                                    'kind': 'LATE_SOLD_COST_ADJUSTMENT', 'reference_id': fill['order_id']})
                                tx.append('corrections', correction_id, {'correction_id': correction_id,
                                    'case_id': case['case_id'], 'code': case['code'],
                                    'reference_fill_id': fill['order_id'], 'lot_id': lot['lot_id'],
                                    'effective_date': fill['effective_date'],
                                    'realized_cost_adjustment_fen': correction, 'cash_adjustment_fen': 0,
                                    'reason': 'LATE_SHARE_COST_ALLOCATION', 'evidence': deepcopy(payload)})
                            self._mark_restatement(tx, event['ex_date'])
                        allocation.update(quantity=str(quantity), cost_fen=moved,
                                          original_quantity=allocation['quantity'])
                        total_quantity += quantity
                        total_cost += moved
                    entitlement.update(kind='SHARES', quantity=str(total_quantity), delivered_quantity=0,
                                       cost_fen=total_cost, remaining_cost_fen=total_cost)
                    if total_quantity != total_quantity.to_integral_value():
                        entitlement['status'] = 'WAITING_ALLOCATION'
                identity = _key(case['case_id'], entitlement['kind'])
                tx.append('entitlements', identity, {'entitlement_id': identity, **entitlement})
                self._mark_restatement(tx, event['ex_date'])
                case['actions'][operation] = {'state': 'APPLIED', 'operation_date': event['ex_date']}
                case['event'] = event
                tx.save('corporate_action_cases', case['case_id'], case)
                results.append({'state': 'APPLIED', 'operation': operation, 'entitlement_id': identity})
            self._refresh_dividend_tax(tx, payload.get('code'), day)
            return {'state': 'APPLIED' if all(r['state'] in {'APPLIED', 'ALREADY_APPLIED', 'NOT_APPLICABLE'}
                                             for r in results) else 'WAITING_DATA', 'actions': results}
        return self._mutate(account_id, operation_id, payload, expected_revision, day, apply)

    def settle_entitlements(self, account_id, payload, *, operation_id, expected_revision):
        day = self._corporate_day(payload)
        operations = [payload['operation']] if payload.get('operation') else ['cash_payment', 'share_delivery']
        if set(operations) - {'cash_payment', 'share_delivery'}:
            raise LedgerError('INVALID_INPUT', '交付权益动作无效')

        def apply(tx):
            results = []
            for operation in operations:
                case, event, outcome = self._case_action(tx, payload, operation)
                if outcome:
                    results.append(outcome)
                    continue
                kind = 'CASH' if operation == 'cash_payment' else 'SHARES'
                identity = _key(case['case_id'], kind)
                entitlement = tx.get('entitlements', identity)
                if not entitlement:
                    results.append({'state': 'WAITING_DATA', 'reason': 'ACCRUAL_REQUIRED', 'operation': operation})
                    continue
                if kind == 'CASH':
                    calendar = self.market.calendar(start=event['payment_date'], end=event['payment_date'])
                    if not calendar.ready or not calendar.data:
                        results.append({'state': 'WAITING_DATA', 'reason': 'PAYMENT_CALENDAR_UNKNOWN'})
                        continue
                    paid_date = event['payment_date']
                    if str(calendar.data[0].get('is_trading_day')) not in {'1', 'True'}:
                        next_day = self.market.calendar_operation('next_session', paid_date)
                        if not next_day.ready:
                            results.append({'state': 'WAITING_DATA', 'reason': 'PAYMENT_CALENDAR_UNKNOWN'})
                            continue
                        paid_date = next_day.data
                    if paid_date > day:
                        results.append({'state': 'NOT_DUE', 'operation_date': paid_date})
                        continue
                    if entitlement['amount_fen']:
                        tx.cash(_key(identity, 'PAYMENT'), 'DIVIDEND', entitlement['amount_fen'],
                                paid_date, case['case_id'], {'event_ids': case['event_ids']})
                    entitlement.update(paid_fen=entitlement['amount_fen'], status='PAID',
                                       payment_date=event['payment_date'], paid_date=paid_date,
                                       payments=[{'effective_date': paid_date,
                                                  'amount_fen': entitlement['amount_fen']}])
                else:
                    quantity = decimal(entitlement['quantity'])
                    if quantity != quantity.to_integral_value():
                        results.append({'state': 'WAITING_DATA', 'reason': 'WAITING_ALLOCATION'})
                        continue
                    day_result = self.market.calendar(start=event['share_listing_date'], end=event['share_listing_date'])
                    if not day_result.ready or not day_result.data or str(day_result.data[0].get('is_trading_day')) not in {'1', 'True'}:
                        results.append({'state': 'WAITING_DATA', 'reason': 'SHARE_LISTING_CALENDAR_UNKNOWN'})
                        continue
                    allocations = entitlement['allocations']
                    quantities = [int(decimal(a['quantity'])) for a in allocations]
                    remainder = int(quantity) - sum(quantities)
                    for i in sorted(range(len(allocations)), key=lambda i: (
                            -(decimal(allocations[i]['quantity']) - quantities[i]), allocations[i]['lot_id']))[:remainder]:
                        quantities[i] += 1
                    # Costs of internal fractional lot allocation follow delivered shares exactly.
                    costs = allocate_fen(entitlement['cost_fen'], quantities)
                    for allocation, shares, cost in zip(allocations, quantities, costs, strict=True):
                        if not shares:
                            continue
                        lot_id = _key(identity, allocation['lot_id'])
                        lot = {'lot_id': lot_id, 'code': case['code'], 'quantity': shares, 'cost_fen': cost,
                            'original_quantity': shares, 'original_cost_fen': cost,
                            'acquired_date': event['share_listing_date'], 'sellable_date': event['share_listing_date'],
                            'tax_start_date': None, 'tax_start_status': 'RULE_PENDING',
                            'source_case_id': case['case_id'], 'source_order_id': None,
                            'source_lot_id': allocation['lot_id']}
                        tx.append('position_lots', lot_id, lot)
                        tx.append('lot_movements', _key(lot_id, 'DELIVERY'), {
                            'movement_id': _key(lot_id, 'DELIVERY'), 'lot_id': lot_id, 'code': case['code'],
                            'effective_date': event['share_listing_date'], 'quantity_delta': shares,
                            'cost_delta_fen': cost, 'kind': 'SHARE_DELIVERY', 'reference_id': case['case_id']})
                    entitlement.update(delivered_quantity=int(quantity), remaining_cost_fen=0,
                                       status='DELIVERED', listing_date=event['share_listing_date'],
                                       delivery_date=event['share_listing_date'])
                self._mark_restatement(tx, entitlement.get('paid_date') or entitlement.get('delivery_date'))
                tx.save('entitlements', identity, entitlement)
                case['event'] = event
                case['actions'][operation] = {'state': 'APPLIED', 'operation_date': event['operation_date']}
                tx.save('corporate_action_cases', case['case_id'], case)
                results.append({'state': 'APPLIED', 'operation': operation})
            self._refresh_dividend_tax(tx, payload.get('code'), day)
            return {'state': 'APPLIED' if all(r['state'] in {'APPLIED', 'ALREADY_APPLIED', 'NOT_APPLICABLE'}
                                             for r in results) else 'WAITING_DATA', 'actions': results}
        return self._mutate(account_id, operation_id, payload, expected_revision, day, apply)

    def _on_sale(self, tx, fill):
        self._refresh_dividend_tax(tx, fill['code'], fill['effective_date'], fill)

    def _refresh_dividend_tax(self, tx, code, day, current_fill=None):
        fills = tx.list('fills', side='SELL')
        if current_fill and all(f['order_id'] != current_fill['order_id'] for f in fills):
            fills.append(current_fill)
        for case in tx.list('corporate_action_cases'):
            if case.get('status') == 'MERGED':
                continue
            if code and case['code'] != code:
                continue
            if not case.get('eligibility') or not any(e.get('case_id') == case['case_id'] for e in tx.list('entitlements')):
                continue
            basis, reason = dividend_basis(case['event'])
            groups, unknown = {}, []
            unsold_quantity = 0
            for eligible in case['eligibility']:
                remaining = eligible['quantity']
                for fill in sorted(fills, key=lambda f: (f['effective_date'], f['order_id'])):
                    if fill['code'] != case['code'] or fill['effective_date'] <= case['record_date']:
                        continue
                    for allocation in fill.get('lot_allocations', []):
                        if allocation['lot_id'] != eligible['lot_id']:
                            continue
                        sold = min(remaining, allocation['quantity'])
                        remaining -= sold
                        if not sold:
                            continue
                        start = eligible.get('tax_start_date')
                        if basis is None or not start or eligible.get('tax_start_status') != 'KNOWN':
                            unknown.append({'quantity': sold, 'order_id': fill['order_id'],
                                            'reason': reason or 'RULE_PENDING',
                                            'effective_date': max(fill['effective_date'], case['event']['ex_date'])})
                            continue
                        rate = format(dividend_tax_rate(start, fill['effective_date']), 'f')
                        group = groups.setdefault(rate, {'basis_yuan': Decimal(0), 'allocations': [], 'trigger_date': ''})
                        group['basis_yuan'] += basis * sold
                        group['trigger_date'] = max(group['trigger_date'], fill['effective_date'])
                        group['allocations'].append({'lot_id': eligible['lot_id'], 'quantity': sold,
                            'order_id': fill['order_id'], 'effective_date': fill['effective_date'],
                            'basis_yuan': str(basis * sold)})
                unsold_quantity += remaining
            potential_id = _key(case['case_id'], 'POTENTIAL_TAX')
            tx.save('tax_items', potential_id, {'tax_id': potential_id, 'case_id': case['case_id'],
                'code': case['code'], 'status': 'NOT_TRIGGERED' if unsold_quantity else 'RESOLVED',
                'assessed_fen': 0, 'paid_fen': 0, 'due_date': None,
                'potential_quantity': unsold_quantity, 'rate_range': ['0', '0.20'],
                'basis_per_share': str(basis) if basis is not None else None,
                'reason': '未出售部分税档尚未确定，未从净资产扣除' if unsold_quantity else None})
            unknown_id = _key(case['case_id'], 'TAX_UNKNOWN')
            if unknown:
                unknown_day = min(a['effective_date'] for a in unknown)
                previous_unknown = tx.get('tax_items', unknown_id)
                if not previous_unknown or previous_unknown.get('allocations') != unknown:
                    self._mark_restatement(tx, unknown_day)
                tx.save('tax_items', unknown_id, {'tax_id': unknown_id, 'case_id': case['case_id'],
                    'code': case['code'], 'assessed_fen': None, 'paid_fen': 0, 'status': 'WAITING_DATA',
                    'due_date': None, 'effective_date': unknown_day,
                    'reason': 'TAX_BASIS_OR_HOLDING_PERIOD_UNKNOWN', 'allocations': unknown})
            elif tx.get('tax_items', unknown_id):
                old = tx.get('tax_items', unknown_id)
                if old.get('status') != 'RESOLVED':
                    self._mark_restatement(tx, old.get('effective_date', day))
                old.update(status='RESOLVED', assessed_fen=0, reason=None)
                tx.save('tax_items', unknown_id, old)
            for rate, group in groups.items():
                identity = _key(case['case_id'], 'TAX', rate)
                prior = tx.get('tax_items', identity) or {'paid_fen': 0, 'assessed_fen': 0}
                assessed = money_fen(group['basis_yuan'] * decimal(rate))
                if assessed < prior['assessed_fen']:
                    prior.update(status='WAITING_DATA', reason='TAX_REDUCTION_REQUIRES_CORRECTION')
                    tx.save('tax_items', identity, prior)
                    continue
                required = []
                if decimal(case['event'].get('cash_per_share') or '0'):
                    required.append(tx.get('entitlements', _key(case['case_id'], 'CASH')))
                if decimal(case['event'].get('bonus_per_share') or '0') + decimal(case['event'].get('capital_per_share') or '0'):
                    required.append(tx.get('entitlements', _key(case['case_id'], 'SHARES')))
                distributed = bool(required) and all(e and e['status'] in {'PAID', 'DELIVERED'} for e in required)
                cumulative, assessed_previous, assessments = Decimal(0), 0, []
                for allocation in sorted(group['allocations'], key=lambda a: (a['effective_date'], a['order_id'], a['lot_id'])):
                    cumulative += decimal(allocation['basis_yuan'])
                    assessed_now = money_fen(cumulative * decimal(rate))
                    due = None
                    if distributed:
                        anchor = max(allocation['effective_date'], *(e.get('paid_date') or e.get('delivery_date') for e in required))
                        following = self.market.calendar_operation('next_session', anchor)
                        if following.ready:
                            due = following.data
                    assessments.append({'effective_date': max(allocation['effective_date'], case['event']['ex_date']),
                        'amount_fen': assessed_now - assessed_previous, 'due_date': due,
                        'order_id': allocation['order_id'], 'lot_id': allocation['lot_id']})
                    assessed_previous = assessed_now
                paid_remaining = prior['paid_fen']
                unpaid_due = []
                for assessment in assessments:
                    used = min(paid_remaining, assessment['amount_fen'])
                    paid_remaining -= used
                    if assessment['amount_fen'] > used and assessment['due_date']:
                        unpaid_due.append(assessment['due_date'])
                due = min(unpaid_due) if unpaid_due else None
                if assessed != prior['assessed_fen'] and assessments:
                    self._mark_restatement(tx, min(a['effective_date'] for a in assessments))
                tx.save('tax_items', identity, {'tax_id': identity, 'case_id': case['case_id'],
                    'code': case['code'], 'assessed_fen': assessed, 'paid_fen': prior['paid_fen'],
                    'status': 'PAID' if assessed == prior['paid_fen'] else 'PAYABLE', 'due_date': due,
                    'trigger_date': group['trigger_date'], 'rate': rate,
                    'basis_yuan': str(group['basis_yuan']), 'allocations': group['allocations'],
                    'assessments': assessments, 'payments': prior.get('payments', []),
                    'reason': None if due or not assessed else 'DISTRIBUTION_OR_CALENDAR_PENDING'})

    def record_tax(self, account_id, payload, *, operation_id, expected_revision):
        day = self._corporate_day(payload)
        payment_id = payload.get('payment_id') or operation_id

        def apply(tx):
            self._refresh_dividend_tax(tx, None, day)
            active_buy = sum(r.get('cash_fen', 0) for r in tx.list('reservations')
                             if r.get('status') == 'ACTIVE' and r.get('side') == 'BUY')
            results = []
            for item in sorted(tx.list('tax_items'), key=lambda t: (t.get('due_date') or '9999', t['case_id'], t['tax_id'])):
                if item.get('assessed_fen') is None or item.get('status') == 'WAITING_DATA':
                    results.append({'tax_id': item['tax_id'], 'state': 'WAITING_DATA'})
                    continue
                remainder = item['assessed_fen'] - item['paid_fen']
                if not remainder:
                    continue
                if not item.get('due_date') or item['due_date'] > day:
                    results.append({'tax_id': item['tax_id'], 'state': 'NOT_DUE'})
                    continue
                entry_id = _key('tax-payment', payment_id, item['tax_id'])
                # An explicit payment id never acquires a second economic meaning.
                if any(c.get('entry_id') == entry_id for c in tx.list('cash_entries')):
                    results.append({'tax_id': item['tax_id'], 'state': 'ALREADY_APPLIED'})
                    continue
                matured = sum(a['amount_fen'] for a in item.get('assessments', [])
                              if a.get('due_date') and a['due_date'] <= day)
                due_remaining = max(0, matured - item['paid_fen'])
                paid = min(due_remaining, max(0, tx.balance() - active_buy))
                if paid:
                    tx.cash(entry_id, 'DIVIDEND_TAX', -paid, day, item['tax_id'], {'payment_id': payment_id})
                    self._mark_restatement(tx, day)
                    item['paid_fen'] += paid
                    item.setdefault('payments', []).append({'effective_date': day,
                        'amount_fen': paid, 'payment_id': payment_id})
                    item['status'] = 'PAID' if item['paid_fen'] == item['assessed_fen'] else 'PAYABLE'
                    tx.save('tax_items', item['tax_id'], item)
                results.append({'tax_id': item['tax_id'], 'state': 'APPLIED' if paid == due_remaining else 'WAITING_DATA',
                                'paid_fen': paid, 'remaining_fen': remainder - paid})
            return {'state': 'APPLIED' if all(r['state'] in {'APPLIED', 'ALREADY_APPLIED'} for r in results)
                    else 'WAITING_DATA', 'payments': results}
        return self._mutate(account_id, operation_id, payload, expected_revision, day, apply)
