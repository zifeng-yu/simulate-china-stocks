"""Update-local raw proofs retain complete dependency and index guards."""

import os
from contextvars import copy_context
from copy import deepcopy
from threading import Event, Thread

import pytest
from test_market_service import populate, source

from china_stock_sim.market import raw_validation
from china_stock_sim.market.raw_validation import RawValidationScope
from china_stock_sim.market.repository import CorruptData, MarketRepository
from china_stock_sim.market.service import MarketDataService


def tree(repository):
    leaf_body = source([{"value": "original"}])
    leaf = repository.save_raw(leaf_body)
    middle = repository.save_raw({**source([]), "dependencies": [leaf]})
    top = repository.save_raw({**source([]), "dependencies": [middle]})
    return top, middle, leaf, leaf_body


def test_shared_scope_verifies_each_raw_once_and_does_not_leak_mutable_values(tmp_path):
    repository, other = MarketRepository(tmp_path), MarketRepository(tmp_path)
    top, _, leaf, leaf_body = tree(repository)
    with repository.validation_scope() as scope, other.validation_scope(scope):
        for _ in range(3):
            repository.validate_raw(top)
            value = other.read_validated_raw(leaf)
            value["rows"][0]["value"] = "changed by consumer"
            assert repository.read_validated_raw(leaf) == leaf_body
            assert repository.save_raw(deepcopy(leaf_body)) == leaf
        assert scope.metrics["full_checks"] == 3
        assert scope.metrics["cache_hits"] >= 10
    assert scope.closed and not scope.entries and scope.bytes == 0
    assert repository._raw_validation is other._raw_validation is None


@pytest.mark.parametrize("target", ["top", "middle", "leaf"])
@pytest.mark.parametrize("change", ["missing", "same_size_mtime_restored", "replace_inode"])
def test_complete_closure_changes_never_reuse_success(tmp_path, target, change):
    repository = MarketRepository(tmp_path)
    top, middle, leaf, _ = tree(repository)
    path = tmp_path / {"top": top, "middle": middle, "leaf": leaf}[target]
    with repository.validation_scope() as scope:
        repository.validate_raw(top)
        original, stamp = path.read_bytes(), path.stat()
        if change == "missing":
            path.unlink()
        elif change == "same_size_mtime_restored":
            path.write_bytes(b"x" + original[1:])
            os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        else:
            replacement = path.with_suffix(".replacement")
            replacement.write_bytes(original)
            os.utime(replacement, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            replacement.replace(path)
        if change == "replace_inode":
            repository.validate_raw(top)
            assert scope.metrics["full_checks"] > 3
        else:
            with pytest.raises(CorruptData):
                repository.validate_raw(top)
        assert scope.metrics["invalidated"] > 0


def test_lexical_path_changed_to_link_cannot_reuse_old_resolved_proof(tmp_path):
    repository = MarketRepository(tmp_path)
    top, _, leaf, _ = tree(repository)
    path = tmp_path / leaf
    with repository.validation_scope() as scope:
        repository.validate_raw(top)
        target = path.with_name("different-content.json")
        target.write_text('{"wrong":"source"}')
        path.unlink()
        path.symlink_to(target.name)
        with pytest.raises(CorruptData):
            repository.validate_raw(top)
        assert scope.metrics["invalidated"] > 0


def test_change_during_read_never_publishes_proof(tmp_path, monkeypatch):
    repository = MarketRepository(tmp_path)
    reference = repository.save_raw(source([{"value": "original"}]))
    original = raw_validation.read_json

    def changing(path):
        result = original(path)
        path.write_text("changed while reading")
        return result

    with repository.validation_scope() as scope:
        monkeypatch.setattr(raw_validation, "read_json", changing)
        with pytest.raises(CorruptData):
            repository.validate_raw(reference)
        assert reference not in scope.entries


@pytest.mark.parametrize("max_entries,max_bytes", [(0, 1024), (10, 1), (1, 1024 * 1024)])
def test_capacity_is_only_a_performance_limit(tmp_path, max_entries, max_bytes):
    repository = MarketRepository(tmp_path)
    top, _, _, _ = tree(repository)
    scope = RawValidationScope(tmp_path, max_entries=max_entries, max_bytes=max_bytes)
    with repository.validation_scope(scope):
        repository.validate_raw(top)
        first = repository.read_validated_raw(top)
        assert repository.read_validated_raw(top) == first
    assert len(scope.entries) <= max_entries and scope.bytes <= max_bytes
    scope.close()


@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt])
def test_scope_cleanup_on_failure_and_fresh_next_scope(tmp_path, error):
    repository = MarketRepository(tmp_path)
    top, _, _, _ = tree(repository)
    with pytest.raises(error):
        with repository.validation_scope() as scope:
            repository.validate_raw(top)
            raise error()
    assert scope.closed and not scope.entries and repository._raw_validation is None
    with repository.validation_scope() as fresh:
        repository.validate_raw(top)
        assert fresh.metrics["full_checks"] == 3
    with pytest.raises(ValueError, match="生命周期"):
        with repository.validation_scope(scope):
            pass


