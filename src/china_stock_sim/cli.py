"""数据、三策略、执行、账本、报告及显式整日流程与调度入口。"""

import argparse
import json
import sqlite3
import sys
from importlib import import_module
from pathlib import Path

from china_stock_sim import __version__
from china_stock_sim.config import load_settings, project_root
from china_stock_sim.ledger.store import initialize, read_snapshot
from china_stock_sim.models import ChatMessage, create_model_client, load_model_settings
from china_stock_sim.models.settings import model_status
from china_stock_sim.scheduler.workspace import ensure_directories, writer_lock

CAPABILITIES = {
    "account_initialization": "IMPLEMENTED",
    "persistent_account_queries": "IMPLEMENTED",
    "frozen_strategy_audit_and_reports": "IMPLEMENTED",
    "dynamic_account_creation": "IMPLEMENTED",
    "persistent_account_policies": "IMPLEMENTED",
    "strategy_decision_audit": "IMPLEMENTED",
    "policy_bound_model_attempts": "IMPLEMENTED",
    "initial_report": "IMPLEMENTED",
    "market_collection": "IMPLEMENTED",
    "s1_s1lt_execution": "IMPLEMENTED",
    "llm_strategy": "IMPLEMENTED",
    "replaceable_model_client": "IMPLEMENTED",
    "order_matching_and_fees": "IMPLEMENTED",
    "plan_validation": "IMPLEMENTED",
    "execution_read_only_commands": "IMPLEMENTED",
    "execution_submission_recovery": "IMPLEMENTED",
    "corporate_actions": "LEDGER_IMPLEMENTED",
    "ledger_accounting": "IMPLEMENTED",
    "ledger_recovery_and_backup": "IMPLEMENTED",
    "daily_recovery": "IMPLEMENTED",
    "background_scheduler": "IMPLEMENTED",
    "runtime_homepage": "IMPLEMENTED",
    "static_dashboard_publishing": "IMPLEMENTED",
    "runtime_observability": "IMPLEMENTED",
    "local_shutdown_readiness": "IMPLEMENTED",
}


def emit(value: dict, *, error: bool = False) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2), file=sys.stderr if error else sys.stdout)


def _publishing_cli():
    """Load the local extension; an installed but broken extension is an error."""
    try:
        return import_module('china_stock_sim.publishing.cli')
    except ModuleNotFoundError as exc:
        if exc.name != 'china_stock_sim.publishing':
            raise
        return None


