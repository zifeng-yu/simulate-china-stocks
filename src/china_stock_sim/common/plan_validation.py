"""Pure pre-open validation. No strategy selection, collection or ledger writes."""

import re
from copy import deepcopy
from dataclasses import asdict, fields, is_dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from china_stock_sim.common.decisions import normalize_decision_ref, validate_decision_core
from china_stock_sim.common.fees import (
    ExecutionSupportError,
    aware_time,
    fee_breakdown,
    integer,
    number,
    session_date,
)
from china_stock_sim.common.policies import (
    MAX_ORDER_SHARES,
    daily_symbol_limit,
    policy_digest,
    validate_policy_bundle,
)
from china_stock_sim.common.sizing import intent_quantity
from china_stock_sim.common.wire import reject_credentials, to_wire
from china_stock_sim.domain import FrozenOrder, PlanEnvelope, StrategyProposal

SHANGHAI = ZoneInfo("Asia/Shanghai")
_ACCOUNT_VOLATILE = {"snapshot_at", "report_revision", "last_rendered_revision"}
_ALLOWED_QUERIES = {
    "calendar",
    "calendar_operation",
    "prices",
    "security",
    "pool",
    "events",
    "event_coverage",
    "readiness",
}


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ExecutionSupportError("INVALID_INPUT", f"{label}须为非空短字符串")
    return value


def _code(value):
    if not isinstance(value, str):
        raise ExecutionSupportError("INVALID_INPUT", "证券代码须含明确市场")
    match = re.fullmatch(r"(SH|SZ|BJ)\.?(\d{6})", value.strip().upper())
    if not match:
        raise ExecutionSupportError("INVALID_INPUT", "证券代码无效")
    return "".join(match.groups())


def _decimal(value):
    if not isinstance(value, (str, Decimal)):
        raise ExecutionSupportError("INVALID_INPUT", "价格/比例须为十进制字符串或Decimal")
    return number(value, positive=True)


class _Pending(Exception):
    def __init__(self, kind, **details):
        self.issue = {"kind": kind, **details}


class _Evidence:
    def __init__(self, values, cutoff, as_of):
        if not isinstance(values, (list, tuple)):
            raise ExecutionSupportError("INVALID_INPUT", "市场证据须为列表")
        self.values, self.used = deepcopy(list(values)), []
        for q in self.values:
            if not isinstance(q, dict) or set(q) != {"method", "kwargs", "result"}:
                raise ExecutionSupportError("INVALID_INPUT", "查询证据结构无效")
            if q["method"] not in _ALLOWED_QUERIES or not isinstance(q["kwargs"], dict):
                raise ExecutionSupportError("INVALID_INPUT", "只接受公开只读证据")
            if not isinstance(q["result"], dict):
                raise ExecutionSupportError("INVALID_INPUT", "查询结果须为对象")
            if aware_time(q["kwargs"].get("knowledge_cutoff")) != cutoff:
                raise ExecutionSupportError("INVALID_INPUT", "查询知识截止与盘前截止不一致")
            if q["method"] == "prices" and session_date(q["kwargs"].get("end")) > as_of:
                raise ExecutionSupportError("INVALID_INPUT", "盘前不得读取目标日或以后行情")

    def get(self, method, *, conflict=False, **filters):
        found = [
            q
            for q in self.values
            if q["method"] == method and all(q["kwargs"].get(k) == v for k, v in filters.items())
        ]
        if not found:
            raise _Pending("MISSING_QUERY", method=method, kwargs=filters)
        if any(policy_digest(q["result"]) != policy_digest(found[0]["result"]) for q in found):
            raise ExecutionSupportError("INTEGRITY_ERROR", "同一查询有矛盾证据", method=method)
        q = found[0]
        if q not in self.used:
            self.used.append(q)
        result = q["result"]
        if result.get("state") == "CORRUPT":
            raise ExecutionSupportError("INTEGRITY_ERROR", "所用来源原件损坏", method=method)
        if result.get("state") not in ({"READY", "CONFLICT"} if conflict else {"READY"}):
            raise _Pending(
                "SOURCE_NOT_READY",
                method=method,
                kwargs=filters,
                source_state=result.get("state"),
                issues=result.get("issues", []),
            )
        ids = result.get("observation_ids")
        if (
            not isinstance(ids, (list, tuple))
            or not ids
            or any(not isinstance(i, str) or not i for i in ids)
        ):
            raise _Pending("MISSING_OBSERVATION_PROOF", method=method, kwargs=filters)

        def check_known(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in {"available_at", "fetched_at"} and item is not None:
                        if aware_time(item) > aware_time(q["kwargs"]["knowledge_cutoff"]):
                            raise ExecutionSupportError("INVALID_INPUT", "所用数据晚于盘前知识截止")
                    else:
                        check_known(item)
            elif isinstance(value, list):
                for item in value:
                    check_known(item)

        check_known(result.get("data"))
        return result.get("data")


def _one_row(data, code, day=None):
    rows = data if isinstance(data, list) else [data]
    rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and row.get("code") == code
        and (day is None or row.get("date") == day)
    ]
    if len(rows) != 1:
        raise _Pending("MISSING_OR_AMBIGUOUS_ROW", code=code, date=day)
    return rows[0]


