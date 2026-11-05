"""Supplementary tax facts must remain evidenced, nullable and purpose-isolated."""

from copy import deepcopy

import pytest
from test_market_events import FETCHED, event

from china_stock_sim.market.events import _unique, event_readiness, merge_events
from china_stock_sim.market.providers.baostock import BaoStockProvider
from china_stock_sim.market.providers.eastmoney import EastmoneyEventProvider
from china_stock_sim.market.quality import DataQualityError
from china_stock_sim.market.repository import CorruptData, MarketRepository
from china_stock_sim.market.service import MarketDataService
from china_stock_sim.market.tax_facts import (
    TAX_FIELDS,
    normalize_event_tax_facts,
    tax_field_capabilities,
)


def evidenced(*, face="1.00", capital="STOCK_PREMIUM", provider="issuer_fixture"):
    record = event()
    record["provider"] = provider
    for field in TAX_FIELDS:
        record.pop(field, None)
    record["tax_facts"] = {}
    for field, value in zip(TAX_FIELDS, (face, capital, "已公布的分配税基说明"), strict=True):
        record["tax_facts"][field] = {
            "state": "READY", "value": value,
            "sources": [{"provider": provider, "source_field": field,
                         "raw_reference": "fixture/issuer-distribution.json",
                         "applicable_date": "2026-07-16", "event_id": record["event_id"],
                         "value": value, "row_index": 0}],
        }
    return record


def source(record):
    return {"provider": record["provider"], "adapter_version": "fixture",
            "fetched_at": FETCHED, "rows": [record]}


def persist_evidenced(repository, *, bind=True, source_fetched=FETCHED):
    record = evidenced()
    raw = {"provider": record["provider"], "fetched_at": source_fetched,
           "rows": [{**{field: record["tax_facts"][field]["value"] for field in TAX_FIELDS},
                     "code": record["code"], "event_id": record["event_id"],
                     "applicable_date": "2026-07-16"}]}
    reference = repository.save_raw(raw)
    for fact in record["tax_facts"].values():
        fact["sources"][0]["raw_reference"] = reference
    response = source(record)
    if bind:
        response["dependencies"] = [reference]
    repository.put("event", [normalize_event_tax_facts(record)], response)
    return reference


def test_configured_sources_explicitly_cannot_supply_tax_fields():
    for provider in (BaoStockProvider, EastmoneyEventProvider):
        assert provider.event_tax_capabilities == dict.fromkeys(TAX_FIELDS, "UNSUPPORTED")
    assert tax_field_capabilities("unverified_adapter") == dict.fromkeys(TAX_FIELDS, "UNKNOWN")


def test_current_source_unknown_tax_fields_do_not_block_cash_or_adjustment():
    record = event()
    assert all(record[field] is None for field in TAX_FIELDS)
    assert all(fact["state"] == "UNKNOWN" and fact["reason"] == "SOURCE_UNSUPPORTED"
               for fact in record["tax_facts"].values())
    assert record["tax_readiness"]["state"] == "WAITING_DATA"
    assert record["tax_readiness"]["scope"] == "all_supplementary_tax_fields"
    assert event_readiness(record, FETCHED, purpose="adjustment")["state"] == "READY"
    assert event_readiness(record, FETCHED, purpose="settlement")["state"] == "READY"


def test_old_cache_read_adds_unknowns_without_mutation_or_recollection(tmp_path):
    old = event()
    for key in (*TAX_FIELDS, "tax_facts", "tax_readiness", "tax_issues", "tax_conflicts",
                "tax_field_capabilities"):
        old.pop(key, None)
    original = deepcopy(old)
    repository = MarketRepository(tmp_path)
    repository.put("event", [old], source(old))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = MarketDataService(tmp_path).events("SH600000", knowledge_cutoff=FETCHED)
    assert result.ready
    assert result.data[0]["share_face_value"] is None
    assert result.data[0]["tax_facts"]["capital_source"]["state"] == "UNKNOWN"
    assert old == original
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_evidenced_standard_fields_are_normalized_and_provenance_preserved():
    record = normalize_event_tax_facts(evidenced(face="0.10", capital="OTHER"))
    assert record["share_face_value"] == "0.1"
    assert record["capital_source"] == "OTHER"
    assert record["tax_readiness"]["state"] == "READY"
    field = record["tax_facts"]["share_face_value"]
    assert field["sources"][0]["value"] == "0.1"
    assert field["sources"][0]["applicable_date"] == "2026-07-16"
    assert field["sources"][0]["raw_reference"] == "fixture/issuer-distribution.json"


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity", "garbled", None, True, 0.1])
def test_invalid_or_inexact_face_value_is_rejected(value):
    with pytest.raises(DataQualityError):
        normalize_event_tax_facts(evidenced(face=value))


@pytest.mark.parametrize("value", ["UNKNOWN", "EXEMPT", "all_capital", 0, None])
def test_unknown_capital_source_cannot_become_known_exemption(value):
    with pytest.raises(DataQualityError):
        normalize_event_tax_facts(evidenced(capital=value))


