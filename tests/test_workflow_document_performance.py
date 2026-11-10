"""WP02: one canonical traversal preserves the immutable document byte contract."""

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from china_stock_sim.ledger import evidence
from china_stock_sim.ledger.document_objects import document_layout, read_document_bytes
from china_stock_sim.ledger.kernel import LedgerError, canonical, digest


@dataclass
class Record:
    name: str
    amount: Decimal


@pytest.mark.parametrize('value', [
    {'汉字': '字符和换行\n', 'tuple': (1, False, None)},
    {'amount': Decimal('1.2300'), 'date': date(2026, 11, 10),
     'time': datetime(2026, 11, 10, tzinfo=timezone.utc), 'record': Record('证券', Decimal('2'))},
    {'large': ['证券资料' * 200] * 100},
])
def test_canonical_once_exact_bytes_reference_and_existing_document(tmp_path, monkeypatch, value):
    expected = canonical(value).encode('utf-8')
    expected_hash = hashlib.sha256(expected).hexdigest()
    assert expected_hash == digest(value)
    real, visits = evidence.canonical, []

    def counted(item):
        if item is value:
            visits.append(1)
        return real(item)

    monkeypatch.setattr(evidence, 'canonical', counted)
    reference = evidence.document(tmp_path, 'fixture', value)
    path = tmp_path / reference['path']
    original = path.read_bytes()
    identity = path.stat()
    assert reference == {'path': f'data/plans/fixture/{expected_hash}.json',
                         'content_hash': expected_hash}
    layout = document_layout(tmp_path, reference['path'], expected_hash)
    assert read_document_bytes(tmp_path, layout, max_bytes=128 * 1024 * 1024) == expected + b'\n'
    if len(expected) + 1 > 64 * 1024:
        assert json.loads(original)['logical_content_hash'] == expected_hash
    assert evidence.document(tmp_path, 'fixture', value) == reference
    assert path.read_bytes() == original
    assert (path.stat().st_ino, path.stat().st_mtime_ns) == (identity.st_ino, identity.st_mtime_ns)
    assert visits == [1, 1]


@pytest.mark.parametrize('large', [False, True])
def test_document_existing_corruption_is_not_replaced(tmp_path, large):
    value = {'body': '内容' * (40_000 if large else 1)}
    reference = evidence.document(tmp_path, 'fixture', value)
    path = tmp_path / reference['path']
    wrapper = json.loads(path.read_bytes())
    if large:
        path = tmp_path / wrapper['object_ref']['_evidence_ref']['path']
        path.chmod(0o600)
    raw = bytearray(path.read_bytes())
    raw[len(raw) // 2] ^= 1
    path.write_bytes(raw)
    with pytest.raises((LedgerError, ValueError)):
        evidence.document(tmp_path, 'fixture', value)
    assert path.read_bytes() == raw


@pytest.mark.parametrize('value', [
    {'float': 1.5}, {'float': float('nan')}, {'number': Decimal('Infinity')},
    {'api_key': 'private'}, {'authorization': 'private'}, {'password': 'private'},
    {'text': 'Bearer private-fixture-value'},
])
def test_invalid_or_credential_documents_are_rejected_before_write(tmp_path, value):
    with pytest.raises((LedgerError, ValueError)):
        evidence.document(tmp_path, 'fixture', value)
    assert not list(tmp_path.rglob('*.json'))


def test_atomic_write_failure_does_not_leave_target_or_temporary(tmp_path, monkeypatch):
    def failed(*args):
        raise OSError('isolated replace failure')

    monkeypatch.setattr(evidence.os, 'replace', failed)
    with pytest.raises(OSError, match='isolated replace failure'):
        evidence.document(tmp_path, 'fixture', {'body': 'small'})
    assert not [path for path in tmp_path.rglob('*') if path.is_file()]