def test_shared_scope_rejects_other_root_and_nested_scope_preserves_owner(tmp_path):
    repository = MarketRepository(tmp_path / "first")
    other = MarketRepository(tmp_path / "second")
    with repository.validation_scope() as scope:
        with repository.validation_scope() as nested:
            assert nested is scope
        assert not scope.closed
        with pytest.raises(ValueError, match="项目"):
            with other.validation_scope(scope):
                pass
        assert other._raw_validation is None


def test_mutable_save_input_does_not_inherit_prior_digest(tmp_path):
    repository = MarketRepository(tmp_path)
    body = source([{"value": "original"}])
    first = repository.save_raw(body)
    with repository.validation_scope():
        repository.validate_raw(first)
        body["rows"][0]["value"] = "new value"
        second = repository.save_raw(body)
        assert second != first
        assert repository.read_validated_raw(first)["rows"][0]["value"] == "original"
        assert repository.read_validated_raw(second) == body


def test_warm_raw_proof_does_not_cache_index_checksum_or_cutoff(tmp_path):
    repository = MarketRepository(tmp_path)
    identities = populate(repository)
    service = MarketDataService(tmp_path)
    query = dict(code="SH600000", start="2024-02-08", end="2024-02-08")
    with repository.validation_scope() as scope, service.repository.validation_scope(scope):
        current = service.prices(**query, knowledge_cutoff="2024-02-08T20:00:00+08:00")
        assert current.ready
        early = service.prices(**query, knowledge_cutoff="2024-02-08T10:00:00+08:00")
        assert not early.ready
        with repository.connection(write=True) as connection:
            connection.execute("UPDATE observations SET checksum=? WHERE id=?",
                               ("damaged", identities[0]))
        assert service.prices(**query, knowledge_cutoff="2024-02-08T20:00:00+08:00").state == "CORRUPT"


def test_new_observation_during_scope_remains_visible_without_rewriting_old_cutoff(tmp_path):
    repository = MarketRepository(tmp_path)
    row = {"code": "SH600000", "date": "2024-02-08", "name": "old"}
    first = source([], fetched="2024-02-08T18:30:00+08:00")
    repository.put("security", [row], first)
    with repository.validation_scope() as scope:
        old = repository.select("security", cutoff="2024-02-08T19:00:00+08:00")
        repository.put("security", [{**row, "name": "new"}],
                       source([], fetched="2024-02-08T20:00:00+08:00"))
        assert repository.select("security", cutoff="2024-02-08T19:00:00+08:00") == old
        assert repository.select("security", cutoff="2024-02-08T21:00:00+08:00")[0]["payload"]["name"] == "new"
        assert scope.metrics["full_checks"] == 2


def test_same_repository_ordinary_reader_thread_does_not_join_or_close_update_scope(tmp_path):
    repository = MarketRepository(tmp_path)
    reference = repository.save_raw(source([{"value": "original"}]))
    ready, proceed = Event(), Event()
    findings = []

    def ordinary_reader():
        try:
            findings.append(repository._raw_validation)
            findings.append(repository.read_validated_raw(reference))
            # A separate explicit scope in this thread is also independently
            # owned, rather than nesting under another thread's update.
            with repository.validation_scope() as own:
                repository.validate_raw(reference)
                ready.set()
                assert proceed.wait(5)
                repository.validate_raw(reference)
                findings.append(own.closed)
            findings.append(repository._raw_validation)
        except BaseException as error:
            findings.append(error)
            ready.set()

    with repository.validation_scope() as owner:
        repository.validate_raw(reference)
        thread = Thread(target=ordinary_reader)
        thread.start()
        assert ready.wait(5)
        assert owner.metrics["full_checks"] == 1 and not owner.closed
        assert repository._raw_validation is owner
    assert owner.closed
    proceed.set()
    thread.join(5)
    assert not thread.is_alive()
    assert findings == [None, source([{"value": "original"}]), False, None]


def test_context_copied_before_scope_exit_falls_back_to_full_read_after_exit(tmp_path):
    repository = MarketRepository(tmp_path)
    reference = repository.save_raw(source([]))
    with repository.validation_scope():
        repository.validate_raw(reference)
        context = copy_context()
    assert context.run(repository.read_validated_raw, reference) == source([])


def test_scope_cannot_be_explicitly_shared_with_another_thread(tmp_path):
    repository = MarketRepository(tmp_path)
    findings = []
    with repository.validation_scope() as owner:
        def share():
            try:
                with repository.validation_scope(owner):
                    findings.append("wrong thread accepted")
            except ValueError:
                findings.append("rejected")
        thread = Thread(target=share)
        thread.start()
        thread.join(5)
        assert not thread.is_alive() and not owner.closed
    assert findings == ["rejected"]