@pytest.mark.parametrize("missing", ["provider", "raw_reference", "source_field",
                                    "applicable_date", "event_id"])
def test_known_value_requires_complete_source_evidence(missing):
    record = evidenced()
    record["tax_facts"]["share_face_value"]["sources"][0].pop(missing)
    with pytest.raises(DataQualityError):
        normalize_event_tax_facts(record)


def test_uncited_top_level_value_rejected_not_treated_as_default():
    record = event()
    record.pop("tax_facts")
    record["share_face_value"] = "1"
    with pytest.raises(DataQualityError):
        normalize_event_tax_facts(record)


def test_unknown_is_not_zero_and_unsupported_metadata_is_not_silent():
    record = event()
    record["tax_facts"]["share_face_value"]["value"] = "0"
    with pytest.raises(DataQualityError):
        normalize_event_tax_facts(record)
    record = evidenced()
    record["tax_facts"]["share_face_value"]["sources"][0]["applicable_date"] = "2026-02-30"
    with pytest.raises(ValueError):
        normalize_event_tax_facts(record)


def test_known_source_fills_unknown_without_claiming_baostock_support():
    result = merge_events([event(), evidenced()])[0]
    assert result["share_face_value"] == "1"
    assert result["tax_readiness"]["state"] == "READY"
    assert result["tax_field_capabilities"]["baostock"]["share_face_value"] == "UNSUPPORTED"
    assert result["tax_facts"]["share_face_value"]["sources"][0]["provider"] == "issuer_fixture"


@pytest.mark.parametrize("field, second", [
    ("share_face_value", evidenced(face="0.1", provider="issuer_b")),
    ("capital_source", evidenced(capital="OTHER", provider="issuer_b")),
])
def test_tax_conflicts_remain_separate_from_cash_terms(field, second):
    first = evidenced()
    result = merge_events([first, second])[0]
    assert result[field] is None
    assert result["tax_facts"][field]["state"] == "CONFLICT"
    assert f"CONFLICT_{field.upper()}" in result["tax_conflicts"]
    assert result["tax_readiness"]["state"] == "CONFLICT"
    assert not result["conflicts"]
    assert event_readiness(result, FETCHED, purpose="settlement")["state"] == "READY"
    assert result == merge_events([second, first])[0] or (
        result["tax_facts"] == merge_events([second, first])[0]["tax_facts"])
    # Conflicted evidence is retained across later merging, not overwritten by one value.
    again = merge_events([result, first])[0]
    assert again["tax_facts"][field]["state"] == "CONFLICT"


def test_same_source_stage_tax_conflict_is_not_lost():
    first, second = evidenced(), evidenced(face="0.1")
    result = _unique([first, second])[0]
    assert result["tax_facts"]["share_face_value"]["state"] == "CONFLICT"
    assert result["status"] == "IMPLEMENTED"
    assert result["cash_per_share"] == "0.42"


def test_tax_evidence_survives_frozen_snapshot_and_knowledge_cutoff(tmp_path):
    repository = MarketRepository(tmp_path)
    reference = persist_evidenced(repository)
    service = MarketDataService(tmp_path)
    assert service.events("SH600000", knowledge_cutoff="2026-07-16T17:00:00+08:00").state == "MISSING"
    frozen = service.freeze(["SH600000"], "2026-07-16", knowledge_cutoff=FETCHED,
                            purposes=("events",))
    restored = service.read_snapshot(frozen["snapshot_id"])
    assert restored == frozen
    assert reference in str(restored)
    current = service.events("SH600000", knowledge_cutoff=FETCHED)
    assert current.data[0]["tax_facts"]["share_face_value"]["value"] == "1"


@pytest.mark.parametrize("bind, fetched", [
    (False, FETCHED), (True, "2026-07-16T19:00:00+08:00")])
def test_unbound_or_late_tax_evidence_cannot_be_ready(tmp_path, bind, fetched):
    repository = MarketRepository(tmp_path)
    persist_evidenced(repository, bind=bind, source_fetched=fetched)
    result = MarketDataService(tmp_path).events("SH600000", knowledge_cutoff=FETCHED)
    assert result.state == "CORRUPT"


def test_tax_raw_corruption_is_checked_on_snapshot_replay(tmp_path):
    repository = MarketRepository(tmp_path)
    reference = persist_evidenced(repository)
    service = MarketDataService(tmp_path)
    frozen = service.freeze(["SH600000"], "2026-07-16", knowledge_cutoff=FETCHED,
                            purposes=("events",))
    (tmp_path / reference).write_text("{}")
    with pytest.raises(CorruptData):
        service.read_snapshot(frozen["snapshot_id"])


def test_tax_fact_applicable_date_cannot_drift_from_its_event():
    record = evidenced()
    record["tax_facts"]["share_face_value"]["sources"][0]["applicable_date"] = "2026-07-17"
    with pytest.raises(DataQualityError):
        normalize_event_tax_facts(record)
