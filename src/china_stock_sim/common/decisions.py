"""Pure strategy-decision provenance contracts shared by execution and ledger.

These checks establish internal consistency, not an authoritative account-policy
registration. Reading immutable files and checking public market results remain
the responsibilities of the calling application/ledger boundaries.
"""

import re
from copy import deepcopy

from .fees import ExecutionSupportError, aware_time, session_date
from .policies import policy_digest

STRATEGY_QUERY_METHODS = frozenset({
    "calendar", "calendar_operation", "prices", "signal_prices", "security", "pool",
    "events", "event_coverage", "event_settlement", "readiness", "index_quote",
})
_REF_FIELDS = {
    "decision_id", "account_policy_id", "account_policy_hash",
    "strategy_implementation_hash", "input_hash", "decision_core",
}
_CORE_FIELDS = {
    "schema_version", "decision_id", "account_policy", "strategy_implementation_hash",
    "input_bundle", "proposal", "calculation",
}
_POLICY_FIELDS = {
    "policy_id", "account_id", "strategy_id", "strategy_version", "effective_from",
    "effective_to", "known_at", "parameters",
}
_DATA_STATES = {"READY", "PARTIAL", "MISSING", "WAITING_DATA", "CONFLICT", "CORRUPT"}


def _invalid(message):
    raise ExecutionSupportError("INVALID_INPUT", message)


def _integrity(message):
    raise ExecutionSupportError("INTEGRITY_ERROR", message)


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        _invalid("策略决策身份须为非空短字符串")


def _hash(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        _invalid("策略决策摘要须为64位小写SHA256")


def _fields(value, expected, label):
    if not isinstance(value, dict) or set(value) != expected:
        _invalid(f"{label}字段缺失或含未支持字段")


def normalize_decision_ref(value):
    """Validate the single reference shape and return an independent copy."""
    _fields(value, _REF_FIELDS, "策略决策引用")
    for field in ("decision_id", "account_policy_id"):
        _text(value[field])
    for field in ("account_policy_hash", "strategy_implementation_hash", "input_hash"):
        _hash(value[field])
    document = value["decision_core"]
    _fields(document, {"path", "content_hash"}, "策略决策原件引用")
    _hash(document["content_hash"])
    if document["path"] != f"data/plans/strategy/{document['content_hash']}.json":
        _invalid("策略决策原件路径与摘要不一致")
    policy_digest(value)
    return deepcopy(value)


def _known_by(value, cutoff):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"available_at", "fetched_at", "known_at", "knowledge_cutoff"}:
                if item is not None and aware_time(item) > cutoff:
                    _invalid("策略来源晚于盘前知识截止")
            else:
                _known_by(item, cutoff)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _known_by(item, cutoff)


def _historical_prices(value, as_of):
    if isinstance(value, dict):
        for field in ("date", "as_of"):
            if value.get(field) is not None and session_date(value[field]) > as_of:
                _invalid("策略行情晚于声明的行情截止")
        for item in value.values():
            if isinstance(item, (dict, list, tuple)):
                _historical_prices(item, as_of)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _historical_prices(item, as_of)


def _query_evidence(query, cutoff, as_of, target):
    _fields(query, {"method", "kwargs", "result"}, "策略公开查询证据")
    method, kwargs, result = query["method"], query["kwargs"], query["result"]
    if not isinstance(method, str) or method not in STRATEGY_QUERY_METHODS:
        _invalid("策略证据只允许公开只读数据查询")
    if not isinstance(kwargs, dict) or not isinstance(result, dict):
        _invalid("策略查询参数和结果须为对象")
    if aware_time(kwargs.get("knowledge_cutoff")) != cutoff:
        _invalid("策略来源知识截止与提案不一致")
    _fields(result, {"state", "data", "issues", "observation_ids"}, "策略查询结果")
    state = result["state"]
    if not isinstance(state, str) or state not in _DATA_STATES:
        _invalid("策略来源状态无效")
    if not isinstance(result["issues"], (list, tuple)) or any(
        not isinstance(item, dict) for item in result["issues"]
    ):
        _invalid("策略来源问题须为对象列表")
    ids = result["observation_ids"]
    if not isinstance(ids, (list, tuple)):
        _invalid("策略来源观测引用须为列表")
    for identity in ids:
        _text(identity)

    if method == "prices":
        start, end = session_date(kwargs.get("start")), session_date(kwargs.get("end"))
        if start > end or end > as_of:
            _invalid("策略行情查询范围超出声明的行情截止")
    elif method in {"signal_prices", "readiness", "pool"}:
        day = session_date(kwargs.get("as_of"))
        # A next-session pool may be known before that session; its prices may not.
        if day > (target if method == "pool" else as_of):
            _invalid("策略查询目标日超出允许范围")
        if method == "signal_prices":
            size = kwargs.get("size", 121)
            if type(size) is not int or size < 1:
                _invalid("策略信号窗口须为正整数")
    elif method == "index_quote":
        if session_date(kwargs.get("session")) > as_of:
            _invalid("策略指数行情晚于声明的行情截止")
    if method in {"prices", "signal_prices", "index_quote", "readiness"}:
        _historical_prices(result["data"], as_of)
    # Future event dates/calendar sessions are permitted if already publicly known.
    _known_by(result, cutoff)


