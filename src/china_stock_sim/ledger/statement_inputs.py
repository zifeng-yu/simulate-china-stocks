"""Detect changed public facts behind published statements without rewriting trades."""

from decimal import Decimal, InvalidOperation

from china_stock_sim.ledger.decision_evidence import (
    _metric,
    _receipt,
    _save_receipt,
    verification_identity,
)
from china_stock_sim.ledger.evidence import document
from china_stock_sim.ledger.kernel import LedgerError, canonical, digest
from china_stock_sim.market.contracts import aware_time, session_date

QUERY_FIELDS = {
    'calendar': {'start', 'end', 'knowledge_cutoff'},
    'calendar_operation': {'operation', 'day', 'knowledge_cutoff'},
    'prices': {'code', 'start', 'end', 'knowledge_cutoff'},
    'events': {'code', 'knowledge_cutoff'},
    'event_coverage': {'code', 'start', 'end', 'knowledge_cutoff'},
}
EVENT_FIELDS = {
    'event_id', 'code', 'kind', 'status', 'proposal_date', 'record_date', 'ex_date',
    'payment_date', 'share_listing_date', 'cash_per_share', 'bonus_per_share',
    'capital_per_share', 'conflicts', 'unknown_fields', 'terms_ready', 'tax_facts',
}
NUMBERS = {'close', 'cash_per_share', 'bonus_per_share', 'capital_per_share'}
AUDIT_FIELDS = {'_provenance', 'observation_ids', 'fetched_at', 'available_at',
                'raw_reference', 'source_evidence', 'sources'}


def _semantic(value, field=None):
    if isinstance(value, dict):
        return {key: _semantic(item, key) for key, item in value.items() if key not in AUDIT_FIELDS}
    if isinstance(value, (list, tuple)):
        return [_semantic(item) for item in value]
    if value is not None and field in NUMBERS:
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            raise LedgerError('INTEGRITY_ERROR', '账单输入含无效数值') from None
        if not number.is_finite():
            raise LedgerError('INTEGRITY_ERROR', '账单输入含非有限数值')
        return format(number.normalize(), 'f')
    return value


def _projection(method, result, day):
    if not isinstance(result, dict) or not {'state', 'data', 'issues'} <= result.keys():
        raise LedgerError('INTEGRITY_ERROR', '账单缺少完整查询结果')
    state, data, issues = result['state'], result['data'], result['issues']
    if (state not in {'READY', 'PARTIAL', 'MISSING', 'WAITING_DATA', 'CONFLICT', 'CORRUPT'}
            or not isinstance(issues, (list, tuple))
            or any(not isinstance(row, dict) for row in issues)):
        raise LedgerError('INTEGRITY_ERROR', '账单输入状态或问题格式无效')
    if method == 'event_coverage':
        # A new daily coverage check may cover a wider interval with the same facts.
        return {'state': state, 'issues': _semantic(issues)}
    if method == 'events' and isinstance(data, list):
        relevant = [row for row in data if not row.get('record_date') or row['record_date'] <= day]
        data = [{key: value for key, value in row.items() if key in EVENT_FIELDS} for row in relevant]
        identities = {row.get('event_id') for row in relevant}
        issues = [row for row in issues if not row.get('event_id') or row['event_id'] in identities]
        if state in {'READY', 'CONFLICT'}:
            state = 'CONFLICT' if any(row.get('conflicts') for row in relevant) else 'READY'
    elif method == 'prices' and isinstance(data, list):
        # Statement valuation consumes close/date and trust; a changed open/volume
        # cannot retrospectively change the original execution or require revaluation.
        fields = {'code', 'date', 'close', 'conflicts'}
        data = [{key: value for key, value in row.items() if key in fields} for row in data]
    projected = _semantic(data)
    if isinstance(projected, list):
        projected = sorted(projected, key=canonical)
    return {'state': state, 'data': projected, 'issues': _semantic(issues)}


