"""Source access and public facts are separate interfaces; neither writes accounts."""
from typing import Protocol

from china_stock_sim.market.contracts import DataRequest, DataResult, MarketSnapshot


class SourceProvider(Protocol):
    provider_id: str
    adapter_version: str

    def fetch(self, query: str, params: dict) -> dict: ...


class MarketDataReader(Protocol):
    """Public consumer contract; reads never collect or touch an account ledger."""

    def prices(self, code: str, start: str, end: str, *, knowledge_cutoff=None,
               index=False) -> DataResult: ...

    def signal_prices(self, code: str, as_of: str, *, size: int = 121,
                      knowledge_cutoff=None, prepared_inputs=None) -> DataResult:
        """Read a causal adjusted-price window using only facts known at the cutoff.

        READY rows are ordered by published trading sessions and retain raw_close,
        adjusted_close and event_ids; observation_ids identify the consumed facts.
        size is a positive integer, independent of readiness's fixed 21/121 windows.
        Missing or unverified inputs stay non-READY; this never collects or writes.
        Query event_coverage separately when detailed coverage diagnostics are needed.
        prepared_inputs optionally supplies this assembly's already-read window,
        prices, events and coverage DataResults plus exact code/as_of/size/cutoff
        identity. It is a pure reuse input, never serialized in query evidence.
        """
        ...

    def compose_signal_prices(self, *, window: DataResult, prices: DataResult,
                              events: DataResult, event_coverage: DataResult,
                              knowledge_cutoff) -> DataResult:
        """Pure combination of same-query facts; no collection or verification bypass."""
        ...

    def readiness(self, code: str, as_of: str, purpose: str, *, knowledge_cutoff=None,
                  rule_context=None, event_ids=None, operation="settlement") -> DataResult: ...

    def events(self, code: str, *, knowledge_cutoff=None,
               start=None, end=None) -> DataResult: ...

    def event_coverage(self, code: str, start: str, end: str,
                       *, knowledge_cutoff=None) -> DataResult: ...

    def event_settlement(self, code: str, session: str, *, knowledge_cutoff=None,
                         event_ids=None, operation="settlement") -> DataResult: ...

    def calendar(self, *, knowledge_cutoff=None, start=None, end=None) -> DataResult: ...

    def calendar_operation(self, operation: str, day: str, *, size=121,
                           knowledge_cutoff=None) -> DataResult: ...

    def security(self, code: str, *, knowledge_cutoff=None) -> DataResult: ...

    def pool(self, as_of: str, *, knowledge_cutoff=None) -> DataResult: ...

    def freeze(self, codes, as_of: str, *, knowledge_cutoff=None, purposes=("valuation",),
               rule_context=None, event_ids=None, operation="settlement") -> MarketSnapshot: ...

    def read_snapshot(self, snapshot_id: str) -> MarketSnapshot: ...

    def evidence_manifest(self, snapshot_ids, *, observation_ids=()) -> dict: ...

    def revisions(self, kind: str, code: str, start=None, end=None) -> DataResult: ...

    def update(self, request: DataRequest, **kwargs) -> dict: ...