def validate_provenance(proposal):
    """Validate mode and strategy source evidence without reading any source."""
    if not isinstance(proposal, dict):
        _invalid("策略提案须为对象")
    policy_digest(proposal)  # Reject floats, non-finite Decimals and unsupported values.
    mode = proposal.get("provenance_mode", "MANUAL")
    if mode not in ("MANUAL", "STRATEGY"):
        _invalid("提案来源模式须为MANUAL或STRATEGY")
    snapshots = proposal.get("market_snapshot_ids", ())
    queries = proposal.get("data_evidence", ())
    if not isinstance(snapshots, (list, tuple)) or not isinstance(queries, (list, tuple)):
        _invalid("策略来源快照和查询证据须为列表")
    if mode == "MANUAL":
        if proposal.get("decision_ref") is not None or snapshots or queries:
            _invalid("人工提案不得携带策略决策引用或策略来源")
        return None
    reference = normalize_decision_ref(proposal.get("decision_ref"))
    cutoff = aware_time(proposal.get("input_cutoff"))
    as_of = session_date(proposal.get("market_as_of"))
    target = session_date(proposal.get("target_session"))
    if as_of >= target:
        _invalid("策略行情截止必须早于目标交易日")
    for identity in snapshots:
        _hash(identity)
    for query in queries:
        _query_evidence(query, cutoff, as_of, target)
    return reference


def validate_decision_core(proposal, core):
    """Bind a caller-loaded core to the exact normalized proposal and policy."""
    reference = validate_provenance(proposal)
    if reference is None:
        if core is not None:
            _invalid("人工提案不得携带策略决策原件")
        return None
    if core is None:
        raise ExecutionSupportError(
            "WAITING_VALIDATION", "缺少策略决策原件",
            required_input=reference["decision_core"],
        )
    _fields(core, _CORE_FIELDS, "策略决策原件")
    if type(core["schema_version"]) is not int or core["schema_version"] != 1:
        _invalid("策略决策原件结构不受支持")
    if policy_digest(core) != reference["decision_core"]["content_hash"]:
        _integrity("策略决策原件摘要不符")
    if core["decision_id"] != reference["decision_id"]:
        _integrity("策略决策身份与引用不一致")
    _hash(core["strategy_implementation_hash"])
    if core["strategy_implementation_hash"] != reference["strategy_implementation_hash"]:
        _integrity("策略实现摘要与引用不一致")
    if not isinstance(core["proposal"], dict):
        _invalid("策略决策须保存未绑定引用的提案")
    expected = {key: value for key, value in proposal.items() if key != "decision_ref"}
    if policy_digest(core["proposal"]) != policy_digest(expected):
        _integrity("策略决策原意图与实际提案不一致")
    bundle = core["input_bundle"]
    if not isinstance(bundle, dict) or not {"market_snapshot_ids", "data_evidence"} <= set(bundle):
        _invalid("策略输入包缺少来源列表")
    if policy_digest(bundle) != reference["input_hash"]:
        _integrity("策略输入包摘要不符")
    _known_by(bundle, aware_time(proposal["input_cutoff"]))
    for field in ("market_snapshot_ids", "data_evidence"):
        if policy_digest(bundle[field]) != policy_digest(proposal.get(field, ())):
            _integrity("策略输入来源与提案不一致")
    if not isinstance(core["calculation"], dict):
        _invalid("策略计算过程须为对象")

    policy = core["account_policy"]
    _fields(policy, _POLICY_FIELDS, "策略账户政策快照")
    for field in ("policy_id", "account_id", "strategy_id", "strategy_version"):
        _text(policy[field])
    if policy["policy_id"] != reference["account_policy_id"]:
        _integrity("策略账户政策身份不符")
    if policy_digest(policy) != reference["account_policy_hash"]:
        _integrity("策略账户政策摘要不符")
    for field in ("account_id", "strategy_id", "strategy_version"):
        if policy[field] != proposal.get(field):
            _integrity("策略账户政策与提案身份不一致")
    if not isinstance(policy["parameters"], dict):
        _invalid("策略账户参数须为对象")
    target = session_date(proposal["target_session"])
    start = session_date(policy["effective_from"]) if policy["effective_from"] is not None else None
    end = session_date(policy["effective_to"]) if policy["effective_to"] is not None else None
    if (start and start > target) or (end and target > end) or (start and end and start > end):
        _invalid("策略账户政策未在目标日生效")
    if aware_time(policy["known_at"]) > aware_time(proposal["input_cutoff"]):
        _invalid("策略账户政策晚于盘前知识截止")
    return None