def _flag(value):
    return int(value) if type(value) in (int, str) and str(value) in {"0", "1"} else None


def _events_issue(events, code, day, target, cutoff):
    if not isinstance(events, list):
        raise _Pending("INVALID_EVENT_VIEW", code=code)
    for event in events:
        if not isinstance(event, dict) or event.get("code") != code:
            raise _Pending("INVALID_EVENT_VIEW", code=code)
        if event.get("status") == "CANCELLED" or event.get("kind") == "NO_DISTRIBUTION":
            continue
        ex = event.get("ex_date")
        if ex is not None:
            ex = session_date(ex)
        relevant = ex is None or ex >= day
        if not relevant:
            continue
        if event.get("conflicts"):
            raise _Pending(
                "EVENT_CONFLICT",
                code=code,
                event_id=event.get("event_id"),
                issues=event["conflicts"],
            )
        available = event.get("available_at") or event.get("_provenance", {}).get("available_at")
        if available and aware_time(available) > cutoff:
            raise ExecutionSupportError("INVALID_INPUT", "盘前事项晚于知识截止")
        if event.get("kind") == "COMPLEX":
            return "KNOWN_COMPLEX_ACTION"
        if ex is None and event.get("status") == "IMPLEMENTED":
            raise _Pending("UNDATED_ACTION", code=code, event_id=event.get("event_id"))
        if ex and day <= ex <= target and event.get("status") not in {"IMPLEMENTED", "CANCELLED"}:
            raise _Pending(
                "EVENT_IMPLEMENTATION_UNKNOWN", code=code, event_id=event.get("event_id")
            )
    return None


def normalize_proposal(proposal):
    """Return the single canonical proposal shape used by validation and replay identity.

    This checks only serialization/shape, not account resources or trading decisions.
    """
    try:
        p = asdict(proposal) if is_dataclass(proposal) else deepcopy(proposal)
        if not isinstance(p, dict):
            raise ExecutionSupportError("INVALID_INPUT", "策略提案须为对象")
        reject_credentials(p)
        if set(p) - {f.name for f in fields(StrategyProposal)}:
            raise ExecutionSupportError("INVALID_INPUT", "策略提案含未支持字段")
        for name in ("no_order_reason", "rebalance_task_id"):
            p.setdefault(name, None)
        p.setdefault("provenance_mode", "MANUAL")
        p.setdefault("decision_ref", None)
        if p["decision_ref"] is not None:
            p["decision_ref"] = normalize_decision_ref(p["decision_ref"])
        for name in ("market_snapshot_ids", "data_evidence"):
            p.setdefault(name, ())
        for name in ("target_session", "market_as_of"):
            p[name] = session_date(p[name])
        for name in ("input_cutoff", "generated_at"):
            p[name] = aware_time(p[name]).astimezone(timezone.utc).isoformat()
        if not isinstance(p.get("ordered_intents"), (list, tuple)):
            raise ExecutionSupportError("INVALID_INPUT", "意图须为列表")
        for intent in p["ordered_intents"]:
            if not isinstance(intent, dict):
                raise ExecutionSupportError("INVALID_INPUT", "意图须为对象")
            for name in ("quantity", "budget_fen", "sell_fraction"):
                intent.setdefault(name, None)
        policy_digest(p)
        return to_wire(p)
    except (KeyError, TypeError) as exc:
        raise ExecutionSupportError("INVALID_INPUT", "提案字段缺失或格式无效") from exc


