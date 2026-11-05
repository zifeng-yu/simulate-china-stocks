"""Daily accounting and valuation: append statements, never synthesize trades."""

from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from china_stock_sim.common.event_accounting_scope import event_accounting_scope
from china_stock_sim.common.execution_views import statement_record
from china_stock_sim.ledger.corporate import _compatible
from china_stock_sim.ledger.kernel import LedgerError, canonical
from china_stock_sim.ledger.queries import (
    UNRESOLVED,
    historical_lots,
    latest_statements,
    pending_cases,
)
from china_stock_sim.ledger.statement_inputs import _projection
from china_stock_sim.market.calendar import TradingCalendar
from china_stock_sim.market.contracts import aware_time, session_date


def fen(value):
    return int(Decimal(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _economic_record(row):
    """Compare authoritative transaction facts, not their immutable audit wrappers."""
    ignored = {'record_reference', 'original_record_ref', 'storage_format', 'last_attempt',
        'last_attempt_identity', 'account_view_evidence', 'data_evidence', 'execution_context',
        'account_view_hash', 'account_revision', 'expected_revision', 'computed_at',
        'processed_at', 'recorded_at', 'knowledge_cutoff', 'snapshot_at', 'result_id'}
    value = {key: child for key, child in row.items() if key not in ignored}
    if isinstance(value.get('result'), dict):
        value['result'] = _economic_record(value['result'])
    return value


def _financial_value(value, field=None):
    if isinstance(value, dict):
        return {key: _financial_value(child, key) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_financial_value(child) for child in value]
    if value is not None and field in {'price', 'close', 'receivable_quantity', 'net_value'}:
        number = Decimal(str(value))
        if not number.is_finite():
            raise LedgerError('INTEGRITY_ERROR', '账单含非有限经济数值')
        return format(number.normalize(), 'f')
    return value


def _statement_semantics(statement):
    """Full daily economics plus consumed input meaning; audit timing stays historical."""
    ignored = {'id', 'statement_revision', 'account_revision', 'report_status',
        'knowledge_cutoff', 'snapshot_ids', 'data_evidence', 'processed_at',
        'supersedes_revision', 'original_record_ref', 'storage_format'}
    value = {key: child for key, child in statement.items() if key not in ignored}
    for table in ('orders', 'fills'):
        value[table] = [_economic_record(row) for row in statement.get(table, ())]
    queries = []
    for item in statement.get('data_evidence', ()):
        queries.append({'method': item['method'], 'kwargs': {
            key: child for key, child in item['kwargs'].items() if key != 'knowledge_cutoff'},
            'result': _projection(item['method'], item['result'], statement['session'])})
    value['input_semantics'] = sorted(queries, key=canonical)
    return canonical(_financial_value(value))


def _row_day(row):
    return row.get("effective_date") or row.get("recorded_at", "")[:10]


def continuous_watermarks(statements, start, calendar_rows):
    """Advance only through a complete calendar prefix and complete statements."""
    coverage = TradingCalendar(calendar_rows).between(start, max(statements))
    first_gap = min((row['date'] for row in coverage.issues
                     if row.get('kind') == 'MISSING_CALENDAR'), default=None)
    settled = valued = None
    valuation_open = True
    for day in coverage.data:
        if first_gap is not None and day >= first_gap:
            break
        item = statements.get(day)
        if not item or item['accounting_status'] != 'COMPLETE':
            break
        settled = day
        if valuation_open and item['valuation_status'] == 'FINAL':
            valued = day
        else:
            valuation_open = False
    return settled, valued


def cash_at(tx, day):
    return sum(r["amount_fen"] for r in tx.list("cash_entries")
               if r["kind"] == "INITIAL_CAPITAL" or _row_day(r) <= day)


def _known_amount_at(row, day, field, history, date_field):
    entries = row.get(history)
    if entries:
        return sum(r.get("amount_fen", 0) for r in entries if r["effective_date"] <= day)
    return row.get(field, 0) if not row.get(date_field) or row[date_field] <= day else 0


def entitlement_values(tx, day):
    cash_receivable, dividend_income = 0, 0
    shares = []
    for row in tx.list("entitlements"):
        if row.get("recognition_date", "9999") > day:
            continue
        if row["kind"] == "CASH":
            paid = _known_amount_at(row, day, "paid_fen", "payments", "paid_date")
            cash_receivable += row["amount_fen"] - paid
            dividend_income += row["amount_fen"]
        elif row["kind"] == "SHARES":
            delivered = row.get("delivered_quantity", 0)
            cost = row.get("remaining_cost_fen", row.get("cost_fen", 0))
            if row.get("delivery_date", row.get("listing_date") or "9999") > day:
                delivered, cost = 0, row.get("cost_fen", 0)
            quantity = Decimal(row["quantity"]) - delivered
            if quantity:
                shares.append({**row, "remaining_quantity": str(quantity), "remaining_cost_fen": cost})
    return cash_receivable, dividend_income, shares


def tax_values(tx, day):
    assessed = paid = 0
    unknown = []
    for row in tx.list("tax_items"):
        if not row.get("assessments") and row.get("trigger_date", row.get("effective_date", "")) > day:
            continue
        if row.get("status") == "NOT_TRIGGERED":
            continue
        if row.get("assessed_fen") is None:
            unknown.append({"kind": "UNKNOWN_TAX_LIABILITY", "tax_id": row.get("tax_id", row.get("id"))})
            continue
        assessed += _known_amount_at(row, day, "assessed_fen", "assessments", "trigger_date")
        paid += _known_amount_at(row, day, "paid_fen", "payments", "paid_date")
    return assessed, paid, unknown


class StatementsMixin:
    def _market_query(self, evidence, method, **kwargs):
        result = getattr(self.market, method)(**kwargs)
        evidence.append({"method": method, "kwargs": kwargs, "result": result.as_dict()})
        return result

    def _valuation_price(self, code, day, cutoff, evidence, history_start=None, cases=()):
        current = self._market_query(evidence, "prices", code=code, start=day, end=day,
                                     knowledge_cutoff=cutoff)
        candidates = current.data if isinstance(current.data, list) else []
        if current.ready and candidates:
            row = candidates[-1]
            value = Decimal(row["close"])
            if value.is_finite() and value > 0 and not row.get("conflicts"):
                return {"price": str(value), "price_date": row["date"], "status": "FINAL"}
        if current.state in {"CORRUPT", "CONFLICT"}:
            return {"price": None, "price_date": None, "status": "UNAVAILABLE", "reason": current.state}
        start = (datetime.fromisoformat(day).date() - timedelta(days=366)).isoformat()
        if history_start:
            start = min(start, history_start)
        history = self._market_query(evidence, "prices", code=code, start=start, end=day,
                                     knowledge_cutoff=cutoff)
        if history.state in {"CORRUPT", "CONFLICT"}:
            return {"price": None, "price_date": None, "status": "UNAVAILABLE", "reason": history.state}
        valid = [row for row in history.data if row["date"] <= day and not row.get("conflicts")
                 and Decimal(row["close"]).is_finite() and Decimal(row["close"]) > 0]
        if not valid:
            return {"price": None, "price_date": None, "status": "UNAVAILABLE", "reason": "NO_TRUSTED_PRICE"}
        row = max(valid, key=lambda r: r["date"])
        events = self._market_query(evidence, "events", code=code, knowledge_cutoff=cutoff)
        coverage = self._market_query(evidence, "event_coverage", code=code, start=row["date"], end=day,
                                     knowledge_cutoff=cutoff)
        incomparable = not events.ready or not coverage.ready or any(
            not event.get("ex_date") or row["date"] < event["ex_date"] <= day
            for event in events.data if event_accounting_scope(event, day, cases) == 'REQUIRED')
        if incomparable:
            return {"price": None, "price_date": row["date"], "status": "UNAVAILABLE",
                    "reason": "UNCONFIRMED_ADJUSTMENT_BASIS"}
        return {"price": row["close"], "price_date": row["date"], "status": "PROVISIONAL",
                "reason": "LAST_TRUSTED_PRICE"}

    def _event_accounting_issues(self, tx, day, cutoff, evidence):
        """Do not declare completion when a known, entitled action was never registered."""
        issues = []
        prior_day = (datetime.fromisoformat(day).date() - timedelta(days=1)).isoformat()
        codes = sorted({lot["code"] for lot in historical_lots(tx, day) + historical_lots(tx, prior_day)} |
                       {row["code"] for row in tx.list("fills") if row["target_session"] == day} |
                       {case["code"] for case in pending_cases(tx)})
        cases = tx.list("corporate_action_cases")
        for code in codes:
            result = self._market_query(evidence, "events", code=code, knowledge_cutoff=cutoff)
            coverage = self._market_query(evidence, "event_coverage", code=code, start=day, end=day,
                                          knowledge_cutoff=cutoff)
            if result.state == "CORRUPT" or not coverage.ready:
                issues.append({"kind": "EVENT_EVIDENCE_UNAVAILABLE", "code": code})
                continue
            for event in result.data:
                if event_accounting_scope(event, day, cases) != 'REQUIRED':
                    continue
                record = event.get("record_date")
                if not record:
                    if any(lot["code"] == code for lot in historical_lots(tx, day)):
                        issues.append({"kind": "UNDATED_EVENT", "code": code, "event_id": event.get("event_id")})
                    continue
                if record > day:
                    continue
                eligible = sum(lot["quantity"] for lot in historical_lots(tx, record) if lot["code"] == code)
                if not eligible:
                    continue
                if (event.get('conflicts') or
                        (event.get('status') is not None and event['status'] != 'IMPLEMENTED') or
                        (event.get('kind') is not None and event['kind'] not in {
                            'DISTRIBUTION', 'NO_DISTRIBUTION'})):
                    issues.append({'kind': 'UNCONFIRMED_EVENT_TERMS', 'code': code,
                                   'event_id': event.get('event_id')})
                    continue
                candidates = [case for case in cases if case["code"] == code and (
                    event.get("event_id") in case.get("event_ids", []) or
                    (event.get("proposal_date") and case.get("proposal_date") == event["proposal_date"]))]
                if len(candidates) != 1:
                    issues.append({"kind": "UNREGISTERED_OR_AMBIGUOUS_EVENT", "code": code,
                                   "event_id": event.get("event_id")})
                    continue
                case = candidates[0]
                if (case.get('event') and not _compatible(case['event'], event)):
                    issues.append({'kind': 'UNCONFIRMED_EVENT_TERMS', 'code': code,
                                   'event_id': event.get('event_id'), 'case_id': case['case_id']})
                    continue
                if (event.get('status') == 'IMPLEMENTED' and not event.get('ex_date') and
                        any(event.get(field) is None or Decimal(event[field]) > 0 for field in (
                            'cash_per_share', 'bonus_per_share', 'capital_per_share'))):
                    issues.append({'kind': 'UNKNOWN_EVENT_SCHEDULE', 'code': code,
                                   'event_id': event.get('event_id')})
                    continue
                terms = [("cash_accrual", event.get("ex_date"), event.get("cash_per_share")),
                    ("share_accrual", event.get("ex_date"), None if event.get("bonus_per_share") is None or
                     event.get("capital_per_share") is None else
                     Decimal(event["bonus_per_share"]) + Decimal(event["capital_per_share"])),
                    ("cash_payment", event.get("payment_date"), event.get("cash_per_share")),
                    ("share_delivery", event.get("share_listing_date"), None if event.get("bonus_per_share") is None or
                     event.get("capital_per_share") is None else
                     Decimal(event["bonus_per_share"]) + Decimal(event["capital_per_share"]))]
                for action, due, amount in terms:
                    if due and due <= day and (amount is None or Decimal(amount) != 0):
                        if case.get("actions", {}).get(action, {}).get("state") not in {"APPLIED", "NOT_APPLICABLE"}:
                            issues.append({"kind": "DUE_ACTION_UNPROCESSED", "case_id": case["case_id"], "operation": action})
        return issues

    def close_session(self, account_id, payload, *, operation_id, expected_revision):
        payload = dict(payload)
        day = session_date(payload.get("session", payload.get("effective_date")))
        now = self._now()
        cutoff = aware_time(payload.get("knowledge_cutoff", now.isoformat()))
        if cutoff > now:
            raise ValueError("日结知识截止不能晚于实际处理时点")
        local_now = now.astimezone(ZoneInfo("Asia/Shanghai"))
        if local_now.date().isoformat() < day or (local_now.date().isoformat() == day and local_now.time() < time(18, 10)):
            return {"state": "NOT_DUE", "reason": "目标日尚未到日终处理时间", "session": day}
        def close(tx):
            self._verify_evidence(payload, required=False)
            evidence = []
            start = tx.state.get("simulation_start_session")
            if not start or day < start:
                raise LedgerError("INVALID_INPUT", "观察起点前不能生成交易日账单")
            calendar = self._market_query(evidence, "calendar", start=day, end=day,
                                          knowledge_cutoff=cutoff.isoformat())
            if not calendar.ready or not calendar.data:
                raise LedgerError("WAITING_DATA", "日结缺少可信交易日历")
            if str(calendar.data[0].get("is_trading_day")) != "1":
                raise LedgerError("INVALID_INPUT", "非交易日不生成交易日账单")
            statements = latest_statements(tx, summary=True)
            previous_day = None
            prior = None
            accounting_issues = []
            if day != start:
                previous = self._market_query(evidence, "calendar_operation", operation="previous_session",
                    day=day, knowledge_cutoff=cutoff.isoformat())
                if not previous.ready:
                    accounting_issues.append({"kind": "MISSING_PREVIOUS_SESSION"})
                else:
                    previous_day = previous.data
                    prior = statements.get(previous_day)
                    if not prior or prior["accounting_status"] != "COMPLETE":
                        accounting_issues.append({"kind": "PRIOR_ACCOUNTING_INCOMPLETE", "session": previous_day})
            unresolved_orders = [r for r in tx.list("orders") if r["target_session"] <= day
                                 and r["status"] in {"PENDING", "WAITING_DATA"}]
            if unresolved_orders:
                accounting_issues.append({"kind": "UNRESOLVED_ORDERS", "order_ids": [r["order_id"] for r in unresolved_orders]})
            for case in tx.list("corporate_action_cases"):
                if case.get("status") in UNRESOLVED and (case.get("record_date") or "") <= day:
                    accounting_issues.append({"kind": case["status"], "case_id": case["case_id"]})
            for entitlement in tx.list("entitlements"):
                due = entitlement.get("payment_date") if entitlement["kind"] == "CASH" else entitlement.get("listing_date")
                if entitlement.get("status") == "WAITING_ALLOCATION":
                    accounting_issues.append({"kind": "WAITING_ALLOCATION", "case_id": entitlement["case_id"]})
                elif due and due <= day and entitlement.get("status") not in {"PAID", "DELIVERED"}:
                    accounting_issues.append({"kind": "DUE_ENTITLEMENT_UNSETTLED", "case_id": entitlement["case_id"]})
            accounting_issues.extend(self._event_accounting_issues(tx, day, cutoff.isoformat(), evidence))
            lots = historical_lots(tx, day)
            cash_receivable, dividends, share_receivables = entitlement_values(tx, day)
            tax_assessed, tax_paid, unknown = tax_values(tx, day)
            accounting_issues.extend(unknown)
            for tax in tx.list("tax_items"):
                if tax.get("due_date") and tax["due_date"] <= day and tax.get("status") == "PAYABLE":
                    accounting_issues.append({"kind": "DUE_TAX_UNPAID", "tax_id": tax.get("id")})
            quantities = defaultdict(lambda: {"shares": 0, "cost_fen": 0, "receivable_quantity": Decimal(0), "receivable_cost_fen": 0})
            for lot in lots:
                quantities[lot["code"]]["shares"] += lot["quantity"]
                quantities[lot["code"]]["cost_fen"] += lot["cost_fen"]
            for item in share_receivables:
                quantities[item["code"]]["receivable_quantity"] += Decimal(item["remaining_quantity"])
                quantities[item["code"]]["receivable_cost_fen"] += item["remaining_cost_fen"]
            positions, market_value, share_value, unavailable = [], 0, 0, False
            valuation_status = "FINAL"
            for code in sorted(quantities):
                price = self._valuation_price(code, day, cutoff.isoformat(), evidence,
                    min((lot["acquired_date"] for lot in lots if lot["code"] == code), default=start),
                    tx.list('corporate_action_cases', code=code))
                item = quantities[code]
                value = fen(Decimal(price["price"]) * item["shares"] * 100) if price["price"] is not None else None
                receivable = fen(Decimal(price["price"]) * item["receivable_quantity"] * 100) if price["price"] is not None else None
                positions.append({"code": code, **item, "receivable_quantity": str(item["receivable_quantity"]),
                    **price, "market_value_fen": value, "share_receivable_value_fen": receivable})
                if value is None or receivable is None:
                    unavailable = True
                else:
                    market_value += value
                    share_value += receivable
                if price["status"] != "FINAL":
                    valuation_status = "PROVISIONAL"
            if unavailable:
                valuation_status = "UNAVAILABLE"
            elif accounting_issues or any(e.get("status") == "WAITING_ALLOCATION" for e in share_receivables):
                valuation_status = "PROVISIONAL"
            cash = cash_at(tx, day)
            tax_payable = tax_assessed - tax_paid
            known_total = cash + market_value + cash_receivable + share_value - tax_payable
            asset_value = None if unavailable else known_total
            final_value = asset_value if valuation_status == "FINAL" else None
            initial = tx.account["initial_cash_fen"]
            prior_value = initial if day == start else (prior.get("net_assets_fen") if prior else None)
            fills = tx.list("fills", target_session=day)
            totals = tx.db.execute("""SELECT
                COALESCE(SUM(json_extract(data,'$.realized_pnl_fen')),0),
                COALESCE(SUM(json_extract(data,'$.fees_fen')),0) FROM fills
                WHERE account_id=? AND COALESCE(json_extract(data,'$.effective_date'),
                json_extract(data,'$.target_session'))<=?""", (account_id, day)).fetchone()
            costs = sum(lot["cost_fen"] for lot in lots) + sum(e["remaining_cost_fen"] for e in share_receivables)
            realized = totals[0]
            corrections = [r for r in tx.list("corrections") if r.get("effective_date", "") <= day]
            realized += sum(r.get("realized_cost_adjustment_fen", 0) + r.get("realized_pnl_adjustment_fen", 0) for r in corrections)
            correction_pnl = sum(r.get("pnl_adjustment_fen", r.get("delta_fen", r.get("cash_adjustment_fen", 0))) for r in corrections)
            unrealized = None if unavailable else market_value + share_value - costs
            decomposition = None if unrealized is None else realized + unrealized + dividends - tax_assessed + correction_pnl
            if final_value is not None and decomposition != final_value - initial:
                raise LedgerError("INTEGRITY_ERROR", "收益分解与资产变化不守恒", expected=final_value - initial, actual=decomposition)
            current = statements.get(day)
            revision = current.get("statement_revision", 0) + 1 if current else 1
            plan = next(iter(tx.list("plans", target_session=day)), None)
            reasons = [r for r in tx.list("job_runs") if r.get("target_session", r.get("session")) == day and r.get("kind") == "NO_PLAN"]
            plan_status = plan.get("status", "FROZEN") if plan else (reasons[-1].get("status", reasons[-1].get("reason", "NO_PLAN")) if reasons else "NO_PLAN")
            entries = [r for r in tx.list("cash_entries") if r["kind"] != "INITIAL_CAPITAL" and _row_day(r) == day]
            movements = defaultdict(int)
            for entry in entries:
                movements[entry["kind"]] += entry["amount_fen"]
            opening = initial if day == start else (cash_at(tx, previous_day) if previous_day else None)
            if opening is not None and opening + sum(r["amount_fen"] for r in entries) != cash:
                raise LedgerError("INTEGRITY_ERROR", "日现金期初、流水、期末不守恒")
            accounting_status = "PARTIAL" if accounting_issues else "COMPLETE"
            statement = {"id": f"{day}:{revision}", "account_id": account_id, "session": day, "statement_revision": revision,
                "account_revision": tx.state["account_revision"] + 1,
                "accounting_status": accounting_status, "valuation_status": valuation_status,
                "plan_status": plan_status, "report_status": "PENDING_PUBLICATION",
                "opening_cash_fen": opening, "closing_cash_fen": cash,
                "cash_changes": dict(movements), "cash_entries": entries,
                "positions": positions, "cash_receivable_fen": cash_receivable,
                "share_receivable_value_fen": None if unavailable else share_value,
                "market_value_fen": None if unavailable else market_value,
                "tax_payable_fen": tax_payable, "cumulative_tax_expense_fen": tax_assessed,
                "known_assets_fen": known_total, "provisional_assets_fen": asset_value,
                "net_assets_fen": final_value, "cumulative_pnl_fen": final_value - initial if final_value is not None else None,
                "daily_pnl_fen": final_value - prior_value if final_value is not None and prior_value is not None else None,
                "net_value": str(Decimal(final_value) / initial) if final_value is not None else None,
                "realized_pnl_fen": realized, "unrealized_pnl_fen": unrealized,
                "cumulative_dividend_income_fen": dividends, "correction_pnl_fen": correction_pnl,
                "pnl_decomposition_fen": decomposition,
                "cumulative_transaction_fees_fen": totals[1] + sum(
                    r.get("transaction_fee_adjustment_fen", 0) for r in corrections),
                "orders": tx.list("orders", target_session=day),
                "fills": fills,
                "issues": accounting_issues, "knowledge_cutoff": cutoff.isoformat(),
                "snapshot_ids": payload.get("snapshot_ids", payload.get("market_snapshot_ids", [])),
                "data_evidence": evidence, "processed_at": tx.now,
                "supersedes_revision": current.get("statement_revision") if current else None}
            # Read only this day's actual stored row, not the invalidation overlay
            # used by consumer views. Compare before any snapshot/CAS is created.
            original = tx.get('daily_nav', current['id']) if current else None
            if original is not None and _statement_semantics(original) == _statement_semantics(statement):
                progressed = self._advance_statement_progress(tx, day, original, cutoff)
                if not progressed:
                    return {'state': 'ALREADY_APPLIED', 'statement': original,
                            'statement_changed': False, 'recovery_progressed': False}
                self._statement_watermarks(tx, start, cutoff)
                return {'state': 'APPLIED', 'statement': original,
                        'statement_changed': False, 'recovery_progressed': True}
            statement['orders'] = [statement_record(self.root, row, 'orders') for row in statement['orders']]
            statement['fills'] = [statement_record(self.root, row, 'fills') for row in statement['fills']]
            tx.append("daily_nav", f"{day}:{revision}", statement)
            tx.append("daily_positions", f"{day}:{revision}", {"session": day, "statement_revision": revision, "positions": positions})
            self._advance_statement_progress(tx, day, statement, cutoff)
            self._statement_watermarks(tx, start, cutoff)
            return {"state": "APPLIED", "statement": statement}
        return self._mutate(account_id, operation_id, payload, expected_revision, day, close)

    def _advance_statement_progress(self, tx, day, statement, cutoff):
        """A genuine recovery cursor may advance without duplicating equal NAV."""
        if tx.state.get('restate_from') != day:
            return False
        complete = (statement['accounting_status'] == 'COMPLETE'
                    and statement['valuation_status'] == 'FINAL')
        if not complete:
            return False
        through = tx.state.get('restate_through') or max(latest_statements(tx, summary=True))
        if day >= through:
            tx.state['restate_from'] = tx.state['restate_through'] = None
        else:
            following = self.market.calendar_operation('next_session', day,
                                                       knowledge_cutoff=cutoff.isoformat())
            if not following.ready:
                return False
            tx.state['restate_from'], tx.state['restate_through'] = following.data, through
        for job in tx.list('job_runs', kind='RESTATE_SESSION', session=day):
            tx.save('job_runs', job['id'], {**job, 'status': 'COMPLETE',
                                           'statement_revision': statement['statement_revision']})
        return True

    def _statement_watermarks(self, tx, start, cutoff):
        statements = latest_statements(tx, summary=True)
        calendar = self.market.calendar(start=start, end=max(statements),
                                        knowledge_cutoff=cutoff.isoformat())
        settled = valued = None
        if calendar.ready:
            settled, valued = continuous_watermarks(statements, start, calendar.data)
        tx.state['settled_through'], tx.state['valued_through'] = settled, valued
