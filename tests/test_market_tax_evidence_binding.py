"""An existing, checksum-valid document is not enough to prove a tax value."""

from copy import deepcopy

import pytest
from test_market_events import FETCHED, event

from china_stock_sim.market.repository import MarketRepository
from china_stock_sim.market.service import MarketDataService

CODE = "SH600000"
DAY = "2026-07-16"


def persist(repository, *, raw_changes=None, source_changes=None, omit_raw=(), rows=None):
    record = event()
    record["provider"] = "issuer_fixture"
    row = {"code": CODE, "event_id": record["event_id"], "applicable_date": DAY,
           "FACE_VALUE": "1.00"}
    row.update(raw_changes or {})
    for key in omit_raw:
        row.pop(key, None)
    raw = {"provider": record["provider"], "fetched_at": FETCHED,
           "rows": [row] if rows is None else rows}
    reference = repository.save_raw(raw)
    source = {"provider": record["provider"], "source_field": "FACE_VALUE",
              "raw_reference": reference, "event_id": record["event_id"],
              "applicable_date": DAY, "value": "1.00", "row_index": 0}
    source.update(source_changes or {})
    record["tax_facts"]["share_face_value"] = {
        "state": "READY", "value": "1.00", "sources": [source]}
    # Canonical repository reads validate the supplied content independently of
    # the producer. Do not pre-normalize away deliberately malformed test cases.
    response = {"provider": record["provider"], "fetched_at": FETCHED,
                "dependencies": [reference], "rows": [record]}
    repository.put("event", [record], response)
    return record, reference


@pytest.fixture
def repository(tmp_path):
    return MarketRepository(tmp_path)


def test_ready_fact_matches_actual_field_identity_and_date(repository):
    _, reference = persist(repository)
    service = MarketDataService(repository.root)
    result = service.events(CODE, knowledge_cutoff=FETCHED)
    assert result.ready and result.data[0]["share_face_value"] == "1"
    fact = result.data[0]["tax_facts"]["share_face_value"]
    assert fact["state"] == "READY"
    assert fact["sources"][0]["row_index"] == 0
    assert fact["sources"][0]["raw_reference"] == reference
    snapshot = service.freeze([CODE], DAY, purposes=("events",), knowledge_cutoff=FETCHED)
    assert service.read_snapshot(snapshot["snapshot_id"]) == snapshot


@pytest.mark.parametrize("changes", [
    {"FACE_VALUE": "0.1"}, {"FACE_VALUE": "-1"}, {"FACE_VALUE": None},
    {"code": "SZ000001"}, {"event_id": "another-event"},
    {"applicable_date": "2026-07-17"}, {"applicable_date": "2026-02-30"},
])
def test_valid_object_with_wrong_tax_content_cannot_be_promoted_to_ready(repository, changes):
    persist(repository, raw_changes=changes)
    result = MarketDataService(repository.root).events(CODE, knowledge_cutoff=FETCHED)
    assert result.state == "CORRUPT" and not result.data


@pytest.mark.parametrize("missing", ["code", "event_id", "applicable_date", "FACE_VALUE"])
def test_undocumented_identity_or_value_cannot_prove_tax_facts(repository, missing):
    persist(repository, omit_raw=(missing,))
    assert MarketDataService(repository.root).events(CODE).state == "CORRUPT"


@pytest.mark.parametrize("index", [True, -1, 1, "0", None])
def test_invalid_exact_row_location_is_rejected(repository, index):
    persist(repository, source_changes={"row_index": index})
    assert MarketDataService(repository.root).events(CODE).state == "CORRUPT"


def test_producer_cannot_relabel_unrelated_event_as_matching_evidence(repository):
    persist(repository, raw_changes={"event_id": "unrelated"},
            source_changes={"event_id": "unrelated"})
    assert MarketDataService(repository.root).events(CODE).state == "CORRUPT"


def test_exact_row_selector_not_first_match_or_any_matching_value(repository):
    wanted = event()
    row = {"code": CODE, "event_id": wanted["event_id"], "applicable_date": DAY,
           "FACE_VALUE": "1.00"}
    other = {**deepcopy(row), "FACE_VALUE": "0.1"}
    persist(repository, rows=[other, row], source_changes={"row_index": 1})
    assert MarketDataService(repository.root).events(CODE).ready


def test_exact_row_with_wrong_value_cannot_use_another_row_to_pass(repository):
    wanted = event()
    row = {"code": CODE, "event_id": wanted["event_id"], "applicable_date": DAY,
           "FACE_VALUE": "1.00"}
    other = {**deepcopy(row), "FACE_VALUE": "0.1"}
    persist(repository, rows=[other, row], source_changes={"row_index": 0})
    assert MarketDataService(repository.root).events(CODE).state == "CORRUPT"


@pytest.mark.parametrize("field,claimed,actual", [
    ("capital_source", "STOCK_PREMIUM", "OTHER"),
    ("tax_basis_note", "公开说明采用面值", "实际原文另有计税说明"),
])
def test_capital_and_note_claims_also_require_matching_original_values(
        repository, field, claimed, actual):
    record = event()
    record["provider"] = "issuer_fixture"
    raw = {"provider": record["provider"], "fetched_at": FETCHED,
           "rows": [{"code": CODE, "event_id": record["event_id"], "applicable_date": DAY,
                     "source_value": actual}]}
    reference = repository.save_raw(raw)
    record["tax_facts"][field] = {"state": "READY", "value": claimed, "sources": [{
        "provider": record["provider"], "source_field": "source_value",
        "raw_reference": reference, "event_id": record["event_id"],
        "applicable_date": DAY, "value": claimed, "row_index": 0}]}
    repository.put("event", [record], {
        "provider": record["provider"], "fetched_at": FETCHED,
        "rows": [record], "dependencies": [reference]})
    assert MarketDataService(repository.root).events(CODE).state == "CORRUPT"
