"""Detect changed public facts behind published statements without rewriting trades."""

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal, InvalidOperation
from threading import get_ident

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
_REVIEW_SCOPE = ContextVar('statement_input_review_scope', default=None)
_REVIEW_MAX_ENTRIES = 4096
_REVIEW_MAX_BYTES = 16 * 1024 * 1024


def _review_get(scope, key):
    if scope is None or scope['disabled']:
        return None
    try:
        value = scope['values'].get(key)
        if value is not None:
            scope['metrics']['hits'] += 1
        return value
    except Exception:
        # Derived acceleration is disposable; its failure cannot become a
        # financial error or a successful validation result.
        scope['disabled'] = True
        scope['metrics']['disabled'] = True
        scope['values'] = {}
        return None


def _review_put(scope, key, value, size):
    if scope is None or scope['disabled']:
        return
    try:
        if (key not in scope['values'] and len(scope['values']) < _REVIEW_MAX_ENTRIES
                and scope['bytes'] + size <= _REVIEW_MAX_BYTES):
            scope['values'][key] = value
            scope['bytes'] += size
            scope['metrics'].update(entries=len(scope['values']), bytes=scope['bytes'])
    except Exception:
        scope['disabled'] = True
        scope['metrics']['disabled'] = True
        scope['values'] = {}


