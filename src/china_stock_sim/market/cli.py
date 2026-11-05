"""Standalone data commands: no account, model, strategy or reporting imports."""

import argparse
import json
import sqlite3
from pathlib import Path

from china_stock_sim.market.contracts import DataRequest, PendingEventRange
from china_stock_sim.market.event_operations import EVENT_OPERATIONS
from china_stock_sim.market.pools import prepare_pool
from china_stock_sim.market.preparation import data_status
from china_stock_sim.market.repository import MarketRepository
from china_stock_sim.market.service import MarketDataService
from china_stock_sim.market.settings import data_doctor

COMMANDS = {"prepare-data", "data-status", "data-doctor", "data-sync", "data-query",
            "data-calendar", "data-freeze", "data-snapshot", "data-revisions",
            "data-rebuild-events"}


def register_commands(commands):
    prepare = commands.add_parser("prepare-data", help="首次或月末候选池准备；默认仅检查缓存")
    prepare.add_argument("--as-of", required=True)
    prepare.add_argument("--download", action="store_true")
    prepare.add_argument("--repair", action="store_true")
    prepare.add_argument("--force", action="store_true", help="显式准备后继池，保留原池")
    prepare.add_argument("--max-requests", type=int)
    prepare.add_argument("--budget-seconds", type=int)
    commands.add_parser("data-doctor", help="离线检查数据环境，不检查账户或模型")
    commands.add_parser("data-status", help="只读查看数据任务与准备状态")
    sync = commands.add_parser("data-sync", help="显式采集与增量补缺；默认只评估不联网")
    sync.add_argument("--as-of", required=True)
    sync.add_argument("--codes", default="")
    sync.add_argument("--holdings", default="")
    sync.add_argument("--orders", default="")
    sync.add_argument("--actions", default="")
    sync.add_argument("--pending-event-ranges", type=Path,
                      help="权益待办范围JSON列表（code/start/end/event_ids可选）；不扩大行情窗口")
    sync.add_argument("--history-sessions", type=int, default=121)
    sync.add_argument("--download", action="store_true")
    sync.add_argument("--refresh", action="store_true", help="显式复查源订正，保留既有事实")
    sync.add_argument("--repair", action="store_true", help="隔离损坏原件后补取")
    sync.add_argument("--no-events", action="store_true")
    sync.add_argument("--no-indices", action="store_true")
    sync.add_argument("--max-requests", type=int)
    sync.add_argument("--budget-seconds", type=int)
    rebuild = commands.add_parser("data-rebuild-events", help="离线重建权益分类；默认只显示计划")
    rebuild.add_argument("--as-of", required=True)
    rebuild.add_argument("--codes", required=True)
    rebuild.add_argument("--apply", action="store_true", help="显式原子追加修正记录")
    rebuild.add_argument("--expected-plan", help="应用时必须提供已核对的计划摘要")
    query = commands.add_parser("data-query", help="只读查询，绝不触发采集")
    query.add_argument("--code", required=True)
    query.add_argument("--as-of", required=True)
    query.add_argument("--purpose", choices=["valuation", "llm", "rules", "execution", "events",
                                             "event_settlement", "index"],
                       default="valuation")
    query.add_argument("--known-at")
    query.add_argument("--event-ids", help="权益结算待办事件 ID，逗号分隔；省略则检查当日事项")
    query.add_argument("--event-operation", choices=EVENT_OPERATIONS, default="settlement",
                       help="权益事实动作；READY只指事实齐全，不表示已到操作日")
    query.add_argument("--rule-context", type=Path, help="执行层提供的规则 JSON 文件")
    calendar = commands.add_parser("data-calendar", help="只读查询真实交易日历")
    calendar.add_argument("operation", choices=["window", "next_session", "previous_session", "is_month_end"])
    calendar.add_argument("--date", required=True)
    calendar.add_argument("--size", type=int, default=121)
    calendar.add_argument("--known-at")
    freeze = commands.add_parser("data-freeze", help="显式保存不可变数据快照，不采集")
    freeze.add_argument("--codes", required=True)
    freeze.add_argument("--as-of", required=True)
    freeze.add_argument("--purposes", default="valuation")
    freeze.add_argument("--known-at")
    freeze.add_argument("--event-operation", choices=EVENT_OPERATIONS, default="settlement")
    freeze.add_argument("--event-ids", help="明确待办事件ID，逗号分隔；仅支持单证券冻结")
    commands.add_parser("data-snapshot", help="只读校验并读取已冻结快照").add_argument("snapshot_id")
    revisions = commands.add_parser("data-revisions", help="只读查看来源事实修订")
    revisions.add_argument("--kind", choices=["bar", "index", "security", "event"], default="bar")
    revisions.add_argument("--code", required=True)
    revisions.add_argument("--start")
    revisions.add_argument("--end")