def publish_report(settings, snapshot):
    from china_stock_sim.ledger import LedgerService
    from china_stock_sim.reporting.ledger import write_ledger_report

    outcome = write_ledger_report(settings, snapshot)
    ledger = LedgerService(settings)
    acknowledgments = []
    for published in outcome['published']:
        pending = ledger.pending_work(published['account_id'])
        if not pending['report_pending']:
            continue
        day = ledger._now().astimezone(settings.timezone).date().isoformat()
        acknowledgments.append(ledger.record_step_result(published['account_id'], {
            'effective_date': day, 'step': 'report', 'status': 'SUCCEEDED',
            'rendered_revision': published['report_revision']},
            operation_id=f'published:{published["report_revision"]}',
            expected_revision=published['account_revision']))
    return {**outcome, 'publication_receipts': acknowledgments}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="前向模拟：独立数据、账本、执行检查与报告")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--root", type=Path, help="项目根目录；放在子命令之前")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="新空账本按种子建立账户；已有账本仅核验，不采集或交易")
    commands.add_parser("doctor", help="只读检查初始化状态；不代表交易系统已经接通")
    commands.add_parser("status", help="只读查看账户及账本状态")
    commands.add_parser("report", help="从现有账本生成初始化页或逐日账单")
    model_parser = commands.add_parser("model-check", help="只读核对模型配置；可显式测试连接")
    model_parser.add_argument("--live", action="store_true", help="发送一次无项目数据的测试请求")
    from china_stock_sim.application.workflow_cli import COMMANDS as WORKFLOW_COMMANDS
    from china_stock_sim.application.workflow_cli import execute as execute_workflow
    from china_stock_sim.application.workflow_cli import register_commands as register_workflow
    from china_stock_sim.desktop.indicator_cli import COMMANDS as INDICATOR_COMMANDS
    from china_stock_sim.desktop.indicator_cli import execute as execute_indicator
    from china_stock_sim.desktop.indicator_cli import register_commands as register_indicator
    from china_stock_sim.execution.cli import COMMANDS as EXECUTION_COMMANDS
    from china_stock_sim.execution.cli import execute as execute_execution
    from china_stock_sim.execution.cli import register_commands as register_execution
    from china_stock_sim.ledger.cli import COMMANDS as LEDGER_COMMANDS
    from china_stock_sim.ledger.cli import execute as execute_ledger
    from china_stock_sim.ledger.cli import register_commands as register_ledger
    from china_stock_sim.market.cli import COMMANDS, execute, register_commands
    from china_stock_sim.readiness.cli import COMMANDS as READINESS_COMMANDS
    from china_stock_sim.readiness.cli import execute as execute_readiness
    from china_stock_sim.readiness.cli import register_commands as register_readiness
    from china_stock_sim.runtime.cli import COMMANDS as RUNTIME_COMMANDS
    from china_stock_sim.runtime.cli import execute as execute_runtime
    from china_stock_sim.runtime.cli import execute_observed
    from china_stock_sim.runtime.cli import register_commands as register_runtime
    from china_stock_sim.scheduler.cli import COMMANDS as SCHEDULER_COMMANDS
    from china_stock_sim.scheduler.cli import execute as execute_scheduler
    from china_stock_sim.scheduler.cli import register_commands as register_scheduler
    from china_stock_sim.scheduler.dashboard_cli import COMMANDS as DASHBOARD_COMMANDS
    from china_stock_sim.scheduler.dashboard_cli import execute as execute_dashboard
    from china_stock_sim.scheduler.dashboard_cli import register_commands as register_dashboard
    from china_stock_sim.strategies.cli import COMMANDS as STRATEGY_COMMANDS
    from china_stock_sim.strategies.cli import execute as execute_strategy
    from china_stock_sim.strategies.cli import register_commands as register_strategies

    publishing = _publishing_cli()
    publishing_commands = publishing.COMMANDS if publishing is not None else set()
    register_commands(commands)
    register_ledger(commands)
    register_execution(commands)
    register_strategies(commands)
    register_workflow(commands)
    register_scheduler(commands)
    register_dashboard(commands)
    if publishing is not None:
        publishing.register_commands(commands)
    register_runtime(commands)
    register_readiness(commands)
    register_indicator(commands)
    args = parser.parse_args(argv)
    try:
        if args.command in READINESS_COMMANDS | INDICATOR_COMMANDS:
            handler = execute_readiness if args.command in READINESS_COMMANDS else execute_indicator
            result, exit_code = handler(args, project_root(args.root))
            emit(result)
            return exit_code
        if args.command in RUNTIME_COMMANDS:
            result, exit_code = execute_runtime(args, project_root(args.root))
            emit(result)
            return exit_code
        if args.command in DASHBOARD_COMMANDS | publishing_commands:
            handler = execute_dashboard if args.command in DASHBOARD_COMMANDS else publishing.execute
            result, exit_code = execute_observed(handler, args, project_root(args.root))
            emit(result)
            return exit_code
        if args.command in WORKFLOW_COMMANDS | SCHEDULER_COMMANDS:
            handler = execute_workflow if args.command in WORKFLOW_COMMANDS else execute_scheduler
            result, exit_code = execute_observed(handler, args, project_root(args.root))
            emit(result)
            return exit_code
        if args.command in STRATEGY_COMMANDS:
            result, exit_code = execute_strategy(args, project_root(args.root))
            emit(result)
            return exit_code
        if args.command in EXECUTION_COMMANDS:
            result, exit_code = execute_execution(args, project_root(args.root))
            emit(result)
            return exit_code
        if args.command in LEDGER_COMMANDS:
            result, exit_code = execute_ledger(args, project_root(args.root))
            emit(result)
            return exit_code
        if args.command in COMMANDS:
            root = project_root(args.root)
            result, exit_code = execute(args, root)
            # Reporting is an outer application concern, never a dependency of the data layer.
            if args.command == "prepare-data" and result.get("preparation"):
                from china_stock_sim.reporting.preparation import write_preparation_report

                try:
                    write_preparation_report(root, result["preparation"])
                except Exception as exc:
                    result["report_error"] = str(exc)
            emit(result)
            return exit_code
        settings = load_settings(args.root)
        if args.command == "init":
            ensure_directories(settings)
            with writer_lock(settings):
                result = initialize(settings)
                snapshot = read_snapshot(settings)
                publish_report(settings, snapshot)
            emit({**result, "state": snapshot["state"], "report": str(settings.report)})
        elif args.command == "doctor":
            from china_stock_sim.market.preparation import data_status
            from china_stock_sim.market.repository import MarketRepository
            from china_stock_sim.market.settings import data_doctor

            checks = {
                "python": sys.version.split()[0],
                "python_compatible": (3, 12) <= sys.version_info[:2] < (3, 13),
                "executable": sys.executable,
                "using_project_venv": Path(sys.prefix).resolve() == settings.root / ".venv",
                "project": str(settings.root),
                "config": "OK",
                "data_preparation": {
                    key: value for key, value in data_status(settings.root).items()
                    if key in {"state", "price_ready", "signal_ready", "trading_ready"}
                },
                "model": model_status(
                    settings.root, settings.raw["strategies"]["llm"]["model_ref"]
                ),
                "initial_report": {
                    "path": str(settings.report),
                    "ready": settings.report.is_file()
                    and any(marker in settings.report.read_text(encoding="utf-8")
                            for marker in ("INITIALIZED_NOT_RUNNING", "LEDGER_REPORT")),
                },
            }
            checks["data_preparation"]["scope"] = "saved_pool_preparation_progress"
            try:
                checks["data_service"] = data_doctor(settings.root)
                jobs = MarketRepository(settings.root).states("jobs").values()
                recent = max(jobs, key=lambda job: job["started_at"], default=None)
                checks["data_service"]["latest_saved_job"] = (
                    {key: recent.get(key) for key in ("as_of", "state", "updated_at",
                                                     "requests_this_run", "indices_complete")}
                    if recent else None)
                checks["data_service"]["readiness_check"] = "use_data_query_for_requested_purpose"
            except (OSError, ValueError, sqlite3.Error) as exc:
                checks["data_service"] = {"state": "NOT_READY", "reason": str(exc)}
            try:
                checks["ledger"] = read_snapshot(settings)
                ready = (
                    checks["python_compatible"]
                    and checks["using_project_venv"]
                    and checks["initial_report"]["ready"]
                )
            except (FileNotFoundError, ValueError, sqlite3.Error) as exc:
                checks["ledger"] = {"state": "NOT_READY", "reason": str(exc)}
                ready = False
            try:
                from china_stock_sim.application.workflow import WorkflowApplication

                checks['workflow'] = WorkflowApplication(settings).inspect()
            except (OSError, ValueError, RuntimeError, sqlite3.Error):
                checks['workflow'] = {'state': 'NOT_READY', 'configured_for_forward_workflow': False}
            emit(
                {
                    "foundation_ready": ready,
                    "forward_simulation_ready": bool(ready and checks['workflow'].get(
                        'configured_for_forward_workflow')),
                    "checks": checks,
                    "capabilities": {**CAPABILITIES, 'static_dashboard_publishing': (
                        'IMPLEMENTED' if publishing is not None else 'LOCAL_COMPONENT_NOT_INSTALLED')},
                }
            )
            return 0 if ready else 2
        elif args.command == "status":
            emit(read_snapshot(settings))
        elif args.command == "model-check":
            ref = settings.raw["strategies"]["llm"]["model_ref"]
            status = model_status(settings.root, ref)
            if not status["configured"]:
                emit(status)
                return 2
            if args.live:
                client = create_model_client(load_model_settings(settings.root, ref))
                response = client.complete(
                    [ChatMessage("user", '这是连接测试，请只返回 JSON：{"ok":true}')],
                    max_output_tokens=32,
                    json_mode=True,
                )
                try:
                    valid = json.loads(response.text) == {"ok": True}
                except ValueError:
                    valid = False
                emit(
                    {
                        **status,
                        "network_tested": True,
                        "test_response_valid": valid,
                        "finish_reason": response.finish_reason,
                        "usage": response.usage,
                        "strategy_enabled": False,
                    }
                )
                return 0 if valid else 2
            emit({**status, "strategy_enabled": False})
        elif args.command == "report":
            # Do not create a database as a side effect of requesting a report.
            if not settings.database.is_file():
                raise FileNotFoundError("请先执行 init 建立账户")
            with writer_lock(settings):
                snapshot = read_snapshot(settings)
                publication = publish_report(settings, snapshot)
            emit({"report": str(settings.report), "state": snapshot["state"],
                  'publication': publication})
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, sqlite3.Error) as exc:
        emit({"state": getattr(exc, "state", getattr(exc, "code", "ERROR")), "reason": str(exc)}, error=True)
        return 2
