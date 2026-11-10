"""Offline fixed-input cached-update comparison; never reads production data.

Creates one temporary 100-security, 121-session market with controlled sources,
then measures one same-day cached update. --source selects a temporary reference
src directory. Provider calls during the measured update are forbidden.
"""

import argparse
import hashlib
import json
import resource
import sys
import tempfile
from collections import Counter
from pathlib import Path
from time import perf_counter

REPOSITORY = Path(__file__).resolve().parents[1]


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def run(args):
    if args.source:
        sys.path.insert(0, str(Path(args.source).resolve()))
    sys.path.insert(1, str(REPOSITORY / "tests"))
    import pytest
    from test_market_pending_event_ranges import EventSource
    from test_market_recovery import root_with_config
    from test_market_update import AS_OF, Clock

    from china_stock_sim.market import events, freshness, repository, storage, update
    from china_stock_sim.market.contracts import DataRequest
    from china_stock_sim.market.service import MarketDataService

    codes = tuple(f"SH{600000 + index:06d}" for index in range(args.codes))
    clock, provider_calls = Clock(), []

    class ControlledSource(EventSource):
        forbid = False

        def fetch(self, query, params):
            if self.forbid:
                raise AssertionError("Cached benchmark unexpectedly requested a provider")
            if query != "securities":
                return super().fetch(query, params)
            self.calls.append((self.provider_id, query, dict(params)))
            rows = [{"code": code[:2].lower() + "." + code[2:], "code_name": "离线证券",
                     "ipoDate": "2000-01-01", "outDate": "", "type": "1", "status": "1"}
                    for code in codes]
            return {"provider": self.provider_id, "adapter_version": self.adapter_version,
                    "fetched_at": self.clock().isoformat(), "rows": rows,
                    "fields": list(rows[0])}

    patch = pytest.MonkeyPatch()
    with tempfile.TemporaryDirectory(prefix="market-update-perf-") as folder:
        root = root_with_config(Path(folder))
        sources = {name: ControlledSource(name, clock, provider_calls)
                   for name in ("baostock", "eastmoney", "tencent")}
        request = DataRequest(AS_OF, candidates=codes, history_sessions=121,
                              include_events=True, include_indices=True)
        seed = update.MarketUpdater(root, providers=sources, clock=clock).run(request)
        if seed["state"] != "COMPLETE":
            raise AssertionError(("Fixture incomplete", seed))
        for item in sources.values():
            item.forbid = True
        provider_calls.clear()
        repo = repository.MarketRepository(root)

        def saved_facts():
            with repo.connection() as connection:
                return [dict(row) for row in connection.execute(
                    "SELECT * FROM observations ORDER BY id")]

        def saved_raw():
            return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in repo.objects.glob("*.json")}

        initial_facts, initial_raw = saved_facts(), saved_raw()
        calls = Counter()
        modules = [repository, storage, update, freshness]
        try:
            from china_stock_sim.market import raw_validation
        except ImportError:
            raw_validation = None
        if raw_validation is not None:
            modules.append(raw_validation)
        for module in modules:
            if hasattr(module, "read_json"):
                original = module.read_json

                def read(path, _original=original):
                    if path.is_relative_to(repo.objects):
                        calls["raw_json_reads"] += 1
                        calls["raw_json_bytes"] += path.stat().st_size
                    return _original(path)

                patch.setattr(module, "read_json", read)
            if hasattr(module, "digest"):
                original = module.digest

                def digest(value, _original=original):
                    calls["digest_calls"] += 1
                    return _original(value)

                patch.setattr(module, "digest", digest)
        for name in ("validate_raw", "save_raw", "_decode"):
            original = getattr(repository.MarketRepository, name)

            def counted(self, *a, _name=name, _original=original, **kw):
                calls[_name + "_calls"] += 1
                return _original(self, *a, **kw)

            patch.setattr(repository.MarketRepository, name, counted)
        for name in ("normalize_baostock_events", "normalize_eastmoney_events"):
            original = getattr(events, name)

            def normalize(*a, _name=name, _original=original, **kw):
                calls[_name + "_calls"] += 1
                return _original(*a, **kw)

            patch.setattr(events, name, normalize)
        scopes = []
        if raw_validation is not None:
            original_init = raw_validation.RawValidationScope.__init__

            def initialized(self, *a, **kw):
                original_init(self, *a, **kw)
                scopes.append(self)

            patch.setattr(raw_validation.RawValidationScope, "__init__", initialized)
        updater = update.MarketUpdater(root, providers=sources, clock=clock)
        before_cpu = resource.getrusage(resource.RUSAGE_SELF)
        started = perf_counter()
        result = updater.run(request)
        elapsed = perf_counter() - started
        after_cpu = resource.getrusage(resource.RUSAGE_SELF)
        counts = dict(calls)
        patch.undo()
        if result["state"] != "COMPLETE" or result["requests_this_run"] != 0 or provider_calls:
            raise AssertionError(("Cached update not equivalent", result))
        final_facts, final_raw = saved_facts(), saved_raw()
        if initial_facts != final_facts or initial_raw != final_raw:
            raise AssertionError("Cached update changed normalized facts or immutable originals")
        service = MarketDataService(root)
        sessions = seed["sessions"]
        public = {}
        for code in codes:
            public[code] = {
                "prices": service.prices(code, sessions[0], AS_OF,
                                         knowledge_cutoff=clock()).as_dict(),
                "events": service.events(code, knowledge_cutoff=clock()).as_dict(),
                "coverage": service.event_coverage(code, sessions[0], AS_OF,
                                                   knowledge_cutoff=clock()).as_dict(),
                "signals": service.signal_prices(code, AS_OF, size=121,
                                                 knowledge_cutoff=clock()).as_dict()}
        projection = {key: value for key, value in result.items() if key not in {"job_id", "pid"}}
        record = {"scope": "isolated_fixed_input_cached_update_no_network",
                  "source": str(Path(args.source).resolve()) if args.source else "current",
                  "codes": len(codes), "history_sessions": len(sessions),
                  "elapsed_seconds": elapsed,
                  "process_cpu_seconds": after_cpu.ru_utime + after_cpu.ru_stime
                  - before_cpu.ru_utime - before_cpu.ru_stime,
                  "peak_rss_bytes": after_cpu.ru_maxrss * (1 if sys.platform == "darwin" else 1024),
                  "peak_rss_scope": "PROCESS_LIFETIME_NOT_INTERVAL_DELTA",
                  "counts": counts, "validation_scopes": [dict(s.metrics) for s in scopes],
                  "all_scopes_closed": all(s.closed and not s.entries for s in scopes),
                  "provider_calls": provider_calls, "requests_this_run": result["requests_this_run"],
                  "normalized_fact_count": len(final_facts), "raw_object_count": len(final_raw),
                  "facts_unchanged": initial_facts == final_facts,
                  "raw_objects_unchanged": initial_raw == final_raw,
                  "facts_sha256": canonical_hash(final_facts),
                  "raw_objects_sha256": canonical_hash(final_raw),
                  "job_projection_sha256": canonical_hash(projection),
                  "public_query_sha256": canonical_hash(public),
                  "production_read_or_write": False}
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(record, ensure_ascii=False), flush=True)
    return record


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", help="Temporary reference src directory")
    parser.add_argument("--codes", type=int, default=100)
    parser.add_argument("--output", required=True)
    options = parser.parse_args()
    if not 2 <= options.codes <= 100:
        parser.error("--codes must be between 2 and 100")
    run(options)