def _encoded_hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _statement_queries(statement, session, scope, owner, validator):
    raw = statement.get('data_evidence')
    key = None
    if isinstance(raw, str):
        if scope is not None:
            key = ('statement', owner, statement['id'], session, statement['statement_revision'],
                   statement['knowledge_cutoff'], _encoded_hash(raw), validator)
        compiled = _review_get(scope, key)
        if compiled is not None:
            return compiled
        try:
            queries = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise LedgerError('INTEGRITY_ERROR', '已发布账单输入证据损坏', session=session) from exc
    else:
        queries = raw
    if not isinstance(queries, list) or not queries:
        raise LedgerError('INTEGRITY_ERROR', '已发布账单缺少公开输入证据', session=session)
    compiled = []
    for item in queries:
        if not isinstance(item, dict):
            raise LedgerError('INTEGRITY_ERROR', '账单输入查询格式无效', session=session)
        method, kwargs = item.get('method'), item.get('kwargs')
        if (method not in QUERY_FIELDS or not isinstance(kwargs, dict)
                or not set(kwargs) <= QUERY_FIELDS[method] or 'knowledge_cutoff' not in kwargs):
            raise LedgerError('INTEGRITY_ERROR', '账单含不支持的输入查询', session=session)
        if aware_time(kwargs['knowledge_cutoff']) != aware_time(statement['knowledge_cutoff']):
            raise LedgerError('INTEGRITY_ERROR', '账单查询与知识截止不一致', session=session)
        before = canonical(_projection(method, item.get('result'), session))
        compiled.append((method, canonical(kwargs), before))
    compiled = tuple(compiled)
    if key is not None:
        size = sum(len(part.encode('utf-8')) for item in compiled for part in item) + len(str(key))
        _review_put(scope, key, compiled, size)
    return compiled


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
    @contextmanager
    def statement_input_review_scope(self):
        """Disposable immutable projections for one workflow call, never due/finance results.

        All market fingerprints, current statement bytes, transaction state and
        cutoffs remain fresh. Nested calls get their own scope; no cache escapes
        this ledger instance, market adapter, account, or context lifetime.
        """
        scope = {'owner': self, 'market': self.market, 'values': {}, 'bytes': 0,
                 'thread': get_ident(), 'closed': False, 'disabled': False,
                 'metrics': {'hits': 0, 'entries': 0, 'bytes': 0, 'disabled': False}}
        token = _REVIEW_SCOPE.set(scope)
        try:
            yield scope['metrics']
        finally:
            _REVIEW_SCOPE.reset(token)
            scope['closed'] = True
            scope['values'].clear()

    def _statement_input_changes(self, tx, day, cutoff):
        scope = _REVIEW_SCOPE.get()
        if scope is not None and (scope['owner'] is not self or scope['market'] is not self.market
                or scope['closed'] or scope['thread'] != get_ident()):
            scope = None
        validator = verification_identity()
        owner = tx.account_id
        statements = {}
        for row in tx.select('daily_nav', fields=('id', 'session', 'statement_revision',
                'knowledge_cutoff', 'data_evidence'), ranges={'session': {'lte': day}},
                decode_json=False):
            if row['statement_revision'] > statements.get(row['session'], {}).get('statement_revision', 0):
                statements[row['session']] = row
        cache, changes = {}, []
        for session, statement in sorted(statements.items()):
            if session > day:
                continue
            if cutoff < aware_time(statement['knowledge_cutoff']):
                raise LedgerError('INVALID_INPUT', '复查截止不能早于已发布账单知识时点')
            queries = _statement_queries(statement, session, scope, owner, validator)
            for method, encoded_kwargs, before in queries:
                fresh_kwargs = {**json.loads(encoded_kwargs), 'knowledge_cutoff': cutoff.isoformat()}
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
                        if dependency and dependency.get('state') == 'READY':
                            # Coverage checks raw leaf times; event batches can bind
                            # future members filtered by cutoff. Their unchanged file
                            # fingerprints alone do not prove identical query results.
                            query = (fresh_kwargs if method in {'events', 'event_coverage'} else
                                {k: v for k, v in fresh_kwargs.items() if k != 'knowledge_cutoff'})
                            receipt_id = digest({'kind': 'statement_query',
                                'validator': validator, 'method': method, 'query': query,
                                'dependency': dependency['fingerprint']})
                        cache[key] = {'receipt_id': receipt_id, 'dependency': dependency,
                                      'result': None}
                    except Exception as exc:
                        raise LedgerError('INTEGRITY_ERROR', '账单输入公开查询不可用',
                                          method=method, session=session) from exc
                query = cache[key]
                after_key = ('projection', owner, query['receipt_id'], session)
                after = (_review_get(scope, after_key) if query['receipt_id'] else None)
                if after is None:
                    current = self._statement_query_result(query, method, fresh_kwargs, session)
                    after = canonical(_projection(method, current, session))
                    if query['receipt_id']:
                        _review_put(scope, after_key, after, len(after.encode('utf-8')) + len(str(after_key)))
                if before != after:
                    current = self._statement_query_result(query, method, fresh_kwargs, session)
                    changes.append({'session': session,
                        'statement_revision': statement['statement_revision'], 'method': method,
                        'before_hash': _encoded_hash(before), 'after_hash': _encoded_hash(after),
                        'data_evidence': {'method': method, 'kwargs': fresh_kwargs, 'result': current}})
        affected = min((row['session'] for row in changes), default=None)
        return {'state': 'CHANGED' if changes else 'READY', 'session': day,
                'knowledge_cutoff': cutoff.isoformat(), 'affected_from': affected,
                'affected_through': max(statements) if affected else None,
                'changes': changes, 'read_only': True}

    def _statement_query_result(self, query, method, kwargs, session):
        if query['result'] is None:
            try:
                receipt_id = query['receipt_id']
                # Time-sensitive proofs are useful only inside this call's
                # bounded scope. Do not read old cross-cutoff receipts or write
                # one disposable file for every moving clock instant.
                persistent = method not in {'events', 'event_coverage'}
                proof = _receipt(self.root, receipt_id) if receipt_id and persistent else None
                if proof and proof.get('kind') == 'statement_query' and isinstance(proof.get('result'), dict):
                    current = proof['result']
                    _metric('cache_hits')
                else:
                    current = getattr(self.market, method)(**kwargs).as_dict()
                    _metric('full_checks')
                    if receipt_id and persistent and current.get('state') != 'CORRUPT':
                        _save_receipt(self.root, receipt_id, kind='statement_query',
                            dependency_fingerprint=query['dependency']['fingerprint'], result=current)
            except Exception as exc:
                raise LedgerError('INTEGRITY_ERROR', '账单输入公开查询不可用',
                                  method=method, session=session) from exc
            if current.get('state') == 'CORRUPT':
                raise LedgerError('INTEGRITY_ERROR', '账单输入原件损坏', method=method, session=session)
            query['result'] = current
        return query['result']

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
