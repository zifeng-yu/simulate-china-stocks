"""Workflow data scope and bounded collection through the public market APIs.

Inspection is read-only. Collection and month-end preparation are explicit writes,
including a cache-only collection run. This adapter never writes account balances,
repairs raw files, changes source history, or calls a model.
"""

import math
from dataclasses import asdict
from datetime import datetime, time, timedelta
from time import monotonic
from zoneinfo import ZoneInfo

from china_stock_sim.ledger import LedgerService
from china_stock_sim.market.contracts import (
    DataRequest,
    PendingEventRange,
    aware_time,
    session_date,
)
from china_stock_sim.market.pools import prepare_pool
from china_stock_sim.market.preparation import validate_as_of
from china_stock_sim.market.service import MarketDataService
from china_stock_sim.market.settings import load_market_settings

SHANGHAI = ZoneInfo('Asia/Shanghai')


def _retry_at(value, now):
    """Report the next useful wake-up, retaining per-request deadlines unchanged."""
    deadlines = []

    def walk(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if key in {'next_retry_at', 'server_retry_at'} and child:
                    try:
                        stamp = aware_time(child)
                        if stamp > now:
                            deadlines.append(stamp)
                    except (TypeError, ValueError):
                        pass
                elif isinstance(child, (dict, list, tuple)):
                    walk(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                walk(child)

    walk(value)
    return min(deadlines).isoformat() if deadlines else None


def _receipt(state, *, completion=False, changed=False, reasons=(), requests=0, **details):
    return {'state': state, 'completion': bool(completion), 'changed': bool(changed),
            'reasons': list(reasons), 'requests_this_run': requests,
            'next_retry_at': None, **details}


class WorkflowData:
    def __init__(self, settings, market=None, ledger=None, clock=None):
        self.settings, self.root = settings, settings.root
        self.market = market if market is not None else MarketDataService(self.root)
        self.ledger = ledger if ledger is not None else LedgerService(settings, market=self.market)
        self.clock = clock or (lambda: datetime.now(SHANGHAI))

    def _now(self):
        return aware_time(self.clock()).astimezone(SHANGHAI)

    def _upper(self, as_of):
        requested, now = session_date(as_of), self._now()
        if requested > now.date().isoformat():
            raise ValueError('流程不能请求未来日期')
        latest = now.date() if now.time().replace(tzinfo=None) >= time(18, 10) else (
            now.date() - timedelta(days=1))
        return min(requested, latest.isoformat())

    def _limits(self, max_requests, budget_seconds):
        config = load_market_settings(self.root).config
        maximum = config['max_requests_per_run'] if max_requests is None else max_requests
        budget = config['max_run_seconds'] if budget_seconds is None else budget_seconds
        if type(maximum) is not int or not 1 <= maximum <= 1000:
            raise ValueError('请求预算应为1到1000')
        if type(budget) not in {int, float} or not math.isfinite(budget) or not 0 <= budget <= 7200:
            raise ValueError('剩余时间预算应为0到7200秒')
        return maximum, math.floor(budget)

    def _calendar(self, upper):
        cutoff = self._now().isoformat()
        window = self.market.calendar_operation('window', upper, size=1, knowledge_cutoff=cutoff)
        evidence = {'window': window.as_dict()}
        if not window.ready or not window.data:
            return _receipt('WAITING_CALENDAR', reasons=[{'kind': 'COMPLETED_SESSION_UNKNOWN'}],
                            calendar_as_of=upper, calendar_results=evidence)
        day = window.data[-1]
        following = self.market.calendar_operation('next_session', day, knowledge_cutoff=cutoff)
        month = self.market.calendar_operation('is_month_end', day, knowledge_cutoff=cutoff)
        evidence.update(next_session=following.as_dict(), is_month_end=month.as_dict())
        ready = following.ready and month.ready
        return _receipt('READY' if ready else 'WAITING_CALENDAR', completion=ready,
            reasons=[] if ready else [{'kind': 'NEXT_SESSION_UNKNOWN'}],
            calendar_as_of=upper, completed_session=day, day=day,
            next_session=following.data if following.ready else None,
            is_month_end=month.data if month.ready else None, calendar_results=evidence)

    def ensure_calendar(self, as_of, *, download=False, max_requests=None, budget_seconds=None):
        """Resolve D/N from published flags; optional bootstrap also maintains 3 indices."""
        upper = self._upper(as_of)
        result = self._calendar(upper)
        if result['completion'] or not download:
            return result
        maximum, budget = self._limits(max_requests, budget_seconds)
        if budget < 5:
            return {**result, 'state': 'BUDGET_PAUSED',
                    'reasons': [{'kind': 'COLLECTION_BUDGET_EXHAUSTED'}]}
        request = DataRequest(upper, include_events=False, include_indices=True)
        collected = self.market.update(request, download=True, refresh=False, repair=False,
            max_requests=maximum, budget_seconds=budget, clock=self.clock)
        result = self._calendar(upper)
        result.update(changed=True, collection=collected, request=asdict(request),
            requests_this_run=collected.get('requests_this_run', 0),
            next_retry_at=_retry_at(collected, self._now()))
        return result

    def build_request(self, day, accounts, *, target_session=None, candidates=None,
                      history_window=None, include_indices=True):
        """Union business requirements without crossing into ledger storage internals."""
        day = validate_as_of(session_date(day), self._now()).isoformat()
        target = session_date(target_session) if target_session else None
        if target is None:
            following = self.market.calendar_operation('next_session', day,
                                                       knowledge_cutoff=self._now().isoformat())
            target = following.data if following.ready else None
        if target and target < day:
            raise ValueError('政策目标日不能早于行情截止日')
        if history_window is not None and (type(history_window) is not int
                                           or not 1 <= history_window <= 500):
            raise ValueError('历史窗口须为1到500')
        if isinstance(accounts, (str, bytes)):
            raise ValueError('账户须为ID列表，不能是单一字符串')
        identities = tuple(dict.fromkeys(accounts))
        if any(not isinstance(account, str) or not account for account in identities):
            raise ValueError('账户清单须为非空账户ID')
        holdings, orders, actions, ranges, unknown, details = set(), set(), set(), [], [], []
        account_errors = []
        history = max(121, history_window or 121)
        for account in identities:
            try:
                needed = self.ledger.required_market_inputs(account, day)
                historical = self.ledger.positions(account, day)
                local = DataRequest(day, holdings=tuple(needed['holdings']) + tuple(
                    row['code'] for row in historical if row['total_shares'] > 0),
                    pending_orders=tuple(needed['pending_orders']),
                    pending_actions=tuple(needed['pending_actions']),
                    pending_event_ranges=tuple(PendingEventRange(**row)
                                               for row in needed['pending_event_ranges']))
            except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
                account_errors.append({'kind': 'ACCOUNT_DATA_SCOPE_UNAVAILABLE',
                    'account_id': account, 'reason': str(exc)})
                continue
            holdings.update(local.holdings)
            orders.update(local.pending_orders)
            actions.update(local.pending_actions)
            ranges.extend(local.pending_event_ranges)
            unknown.extend({'account_id': account, **row} for row in needed['unknown_ranges'])
            for policy_day in dict.fromkeys((day, target or day)):
                try:
                    policy = self.ledger.get_account_policy(account, policy_day)['policy']
                    size = policy['parameters'].get('lookback_sessions', 21)
                    if type(size) is not int or not 1 <= size <= 500:
                        raise ValueError('持久账户政策历史窗口无效')
                    history = max(history, size)
                except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
                    account_errors.append({'kind': 'ACCOUNT_POLICY_WINDOW_UNAVAILABLE',
                        'account_id': account, 'target_session': policy_day, 'reason': str(exc)})
            details.append({'account_id': account, 'required_market_inputs': needed,
                            'historical_holdings': [p['code'] for p in historical]})
        pool = None
        if candidates is None:
            pool = self.market.pool(day, knowledge_cutoff=self._now().isoformat())
            candidates = tuple(row['code'] for row in pool.data['selected']) if pool.ready else ()
        request = DataRequest(day, candidates=tuple(candidates), holdings=tuple(sorted(holdings)),
            pending_orders=tuple(sorted(orders)), pending_actions=tuple(sorted(actions)),
            pending_event_ranges=tuple(ranges), history_sessions=history,
            include_events=True, include_indices=include_indices)
        reasons = [*account_errors, *[{'kind': 'UNKNOWN_EVENT_RANGE', **item} for item in unknown]]
        if identities and target is None:
            reasons.append({'kind': 'NEXT_POLICY_SESSION_UNKNOWN'})
        if pool is not None and not pool.ready:
            reasons.append({'kind': 'POOL_UNAVAILABLE', 'state': pool.state,
                            'issues': list(pool.issues)})
        return {'request': request, 'accounts': list(identities), 'account_inputs': details,
                'reasons': reasons, 'pool': pool.as_dict() if pool is not None else None,
                'target_session': target, 'unknown_ranges': unknown}

    def inspect(self, day, accounts=(), *, target_session=None, candidates=None,
                history_window=None, include_indices=True):
        """Inspect current cache only; never call update, prepare, freeze, or a provider."""
        scope = self.build_request(day, accounts, target_session=target_session,
            candidates=candidates, history_window=history_window, include_indices=include_indices)
        request = scope.pop('request')
        cutoff = self._now().isoformat()
        window = self.market.calendar_operation('window', request.as_of,
            size=request.history_sessions, knowledge_cutoff=cutoff)
        rows, reasons = [], list(scope['reasons'])
        if not window.ready:
            reasons.append({'kind': 'HISTORY_CALENDAR_UNAVAILABLE', 'issues': list(window.issues)})
        for code in request.ordered_codes:
            row = {'code': code, 'prices': 'NOT_REQUESTED', 'event_coverage': 'NOT_REQUESTED',
                   'pending_event_ranges': []}
            if code in request.price_codes:
                if window.ready:
                    prices = self.market.prices(code, window.data[0], request.as_of,
                                                knowledge_cutoff=cutoff)
                    coverage = self.market.event_coverage(code, window.data[0], request.as_of,
                                                         knowledge_cutoff=cutoff)
                    row.update(prices=prices.state, event_coverage=coverage.state,
                               price_issues=list(prices.issues), event_issues=list(coverage.issues))
                else:
                    row.update(prices='WAITING_DATA', event_coverage='WAITING_DATA')
            for item in request.pending_event_ranges:
                if item.code == code:
                    coverage = self.market.event_coverage(code, item.start, item.end,
                                                         knowledge_cutoff=cutoff)
                    row['pending_event_ranges'].append({**asdict(item), **coverage.as_dict()})
            rows.append(row)
        indices = []
        if include_indices:
            for code in ('SH000001', 'SZ399001', 'SH000300'):
                result = self.market.readiness(code, day, 'index', knowledge_cutoff=cutoff)
                indices.append({'code': code, **result.as_dict()})
        ready = not reasons and all(row['prices'] in {'READY', 'NOT_REQUESTED'}
            and row['event_coverage'] in {'READY', 'NOT_REQUESTED'}
            and all(r['state'] == 'READY' for r in row['pending_event_ranges']) for row in rows)
        ready = ready and all(row['state'] == 'READY' for row in indices)
        return _receipt('READY' if ready else 'WAITING_DATA', completion=ready, reasons=reasons,
            state_scope='cached_public_fact_readiness_not_collection_completion',
            collection_checked=False, request=asdict(request), securities=rows, indices=indices, **{
                key: value for key, value in scope.items() if key != 'reasons'})

    def collect(self, day, accounts=(), *, download=False, target_session=None,
                candidates=None, history_window=None, include_indices=True,
                max_requests=None, budget_seconds=None):
        """Explicit data operation. Cache-only mode still persists a collection job."""
        maximum, budget = self._limits(max_requests, budget_seconds)
        if budget < 5:
            return _receipt('BUDGET_PAUSED', reasons=[{'kind': 'COLLECTION_BUDGET_EXHAUSTED'}])
        scope = self.build_request(day, accounts, target_session=target_session,
            candidates=candidates, history_window=history_window, include_indices=include_indices)
        request = scope.pop('request')
        collection = self.market.update(request, download=download, refresh=False, repair=False,
            max_requests=maximum, budget_seconds=budget, clock=self.clock)
        complete = collection.get('state') == 'COMPLETE' and not scope['reasons']
        state = 'COMPLETE' if complete else ('PARTIAL' if collection.get('state') == 'COMPLETE'
                                           else collection.get('state', 'WAITING_DATA'))
        reasons = [*scope['reasons'], *collection.get('issues', [])]
        return _receipt(state, completion=complete, changed=True, reasons=reasons,
            requests=collection.get('requests_this_run', 0), collection=collection,
            request=asdict(request), next_retry_at=_retry_at(collection, self._now()),
            **{key: value for key, value in scope.items() if key != 'reasons'})

    def refresh_month_end(self, day, *, download=False, accounts=(), target_session=None,
                          history_window=None, max_requests=None, budget_seconds=None):
        """Prepare one frozen monthly successor, then explicitly collect its codes at D."""
        day = validate_as_of(session_date(day), self._now()).isoformat()
        calendar = self._calendar(day)
        if not calendar['completion'] or calendar['completed_session'] != day:
            return _receipt('WAITING_CALENDAR', reasons=[{'kind': 'MONTH_END_CALENDAR_UNKNOWN'}],
                            calendar=calendar)
        target = calendar['next_session']
        if target_session is not None and session_date(target_session) != target:
            raise ValueError('月末后继池目标日必须是紧邻下一交易日')
        current_calendar = self._calendar(self._upper(self._now().date().isoformat()))
        if not current_calendar['completion']:
            return _receipt('WAITING_CALENDAR', reasons=[{'kind': 'CURRENT_POOL_HORIZON_UNKNOWN'}],
                            calendar=current_calendar)
        latest = self.market.pool(max(target, current_calendar['next_session']),
                                  knowledge_cutoff=self._now().isoformat())
        if latest.state == 'CORRUPT':
            return _receipt('NEEDS_DATA_REVIEW', reasons=[{'kind': 'CURRENT_POOL_CORRUPT'}],
                            active_pool=latest.as_dict())
        if latest.ready and latest.data['date'] > day:
            return _receipt('UNCHANGED', completion=True,
                reasons=[{'kind': 'NEWER_POOL_PRESENT', 'selection_date': latest.data['date']}],
                active_pool=latest.as_dict(), target_session=target)
        active = self.market.pool(day, knowledge_cutoff=self._now().isoformat())
        if active.state == 'CORRUPT':
            return _receipt('NEEDS_DATA_REVIEW', reasons=[{'kind': 'CURRENT_POOL_CORRUPT'}],
                            active_pool=active.as_dict())
        if active.ready and not calendar['is_month_end']:
            return _receipt('UNCHANGED', completion=True, active_pool=active.as_dict(),
                            target_session=target)
        maximum, budget = self._limits(max_requests, budget_seconds)
        if budget < 5:
            return _receipt('BUDGET_PAUSED', reasons=[{'kind': 'COLLECTION_BUDGET_EXHAUSTED'}])
        started = monotonic()
        prepared = prepare_pool(self.root, day, download=download, repair=False, force=False,
            clock=self.clock, max_requests=maximum, budget_seconds=budget)
        inner = prepared.get('preparation', {})
        count = inner.get('requests_this_run', 0)
        done = (prepared.get('state') == 'POOL_PREPARED'
                and inner.get('state') == 'PREPARATION_COMPLETE'
                and inner.get('price_ready') is True and inner.get('clues_downloaded') is True)
        if not done:
            return _receipt('PARTIAL', changed=True, requests=count,
                reasons=[{'kind': 'MONTH_END_PREPARATION_INCOMPLETE',
                          'outer_state': prepared.get('state'), 'preparation_state': inner.get('state')}],
                preparation=prepared, target_session=target,
                next_retry_at=_retry_at(prepared, self._now()))
        successor = self.market.pool(target, knowledge_cutoff=self._now().isoformat())
        if not successor.ready or successor.data.get('date') != day:
            return _receipt('WAITING_DATA', changed=True, requests=count,
                reasons=[{'kind': 'SUCCESSOR_POOL_UNAVAILABLE'}], preparation=prepared,
                successor_pool=successor.as_dict(), target_session=target)
        remaining = math.floor(budget - (monotonic() - started))
        if count >= maximum or remaining < 5:
            return _receipt('BUDGET_PAUSED', changed=True, requests=count,
                reasons=[{'kind': 'SUCCESSOR_COLLECTION_BUDGET_PAUSED'}],
                preparation=prepared, successor_pool=successor.as_dict(), target_session=target)
        codes = tuple(row['code'] for row in successor.data['selected'])
        collected = self.collect(day, accounts, download=download, candidates=codes,
            target_session=target, history_window=history_window,
            max_requests=maximum - count, budget_seconds=remaining)
        return {**collected, 'preparation': prepared, 'successor_pool': successor.as_dict(),
                'previous_pool': active.as_dict(), 'target_session': target,
                'pool_changed': successor.observation_ids != latest.observation_ids,
                'requests_this_run': count + collected['requests_this_run']}