class DefaultPlanValidator:
    """Validate an immutable proposal using already-read public evidence."""

    def validate_plan(self, proposal, account_snapshot, market_evidence, policy_bundle, *, now,
                      decision_core=None):
        result = {
            "status": "INVALID_INPUT",
            "validation_id": None,
            "proposal_hash": None,
            "account_snapshot_id": None,
            "account_snapshot_hash": None,
            "account_revision": None,
            "market_as_of": None,
            "target_session": None,
            "input_cutoff": None,
            "accepted_orders": [],
            "skipped_intents": [],
            "issues": [],
            "required_inputs": [],
            "policy_bundle": {},
            "policy_bundle_hash": None,
            "market_evidence": [],
            "plan_envelope": None,
            "provenance_mode": "MANUAL",
            "decision_ref": None,
            "source_proposal": {},
            "strategy_market_snapshot_ids": [],
            "strategy_data_evidence": [],
        }
        try:
            p = normalize_proposal(proposal)
            if not isinstance(account_snapshot, dict):
                raise ExecutionSupportError("INVALID_INPUT", "账户须为对象")
            # The shared canonical digest rejects floats, non-finite values and unsupported types.
            result["proposal_hash"] = policy_digest(p)
            result.update(provenance_mode=p["provenance_mode"], decision_ref=deepcopy(p["decision_ref"]),
                          source_proposal=deepcopy(p),
                          strategy_market_snapshot_ids=deepcopy(list(p["market_snapshot_ids"])),
                          strategy_data_evidence=deepcopy(list(p["data_evidence"])))
            validate_decision_core(p, decision_core)
            result["account_snapshot_hash"] = policy_digest(
                {k: v for k, v in account_snapshot.items() if k not in _ACCOUNT_VOLATILE}
            )
            result.update(
                account_snapshot_id=account_snapshot.get("state_id"),
                account_revision=account_snapshot.get("account_revision"),
            )
            self._validate(
                p, deepcopy(account_snapshot), market_evidence, policy_bundle, now, result
            )
        except _Pending as exc:
            result.update(
                status="WAITING_VALIDATION", issues=[exc.issue], required_inputs=[exc.issue]
            )
        except ExecutionSupportError as exc:
            result.update(
                status=exc.code, issues=[{"kind": exc.code, "reason": exc.message, **exc.details}]
            )
            if exc.code == "WAITING_VALIDATION":
                result["required_inputs"] = [{"kind": "DECISION_CORE", **exc.details}]
        except (KeyError, TypeError, ValueError, ArithmeticError):
            result.update(
                status="INVALID_INPUT",
                issues=[{"kind": "INVALID_INPUT", "reason": "输入字段缺失或格式无效"}],
            )
        if result["status"] != "VALID":
            result["plan_envelope"] = None
        result["validation_id"] = "validation:" + policy_digest(
            {k: v for k, v in result.items() if k not in {"validation_id", "plan_envelope"}}
        )
        return result

    def _validate(self, p, account, evidence, bundle, now, result):
        for field in (
            "proposal_id",
            "account_id",
            "strategy_id",
            "strategy_version",
            "account_snapshot_id",
        ):
            _text(p.get(field), field)
        target, day = session_date(p["target_session"]), session_date(p["market_as_of"])
        cutoff, generated, now = (
            aware_time(p["input_cutoff"]),
            aware_time(p["generated_at"]),
            aware_time(now),
        )
        result.update(target_session=target, market_as_of=day, input_cutoff=cutoff.isoformat())
        deadline = datetime.combine(date.fromisoformat(target), time(9), SHANGHAI)
        available_at = datetime.combine(date.fromisoformat(day), time(18, 10), SHANGHAI)
        if not available_at <= cutoff <= generated <= now:
            raise ExecutionSupportError("INVALID_INPUT", "行情/输入/生成/处理时点先后无效")
        if now >= deadline:
            raise ExecutionSupportError(
                "INVALID_PLAN", "已到目标日09:00截止", reason_code="MISSED_CUTOFF"
            )
        if (
            account.get("account_id") != p["account_id"]
            or account.get("strategy_id") != p["strategy_id"]
        ):
            raise ExecutionSupportError("INVALID_INPUT", "策略与账户身份不一致")
        if account.get("target_session") != target:
            raise ExecutionSupportError("INVALID_INPUT", "账户视图目标日不一致")
        if integer(p["account_revision"]) != integer(account["account_revision"]) or p[
            "account_snapshot_id"
        ] != account.get("state_id"):
            raise ExecutionSupportError("STALE_ACCOUNT", "策略账户修订已过期")
        if aware_time(account["snapshot_at"]) > now:
            raise ExecutionSupportError("INVALID_INPUT", "账户读取时点在未来")
        if account.get("can_plan") is not True:
            raise _Pending("ACCOUNT_NOT_PLANNABLE", reasons=account.get("blocking_reasons", []))
        cash = integer(account["available_cash_fen"])
        integer(account["cash_fen"])
        positions = account.get("positions")
        if not isinstance(positions, list):
            raise ExecutionSupportError("INVALID_INPUT", "账户持仓格式无效")
        bundle = validate_policy_bundle(bundle, target_session=target)
        if aware_time(bundle["knowledge_cutoff"]) != cutoff:
            raise ExecutionSupportError("INVALID_INPUT", "政策包知识截止不一致")
        result.update(policy_bundle=deepcopy(bundle), policy_bundle_hash=bundle["ruleset_hash"])
        intents = p.get("ordered_intents")
        if not isinstance(intents, (list, tuple)):
            raise ExecutionSupportError("INVALID_INPUT", "意图须为列表")
        normalized, codes, indexes = [], set(), set()
        for input_position, intent in enumerate(intents):
            if not isinstance(intent, dict):
                raise ExecutionSupportError("INVALID_INPUT", "意图须为对象")
            allowed = {
                "code",
                "side",
                "reason",
                "order_index",
                "limit_price",
                "quantity",
                "budget_fen",
                "sell_fraction",
            }
            if set(intent) - allowed:
                raise ExecutionSupportError("INVALID_INPUT", "意图含未支持字段")
            code = _code(intent["code"])
            index = integer(intent["order_index"])
            if index != input_position:
                raise ExecutionSupportError("INVALID_PLAN", "order_index须与含HOLD的原意图位置一致")
            if code in codes or index in indexes:
                raise ExecutionSupportError("INVALID_PLAN", "重复代码或顺序编号")
            codes.add(code)
            indexes.add(index)
            _text(intent.get("reason"), "意图理由")
            side = intent.get("side")
            if side not in {"BUY", "SELL", "HOLD"}:
                raise ExecutionSupportError("INVALID_INPUT", "方向无效")
            if side == "HOLD":
                if any(
                    intent.get(k) is not None
                    for k in ("limit_price", "quantity", "budget_fen", "sell_fraction")
                ):
                    raise ExecutionSupportError("INVALID_INPUT", "HOLD不得携带交易数量/价格")
                continue
            price = _decimal(intent.get("limit_price"))
            if price % Decimal("0.01"):
                raise ExecutionSupportError("INVALID_INPUT", "限价须为0.01整数倍")
            fields = ("quantity", "budget_fen") if side == "BUY" else ("quantity", "sell_fraction")
            forbidden = "sell_fraction" if side == "BUY" else "budget_fen"
            if (
                sum(intent.get(k) is not None for k in fields) != 1
                or intent.get(forbidden) is not None
            ):
                raise ExecutionSupportError("INVALID_INPUT", "数量与预算/比例字段矛盾")
            if intent.get("quantity") is not None:
                q = integer(intent["quantity"], minimum=1)
                if q > MAX_ORDER_SHARES or (side == "BUY" and q % 100):
                    raise ExecutionSupportError("INVALID_PLAN", "显式数量不符合整手或单笔上限")
            if intent.get("budget_fen") is not None:
                integer(intent["budget_fen"])
            if intent.get("sell_fraction") is not None and _decimal(intent["sell_fraction"]) > 1:
                raise ExecutionSupportError("INVALID_INPUT", "卖出比例须在0到1之间")
            normalized.append({**intent, "code": code, "price": price})
        limit = daily_symbol_limit(bundle['execution_policy'], account['strategy_id'])
        if limit is not None and len(normalized) > limit:
            raise ExecutionSupportError("INVALID_PLAN", "筛除前买卖意图超过冻结政策代码上限")
        # order_index describes the strategy's explicit order, not arbitrary container order.
        normalized.sort(key=lambda i: (i["side"] != "SELL", i["order_index"]))
        ev = _Evidence(evidence, cutoff, day)
        target_days = ev.get("calendar", start=target, end=target)
        if (
            not isinstance(target_days, list)
            or len(target_days) != 1
            or target_days[0].get("date") != target
        ):
            raise _Pending("TARGET_CALENDAR_UNKNOWN")
        flag = _flag(target_days[0].get("is_trading_day"))
        if flag is None:
            raise _Pending("TARGET_CALENDAR_UNKNOWN")
        if flag != 1:
            raise ExecutionSupportError("INVALID_PLAN", "目标日不是交易日")
        previous = ev.get("calendar_operation", operation="previous_session", day=target)
        if previous != day:
            raise ExecutionSupportError("INVALID_PLAN", "D须为N的前一真实交易日")
        if not normalized and not p.get("no_order_reason") and not intents:
            raise ExecutionSupportError("INVALID_PLAN", "空计划必须有理由")
        if p.get("no_order_reason") is not None:
            _text(p["no_order_reason"], "空计划理由")
        task = p.get("rebalance_task_id")
        if task is not None:
            _text(task, "调仓父任务ID")
        accepted, skipped, pending = [], [], []
        for intent in normalized:
            try:
                reason = self._qualification(intent, ev, day, target, cutoff)
                if reason:
                    skipped.append(
                        {
                            "code": intent["code"],
                            "side": intent["side"],
                            "reason_code": reason,
                            "reason": intent["reason"],
                            "input_order_index": intent["order_index"],
                        }
                    )
                    continue
                q, sizing = self._quantity(intent, positions, bundle["fee_policy"], target)
                if q == 0:
                    skipped.append(
                        {
                            "code": intent["code"],
                            "side": intent["side"],
                            "reason_code": "BELOW_ONE_LOT"
                            if intent["side"] == "BUY"
                            else "BELOW_SELLABLE_LOT",
                            "reason": intent["reason"],
                            "sizing": sizing,
                        }
                    )
                    continue
                fee = fee_breakdown(
                    intent["price"], q, intent["side"], bundle["fee_policy"], target
                )
                accepted.append(
                    {
                        "code": intent["code"],
                        "side": intent["side"],
                        "quantity": q,
                        "limit_price": format(intent["price"], "f"),
                        "reason": intent["reason"],
                        "reserved_cash_fen": -fee["net_cash_fen"] if intent["side"] == "BUY" else 0,
                        "reserved_shares": q if intent["side"] == "SELL" else 0,
                        "order_index": len(accepted),
                        "input_order_index": intent["order_index"],
                        "sizing": sizing,
                    }
                )
            except _Pending as exc:
                pending.append({**exc.issue, "code": intent["code"]})
        result.update(accepted_orders=accepted, skipped_intents=skipped, market_evidence=ev.used)
        if pending:
            result.update(status="WAITING_VALIDATION", issues=pending, required_inputs=pending)
            return
        if sum(o["reserved_cash_fen"] for o in accepted) > cash:
            raise ExecutionSupportError("INVALID_PLAN", "全部买单含费预留超过确定可用现金")
        plan_id = "plan:" + policy_digest(
            {
                "proposal": result["proposal_hash"],
                "account": result["account_snapshot_hash"],
                "market": ev.used,
                "policy": bundle["ruleset_hash"],
            }
        )
        orders = tuple(
            FrozenOrder(
                order_id=f"{plan_id}:{o['code']}",
                code=o["code"],
                side=o["side"],
                quantity=o["quantity"],
                limit_price=Decimal(o["limit_price"]),
                reason=o["reason"],
                reserved_cash_fen=o["reserved_cash_fen"],
                reserved_shares=o["reserved_shares"],
                order_index=o["order_index"],
            )
            for o in accepted
        )
        no_order = (
            p.get("no_order_reason")
            or (
                "; ".join(i["reason_code"] for i in skipped)
                if skipped
                else "; ".join(i["reason"] for i in intents if i["side"] == "HOLD")
            )
            or None
        )
        result["status"] = "VALID"
        report = {
            k: deepcopy(v) for k, v in result.items() if k not in {"plan_envelope", "validation_id"}
        }
        result["plan_envelope"] = PlanEnvelope(
            plan_id=plan_id,
            account_id=p["account_id"],
            strategy_id=p["strategy_id"],
            strategy_version=p["strategy_version"],
            target_session=date.fromisoformat(target),
            market_as_of=date.fromisoformat(day),
            input_cutoff=cutoff,
            generated_at=generated,
            account_revision=account["account_revision"],
            market_snapshot_ids=(),
            orders=orders,
            fee_policy=bundle["fee_policy"],
            rule_policy=bundle["execution_policy"],
            account_snapshot=account,
            account_snapshot_id=account["state_id"],
            account_snapshot_hash=result["account_snapshot_hash"],
            ruleset=bundle,
            no_order_reason=no_order if not orders else None,
            task_id=task,
            data_evidence=tuple(ev.used),
            validation_report=report,
            skipped_intents=tuple(skipped),
            target_pool_evidence=next((q for q in ev.used if q["method"] == "pool"), {}),
            calendar_evidence=tuple(q for q in ev.used if q["method"] == "calendar_operation"),
            provenance_mode=p["provenance_mode"],
            decision_ref=deepcopy(p["decision_ref"]),
            strategy_market_snapshot_ids=tuple(p["market_snapshot_ids"]),
            strategy_data_evidence=tuple(deepcopy(p["data_evidence"])),
        )

    def _qualification(self, intent, ev, day, target, cutoff):
        code, side = intent["code"], intent["side"]
        if not code.startswith(("SH60", "SZ00")):
            return "NOT_MAIN_BOARD"
        security = _one_row(ev.get("security", code=code), code)
        board = security.get("board")
        aliases = {"MAIN", "主板", "SSE_MAIN_A", "SZSE_MAIN_A", "沪市主板", "深市主板"}
        if board is not None and board not in aliases:
            return "NOT_MAIN_BOARD"
        if security.get("delisted") and session_date(security["delisted"]) <= target:
            return "SECURITY_NOT_TRADABLE"
        if not board or not security.get("listed") or security.get("eligibility_known") is not True:
            raise _Pending("SECURITY_IDENTITY_UNKNOWN", code=code)
        source_status = _flag(security.get("source_status"))
        if source_status is None or (source_status == 0 and not security.get("delisted")):
            raise _Pending("SECURITY_STATUS_UNKNOWN", code=code)
        if security.get("special_status_unknown") is True:
            raise _Pending("SPECIAL_STATUS_UNKNOWN", code=code)
        if (
            security.get("relisting_date") == target
            or security.get("delisting_start_date") == target
        ):
            return "MODEL_UNSUPPORTED_SESSION"
        bar = _one_row(ev.get("prices", code=code, start=day, end=day), code, day)
        _decimal(bar.get("close"))
        if str(bar.get("adjustflag")) != "3":
            raise _Pending("UNADJUSTED_PRICE_REQUIRED", code=code)
        if _flag(bar.get("tradestatus")) is None:
            raise _Pending("TRADE_STATUS_UNKNOWN", code=code)
        if side == "SELL":
            # Current holdings may leave the pool, become ST or have future complex actions.
            return None
        if (
            security.get("delisting_start_date")
            and session_date(security["delisting_start_date"]) <= target
        ):
            return "DELISTING"
        st = _flag(bar.get("isST"))
        if st is None:
            raise _Pending("ST_STATUS_UNKNOWN", code=code)
        if st:
            return "ST"
        if _flag(bar.get("tradestatus")) == 0:
            return "SUSPENDED"
        pool = ev.get("pool", as_of=target)
        if not isinstance(pool, dict) or not isinstance(pool.get("selected"), list):
            raise _Pending("POOL_UNKNOWN", code=code)
        if pool.get("effective_from") and session_date(pool["effective_from"]) > target:
            raise _Pending("POOL_NOT_EFFECTIVE", code=code)
        if code not in {_code(row["code"]) for row in pool["selected"]}:
            return "OUTSIDE_POOL"
        sessions = ev.get("calendar_operation", operation="window", day=day, size=121)
        if (
            not isinstance(sessions, list)
            or len(sessions) != 121
            or sessions != sorted(set(sessions))
            or sessions[-1] != day
        ):
            raise _Pending("LISTING_WINDOW_UNKNOWN", code=code)
        if session_date(security["listed"]) > sessions[0]:
            return "NEW_LISTING"
        coverage = ev.get("event_coverage", code=code, start=day, end=day)
        if not isinstance(coverage, dict) or coverage.get("complete") is not True:
            raise _Pending("EVENT_COVERAGE_UNKNOWN", code=code)
        return _events_issue(ev.get("events", conflict=True, code=code), code, day, target, cutoff)

    def _quantity(self, intent, positions, fees, target):
        return intent_quantity(intent, positions, fees, target)


def validate_plan(proposal, account_snapshot, market_evidence, policy_bundle, *, now,
                  decision_core=None):
    return DefaultPlanValidator().validate_plan(
        proposal, account_snapshot, market_evidence, policy_bundle, now=now,
        decision_core=decision_core
    )