def codes(value):
    return tuple(code.strip() for code in value.split(",") if code.strip())


def pending_event_ranges(path: Path | None) -> tuple[PendingEventRange, ...]:
    """Parse explicit business scope without loading account or model configuration."""
    if path is None:
        return ()
    with path.open("rb") as stream:
        content = stream.read(1_000_001)
    if len(content) > 1_000_000:
        raise ValueError("权益待办范围文件超过大小限制")
    value = json.loads(content)
    if not isinstance(value, list) or len(value) > 1000:
        raise ValueError("权益待办范围必须是最多1000项的JSON列表")
    if any(not isinstance(item, dict) for item in value):
        raise ValueError("每项权益待办范围必须是JSON对象")
    return tuple(PendingEventRange(**item) for item in value)


def execute(args, root: Path) -> tuple[dict, int]:
    service = MarketDataService(root)
    if args.command == "data-rebuild-events":
        from china_stock_sim.market.event_rebuild import rebuild_event_classification

        result = rebuild_event_classification(root, codes=codes(args.codes), as_of=args.as_of,
            apply=args.apply, expected_plan=args.expected_plan)
        return result, 0
    if args.command == "data-doctor":
        result = data_doctor(root)
        return result, 0 if result["state"] == "READY" else 2
    if args.command == "data-status":
        return {"preparation": data_status(root), "jobs": MarketRepository(root).states("jobs"),
                "status_scope": "saved_progress_not_readiness"}, 0
    if args.command == "prepare-data":
        result = prepare_pool(root, args.as_of, download=args.download, repair=args.repair,
                              force=args.force, max_requests=args.max_requests,
                              budget_seconds=args.budget_seconds)
        return result, 0 if result["state"] in {"POOL_PREPARED", "UNCHANGED"} else 2
    if args.command == "data-sync":
        range_path = getattr(args, "pending_event_ranges", None)
        ranges = pending_event_ranges(range_path)
        candidates = codes(args.codes)
        if not candidates and range_path is None:
            pool = service.pool(args.as_of)
            if pool.ready:
                candidates = tuple(row["code"] for row in pool.data["selected"])
        request = DataRequest(as_of=args.as_of, candidates=candidates,
                              holdings=codes(args.holdings), pending_orders=codes(args.orders),
                              pending_actions=codes(args.actions),
                              history_sessions=args.history_sessions,
                              include_events=not args.no_events, include_indices=not args.no_indices,
                              pending_event_ranges=ranges)
        result = service.update(request, download=args.download, refresh=args.refresh,
                                repair=args.repair, max_requests=args.max_requests,
                                budget_seconds=args.budget_seconds)
        return result, 0 if result["state"] == "COMPLETE" else 2
    if args.command == "data-query":
        operation = getattr(args, "event_operation", "settlement")
        if operation != "settlement" and args.purpose != "event_settlement":
            raise ValueError("--event-operation仅适用于event_settlement用途")
        rule = json.loads(args.rule_context.read_text()) if args.rule_context else None
        result = service.readiness(args.code, args.as_of, args.purpose,
                                   knowledge_cutoff=args.known_at, rule_context=rule,
                                   event_ids=(codes(args.event_ids)
                                              if getattr(args, "event_ids", None) is not None
                                              else None), operation=operation)
        return result.as_dict(), 0 if result.ready else 2
    if args.command == "data-calendar":
        result = service.calendar_operation(args.operation, args.date, size=args.size,
                                            knowledge_cutoff=args.known_at)
        return result.as_dict(), 0 if result.ready else 2
    if args.command == "data-freeze":
        operation = getattr(args, "event_operation", "settlement")
        event_ids = (codes(args.event_ids) if getattr(args, "event_ids", None) is not None
                     else None)
        if (operation != "settlement" or event_ids is not None) and (
                "event_settlement" not in codes(args.purposes)):
            raise ValueError("权益动作/事件ID冻结必须包含event_settlement用途")
        result = service.freeze(codes(args.codes), args.as_of, knowledge_cutoff=args.known_at,
                                 purposes=codes(args.purposes), operation=operation,
                                 event_ids=event_ids)
        return result, 0
    if args.command == "data-snapshot":
        return service.read_snapshot(args.snapshot_id), 0
    if args.command == "data-revisions":
        result = service.revisions(args.kind, args.code, args.start, args.end)
        return result.as_dict(), 0 if result.ready else 2
    raise ValueError("未知数据命令")


def main(argv=None):
    parser = argparse.ArgumentParser(description="独立数据层：显式采集、只读查询")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    register_commands(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(argv)
    try:
        result, code = execute(args, args.root.resolve())
    except (ValueError, KeyError, TypeError, RuntimeError, OSError, sqlite3.Error) as exc:
        result, code = {"state": "ERROR", "reason": str(exc)}, 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code