class StatementInputsMixin:
    def _statement_input_changes(self, tx, day, cutoff):
        statements = {}
        for row in tx.select('daily_nav', fields=('id', 'session', 'statement_revision',
                'knowledge_cutoff', 'data_evidence'), ranges={'session': {'lte': day}}):
            if row['statement_revision'] > statements.get(row['session'], {}).get('statement_revision', 0):
                statements[row['session']] = row
        cache, changes = {}, []
        for session, statement in sorted(statements.items()):
            if session > day:
                continue
            original_cutoff = aware_time(statement['knowledge_cutoff'])
            if cutoff < original_cutoff:
                raise LedgerError('INVALID_INPUT', '复查截止不能早于已发布账单知识时点')
            queries = statement.get('data_evidence')
            if not isinstance(queries, list) or not queries:
                raise LedgerError('INTEGRITY_ERROR', '已发布账单缺少公开输入证据', session=session)
            for item in queries:
                if not isinstance(item, dict):
                    raise LedgerError('INTEGRITY_ERROR', '账单输入查询格式无效', session=session)
                method, kwargs = item.get('method'), item.get('kwargs')
                if (method not in QUERY_FIELDS or not isinstance(kwargs, dict)
                        or not set(kwargs) <= QUERY_FIELDS[method]
                        or 'knowledge_cutoff' not in kwargs):
                    raise LedgerError('INTEGRITY_ERROR', '账单含不支持的输入查询', session=session)
                if aware_time(kwargs['knowledge_cutoff']) != original_cutoff:
                    raise LedgerError('INTEGRITY_ERROR', '账单查询与知识截止不一致', session=session)
                fresh_kwargs = {**kwargs, 'knowledge_cutoff': cutoff.isoformat()}
                key = canonical({'method': method, 'kwargs': fresh_kwargs})
                if key not in cache:
                    try:
                        dependency = None
                        fingerprinter = getattr(self.market, 'statement_input_fingerprint', None)
                        if callable(fingerprinter):
                            dependency = fingerprinter(method, fresh_kwargs)
                            if dependency.get('state') == 'CORRUPT':
                                raise LedgerError('INTEGRITY_ERROR', '账单输入依赖缺失或损坏',
                                                  method=method, session=session)
                        receipt_id = None
                        proof = None
                        if dependency and dependency.get('state') == 'READY':
                            receipt_id = digest({'kind': 'statement_query',
                                'validator': verification_identity(), 'method': method,
                                'query': {k: v for k, v in fresh_kwargs.items() if k != 'knowledge_cutoff'},
                                'dependency': dependency['fingerprint']})
                            proof = _receipt(self.root, receipt_id)
                        if proof and proof.get('kind') == 'statement_query' and isinstance(proof.get('result'), dict):
                            cache[key] = proof['result']
                            _metric('cache_hits')
                        else:
                            cache[key] = getattr(self.market, method)(**fresh_kwargs).as_dict()
                            _metric('full_checks')
                            if receipt_id and cache[key].get('state') != 'CORRUPT':
                                _save_receipt(self.root, receipt_id, kind='statement_query',
                                    dependency_fingerprint=dependency['fingerprint'], result=cache[key])
                    except Exception as exc:
                        raise LedgerError('INTEGRITY_ERROR', '账单输入公开查询不可用',
                                          method=method, session=session) from exc
                current = cache[key]
                if current.get('state') == 'CORRUPT':
                    raise LedgerError('INTEGRITY_ERROR', '账单输入原件损坏', method=method, session=session)
                before = _projection(method, item['result'], session)
                after = _projection(method, current, session)
                if canonical(before) != canonical(after):
                    changes.append({'session': session,
                        'statement_revision': statement['statement_revision'], 'method': method,
                        'before_hash': digest(before), 'after_hash': digest(after),
                        'data_evidence': {'method': method, 'kwargs': fresh_kwargs, 'result': current}})
        affected = min((row['session'] for row in changes), default=None)
        return {'state': 'CHANGED' if changes else 'READY', 'session': day,
                'knowledge_cutoff': cutoff.isoformat(), 'affected_from': affected,
                'affected_through': max(statements) if affected else None,
                'changes': changes, 'read_only': True}

    def statement_input_changes(self, account_id, session, *, knowledge_cutoff=None):
        day = session_date(session)
        now = self._now()
        cutoff = aware_time(knowledge_cutoff) if knowledge_cutoff is not None else now
        if cutoff > now:
            raise LedgerError('INVALID_INPUT', '复查截止不能晚于实际处理时点')
        return self._read(account_id, lambda tx: self._statement_input_changes(tx, day, cutoff))

    def refresh_statement_inputs(self, account_id, payload, *, operation_id, expected_revision):
        payload = dict(payload)
        if set(payload) - {'session', 'knowledge_cutoff'}:
            raise LedgerError('INVALID_INPUT', '输入复查仅接受日期与知识截止，不接受外部变化结论')
        day = session_date(payload['session'])
        now = self._now()
        cutoff = aware_time(payload.get('knowledge_cutoff', now.isoformat()))
        if cutoff > now:
            raise LedgerError('INVALID_INPUT', '复查截止不能晚于实际处理时点')

        def refresh(tx):
            result = self._statement_input_changes(tx, day, cutoff)
            affected, through = result['affected_from'], result['affected_through']
            if affected is None:
                return {'state': 'ALREADY_APPLIED', 'changed': False, 'session': day}
            previous, previous_through = tx.state.get('restate_from'), tx.state.get('restate_through')
            if previous and previous <= affected and previous_through and previous_through >= through:
                return {'state': 'ALREADY_APPLIED', 'changed': True,
                        'restate_from': previous, 'restate_through': previous_through, 'session': day}
            self._mark_restatement(tx, affected)
            # Invalidation also covers a fully published provisional history whose
            # accounting watermark has not advanced to the newly changed session.
            tx.state['restate_from'] = min(tx.state.get('restate_from') or affected, affected)
            tx.state['restate_through'] = max(tx.state.get('restate_through') or through, through)
            proof = document(self.root, 'statement-inputs', {
                'schema_version': 1, 'account_id': tx.account_id,
                'knowledge_cutoff': cutoff.isoformat(), 'affected_from': affected,
                'affected_through': through, 'changes': result['changes']})
            for session in sorted({row['session'] for row in tx.select(
                    'daily_nav', fields=('session',), ranges={'session': {'gte': affected, 'lte': through}})}):
                if affected <= session <= through:
                    identity = f'statement-inputs:{session}:{proof["content_hash"]}'
                    tx.append('job_runs', identity, {'kind': 'RESTATE_SESSION', 'session': session,
                        'status': 'WAITING_DATA', 'reason': 'STATEMENT_INPUT_CHANGED',
                        'knowledge_cutoff': cutoff.isoformat(), 'document': proof})
            return {'state': 'APPLIED', 'changed': True, 'session': day,
                    'restate_from': tx.state['restate_from'],
                    'restate_through': tx.state['restate_through'], 'change_count': len(result['changes'])}

        return self._mutate(account_id, operation_id, payload, expected_revision, day, refresh)
