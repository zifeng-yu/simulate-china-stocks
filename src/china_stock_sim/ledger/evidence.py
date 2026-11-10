"""Public market evidence checks and immutable, local business documents."""

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from china_stock_sim.common import fees as fee_math
from china_stock_sim.config import local_path
from china_stock_sim.ledger.kernel import LedgerError, canonical
from china_stock_sim.market.contracts import aware_time, session_date

SHANGHAI = ZoneInfo('Asia/Shanghai')


def document(root, category, value):
    text = canonical(value)
    if re.search(r'(sk-[A-Za-z0-9_.-]{12,}|Bearer\s+\S+)', text, re.I):
        raise LedgerError('INVALID_INPUT', '业务证据禁止包含模型密钥或授权头')
    if any(re.search(r'"(?:api[_-]?key|authorization|password)"\s*:', text, re.I)
           for _ in [0]):
        raise LedgerError('INVALID_INPUT', '业务证据包含凭据字段')
    encoded = text.encode('utf-8')
    content_hash = hashlib.sha256(encoded).hexdigest()
    del text
    relative = f'data/plans/{category}/{content_hash}.json'
    path = local_path(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded += b'\n'
    if path.exists():
        from china_stock_sim.ledger.document_objects import document_layout, read_document_bytes
        layout = document_layout(root, relative, content_hash)
        if read_document_bytes(root, layout, max_bytes=128 * 1024 * 1024) != encoded:
            raise LedgerError('INTEGRITY_ERROR', '已有证据文件内容损坏')
    else:
        if len(encoded) > 64 * 1024:
            from china_stock_sim.common.evidence_objects import put_blob
            from china_stock_sim.ledger.document_objects import FORMAT
            encoded = (canonical({'storage_format': FORMAT,
                'logical_content_hash': content_hash,
                'object_ref': put_blob(root, encoded)}) + '\n').encode()
        fd, temporary = tempfile.mkstemp(prefix='.ledger-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return {'path': relative, 'content_hash': content_hash}


def _fee_call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except fee_math.ExecutionSupportError as exc:
        raise LedgerError(exc.code, str(exc), **exc.details) from exc


def number(value, *, positive=False):
    return _fee_call(fee_math.number, value, positive=positive)


def integer(value, *, minimum=0):
    return _fee_call(fee_math.integer, value, minimum=minimum)


def money(value):
    return _fee_call(fee_math.money, value)


def fee_breakdown(price, quantity, side, policy, session):
    return _fee_call(fee_math.fee_breakdown, price, quantity, side, policy, session)


class EvidenceMixin:
    def _verify_evidence(self, payload, required=False):
        ids = payload.get('market_snapshot_ids', payload.get('snapshot_ids', []))
        queries = payload.get('data_evidence', [])
        if isinstance(ids, str) or not isinstance(ids, (list, tuple)):
            raise LedgerError('INVALID_INPUT', '快照ID须为列表')
        if not isinstance(queries, (list, tuple)):
            raise LedgerError('INVALID_INPUT', '数据查询证据须为列表')
        if required and not ids and not queries:
            raise LedgerError('WAITING_DATA', '缺少冻结数据或公开查询证据')
        cutoff = payload.get('knowledge_cutoff', payload.get('input_cutoff'))
        if cutoff is not None and aware_time(cutoff) > self._now():
            raise LedgerError('INVALID_INPUT', '证据截止不能晚于实际处理时点')
        snapshots, results = [], []
        try:
            for identity in dict.fromkeys(ids):
                snapshot = self.market.read_snapshot(identity)
                content = snapshot['body']['content']
                available = aware_time(content['knowledge_cutoff'])
                if available > self._now() or (cutoff and available > aware_time(cutoff)):
                    raise LedgerError('INVALID_INPUT', '快照知识时点超出本次输入截止')
                snapshots.append(snapshot)
            allowed = {'calendar', 'calendar_operation', 'prices', 'security', 'pool', 'events',
                       'event_coverage', 'event_settlement', 'readiness', 'index_quote',
                       'signal_prices'}
            for evidence in queries:
                method, kwargs = evidence['method'], evidence['kwargs']
                if method not in allowed or not isinstance(kwargs, dict):
                    raise LedgerError('INVALID_INPUT', '只允许公开只读数据查询证据')
                known = kwargs.get('knowledge_cutoff')
                if not known or aware_time(known) > self._now() or (
                        cutoff and aware_time(known) != aware_time(cutoff)):
                    raise LedgerError('INVALID_INPUT', '公开查询须保存一致的知识截止')
                actual = getattr(self.market, method)(**kwargs).as_dict()
                if canonical(actual) != canonical(evidence['result']):
                    raise LedgerError('INTEGRITY_ERROR', '公开查询证据与原结果不一致')
                if actual['state'] == 'CORRUPT':
                    raise LedgerError('INTEGRITY_ERROR', '数据证据损坏')
                results.append(actual)
        except LedgerError:
            raise
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise LedgerError('INTEGRITY_ERROR', '数据证据无法核验', detail=str(exc)) from exc
        return {'snapshots': snapshots, 'query_results': results, 'queries': list(queries)}

    @staticmethod
    def _has_observations(result, body=None):
        ids = result.get('observation_ids')
        if not isinstance(ids, (list, tuple)) or not ids or any(
                not isinstance(item, str) or not item for item in ids):
            return False
        if body is not None and not set(ids).issubset(body.get('observation_ids', [])):
            raise LedgerError('INTEGRITY_ERROR', '用途证据的观测ID未包含于冻结快照')
        return True

    @staticmethod
    def _price_rows(result, code, as_of, *, exact=False, adjusted=False, status=False):
        rows = result.get('data')
        allowed = {'READY', 'WAITING_DATA', 'PARTIAL'} if status else {'READY'}
        if not isinstance(rows, list) or not rows or result.get('state') not in allowed:
            return False
        matched = False
        for row in rows:
            if not isinstance(row, dict) or row.get('code') != code:
                return False
            try:
                day = session_date(row.get('date'))
                if day > as_of:
                    return False
                price = row.get('adjusted_close') if adjusted else row.get('close')
                if status and str(row.get('tradestatus')) == '0':
                    usable = True
                else:
                    usable = number(price, positive=True) > 0
            except (LedgerError, ValueError, TypeError):
                return False
            matched = matched or (usable and (day == as_of if exact else day <= as_of))
        return matched

    @classmethod
    def _evidence_for(cls, verified, code, as_of, *, exact=False, allow_status=False):
        """Require actual price rows, never unrelated READY event/calendar results."""
        for snapshot in verified['snapshots']:
            body = snapshot['body']
            content = body['content']
            day = content['as_of']
            if not (day == as_of if exact else day <= as_of):
                continue
            for purpose, result in content.get('results', {}).get(code, {}).items():
                if purpose not in {'valuation', 'llm', 'rules'}:
                    continue
                if cls._has_observations(result, body) and cls._price_rows(
                        result, code, as_of, exact=exact, adjusted=purpose in {'llm', 'rules'},
                        status=allow_status and purpose == 'valuation'):
                    return
            if allow_status:
                result = content.get('results', {}).get(code, {}).get('execution', {})
                facts = result.get('data')
                if (result.get('state') in {'READY', 'WAITING_DATA', 'PARTIAL'}
                        and cls._has_observations(result, body) and isinstance(facts, dict)
                        and facts.get('code') == code and facts.get('date') == as_of
                        and str(facts.get('tradestatus')) == '0'):
                    return
        for query in verified['queries']:
            kwargs, method, result = query['kwargs'], query['method'], query['result']
            purpose = kwargs.get('purpose')
            facts = result.get('data')
            if (allow_status and method == 'readiness' and purpose == 'execution'
                    and kwargs.get('code') == code and kwargs.get('as_of') == as_of
                    and result.get('state') in {'READY', 'WAITING_DATA', 'PARTIAL'}
                    and cls._has_observations(result) and isinstance(facts, dict)
                    and facts.get('code') == code and facts.get('date') == as_of
                    and str(facts.get('tradestatus')) == '0'):
                return
            if method != 'prices' and not (
                    method == 'readiness' and purpose in {'valuation', 'llm', 'rules'}):
                continue
            day = kwargs.get('as_of', kwargs.get('end'))
            if (kwargs.get('code') == code and day and (day == as_of if exact else day <= as_of)
                    and cls._has_observations(result) and cls._price_rows(
                        result, code, as_of, exact=exact,
                        adjusted=purpose in {'llm', 'rules'}, status=allow_status)):
                return
        raise LedgerError('WAITING_DATA', '证据未覆盖对应证券及有效行情日期', code=code, date=as_of)

    def _execution_evidence(self, verified, payload, code, target):
        """Bind the execution layer's versioned rules to exact public market facts."""
        context = payload.get('execution_context')
        if not isinstance(context, dict) or not {'rule_id', 'effective_from', 'price_tick'} <= context.keys():
            raise LedgerError('INVALID_INPUT', '确定成交缺少完整execution_context')
        if not isinstance(context['rule_id'], str) or not context['rule_id'].strip():
            raise LedgerError('INVALID_INPUT', '执行价格规则ID无效')
        number(context['price_tick'], positive=True)
        if session_date(context['effective_from']) > target or (
                context.get('effective_to') and session_date(context['effective_to']) < target):
            raise LedgerError('INVALID_INPUT', '执行价格规则不在目标日有效期内')
        if context.get('no_price_limit') is not True and not (
                Decimal(0) < number(context.get('limit_rate'), positive=True) < Decimal(1)):
            raise LedgerError('INVALID_INPUT', '执行价格规则涨跌幅无效')
        cutoff = aware_time(payload['knowledge_cutoff'])
        try:
            actual = self.market.readiness(code, target, 'execution', knowledge_cutoff=cutoff,
                                           rule_context=context).as_dict()
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise LedgerError('INTEGRITY_ERROR', '执行用途证据无法通过公开接口核验') from exc
        facts = actual.get('data')
        if (actual.get('state') != 'READY' or not isinstance(facts, dict) or not facts
                or not self._has_observations(actual) or facts.get('code') != code
                or facts.get('date') != target or facts.get('rule_id') != context['rule_id']
                or aware_time(facts.get('knowledge_cutoff')) != cutoff):
            raise LedgerError('WAITING_DATA', '目标日执行资格事实未齐备', code=code, date=target)
        for snapshot in verified['snapshots']:
            body, content = snapshot['body'], snapshot['body']['content']
            result = content.get('results', {}).get(code, {}).get('execution', {})
            if (content['as_of'] == target and aware_time(content['knowledge_cutoff']) == cutoff
                    and self._has_observations(result, body) and canonical(result) == canonical(actual)):
                return
        for query in verified['queries']:
            kwargs = query['kwargs']
            if (query['method'] == 'readiness' and kwargs.get('purpose') == 'execution'
                    and kwargs.get('code') == code and kwargs.get('as_of') == target
                    and aware_time(kwargs['knowledge_cutoff']) == cutoff
                    and canonical(kwargs.get('rule_context')) == canonical(context)
                    and canonical(query['result']) == canonical(actual)):
                return
        raise LedgerError('WAITING_DATA', '冻结证据未绑定同一执行规则、截止时点和执行观测',
                          code=code, date=target)

    def _session(self, day, *, cutoff=None):
        day = session_date(day)
        result = self.market.calendar(start=day, end=day,
                                      knowledge_cutoff=cutoff or self._now().isoformat())
        if not result.ready or not result.data:
            raise LedgerError('WAITING_DATA', '交易日历缺失', date=day)
        if str(result.data[0]['is_trading_day']) != '1':
            raise LedgerError('INVALID_INPUT', '目标日期不是交易日', date=day)
        return day

    def _adjacent(self, operation, day, *, cutoff=None):
        result = self.market.calendar_operation(operation, day,
                    knowledge_cutoff=cutoff or self._now().isoformat())
        if not result.ready:
            raise LedgerError('WAITING_DATA', '相邻交易日未知', date=day)
        return result.data

    def _before_cutoff(self, target):
        deadline = datetime.combine(datetime.fromisoformat(target).date(), time(9), SHANGHAI)
        if self._now() >= deadline:
            raise LedgerError('INVALID_INPUT', '已过目标日09:00封存截止', target_session=target)

    @staticmethod
    def _payload(value):
        return json.loads(canonical(value))
