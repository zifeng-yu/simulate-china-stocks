"""Public data contracts. Queries are read-only; collection is an explicit operation."""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal, TypedDict

from china_stock_sim.market.quality import normalize_code

DataState = Literal["READY", "PARTIAL", "MISSING", "WAITING_DATA", "CONFLICT", "CORRUPT"]
Purpose = Literal["valuation", "llm", "rules", "execution", "events", "event_settlement", "index"]


class SnapshotBody(TypedDict):
    schema: int
    content: dict
    observation_ids: list[str]


class MarketSnapshot(TypedDict):
    """Verified on-disk snapshot envelope shared with strategy and execution layers.

    content includes as_of, knowledge_cutoff, purposes, pool and per-code results.
    Every result uses DataResult.as_dict(); price/amount fields are decimal strings.
    """

    snapshot_id: str
    body: SnapshotBody


def aware_time(value: str | datetime) -> datetime:
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.utcoffset() is None:
        raise ValueError("数据时点必须包含时区")
    return result


def session_date(value: str | date) -> str:
    if isinstance(value, datetime):
        raise ValueError("交易日不能含具体时间")
    result = date.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(result, date) or (isinstance(value, str) and result.isoformat() != value):
        raise ValueError("交易日必须为 YYYY-MM-DD")
    return result.isoformat()


@dataclass(frozen=True)
class DataResult:
    state: DataState
    data: Any = field(default_factory=list)
    issues: tuple[dict, ...] = ()
    observation_ids: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.state == "READY"

    def as_dict(self) -> dict:
        return {"state": self.state, "data": self.data, "issues": list(self.issues),
                "observation_ids": list(self.observation_ids)}


@dataclass(frozen=True)
class PendingEventRange:
    """Unfinished distribution facts to refresh independently of price history.

    Dates identify the historical event scope, not future payment due dates.
    event_ids are consumer references only; they do not replace source coverage.
    """

    code: str
    start: str
    end: str
    event_ids: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "code", normalize_code(self.code))
        object.__setattr__(self, "start", session_date(self.start))
        object.__setattr__(self, "end", session_date(self.end))
        if self.start > self.end:
            raise ValueError("权益范围起始日晚于截止日")
        if isinstance(self.event_ids, (str, bytes)) or not isinstance(self.event_ids, (tuple, list)):
            raise ValueError("权益事件引用必须为列表或元组")
        if any(not isinstance(value, str) or not value.strip() or len(value) > 256
               for value in self.event_ids):
            raise ValueError("权益事件引用必须为非空字符串，长度不超过256")
        if len(self.event_ids) > 1000:
            raise ValueError("一次权益范围的事件引用过多")
        object.__setattr__(self, "event_ids", tuple(dict.fromkeys(self.event_ids)))


@dataclass(frozen=True)
class DataRequest:
    """No account balances or model clients cross this interface.

    Monetary/price values in returned JSON facts are lossless decimal strings;
    consumers can use Decimal, never binary floating point for accounting.
    """

    as_of: str
    candidates: tuple[str, ...] = ()
    holdings: tuple[str, ...] = ()
    pending_orders: tuple[str, ...] = ()
    pending_actions: tuple[str, ...] = ()
    history_sessions: int = 121
    include_events: bool = True
    include_indices: bool = True
    pending_event_ranges: tuple[PendingEventRange, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "as_of", session_date(self.as_of))
        if type(self.history_sessions) is not int or not 1 <= self.history_sessions <= 500:
            raise ValueError("历史窗口必须为 1 到 500 个交易日")
        for name in ("candidates", "holdings", "pending_orders", "pending_actions"):
            values = tuple(dict.fromkeys(normalize_code(c) for c in getattr(self, name)))
            if len(values) > 1000:
                raise ValueError("一次请求证券过多")
            object.__setattr__(self, name, values)
        if not isinstance(self.pending_event_ranges, (tuple, list)) or any(
                not isinstance(value, PendingEventRange) for value in self.pending_event_ranges):
            raise ValueError("历史权益范围必须为PendingEventRange列表或元组")
        if len(self.pending_event_ranges) > 1000:
            raise ValueError("一次请求历史权益范围过多")
        ranges = tuple(dict.fromkeys(self.pending_event_ranges))
        if ranges and not self.include_events:
            raise ValueError("历史权益范围不能与include_events=False同时使用")
        if any(value.end > self.as_of for value in ranges):
            raise ValueError("历史权益范围不得晚于请求as_of；未来事项通过events视图读取")
        object.__setattr__(self, "pending_event_ranges", ranges)

    @property
    def price_codes(self) -> tuple[str, ...]:
        """Legacy code lists retain their normal history maintenance semantics."""
        return tuple(dict.fromkeys(self.holdings + self.pending_orders + self.pending_actions
                                   + self.candidates))

    @property
    def ordered_codes(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(self.holdings + self.pending_orders + self.pending_actions
                                   + tuple(item.code for item in self.pending_event_ranges)
                                   + self.candidates))
