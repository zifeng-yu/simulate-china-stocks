"""Real source identities and explicit policy adoption; no production writes."""

import hashlib
from copy import deepcopy
from datetime import datetime

import pytest
from test_ledger_trading import DAY, Facts, call, revision

from china_stock_sim.common import strategy_definitions as definitions
from china_stock_sim.ledger import LedgerService


@pytest.fixture
def implementation_tree(tmp_path, monkeypatch):
    root = tmp_path / 'isolated_implementation'
    for relative in {p for files in definitions.IMPLEMENTATION_FILES.values() for p in files}:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# isolated source identity: ' + relative + '\n')
    monkeypatch.setattr(definitions, '_IMPLEMENTATION_ROOT', root)
    return root


def identities():
    return {name: definitions.get_definition(name)['implementation_hash']
            for name in ('S1', 'S1-LT', 'LLM-Daily')}


@pytest.mark.parametrize('strategy', ['S1', 'S1-LT', 'LLM-Daily'])
def test_builtin_identity_binds_explicit_file_bytes_and_is_deterministic(implementation_tree, strategy):
    first = definitions.get_definition(strategy)
    assert first == definitions.get_definition(strategy)
    assert first['implementation_scope'] == 'current_source_files'
    assert first['strategy_version'].endswith('-implementation-2')
    assert set(first['implementation_files']) == set(definitions.IMPLEMENTATION_FILES[strategy])
    for relative, expected in first['implementation_files'].items():
        assert hashlib.sha256((implementation_tree / relative).read_bytes()).hexdigest() == expected
    assert not any('.env' in path or path.startswith('configs/')
                   for path in first['implementation_files'])


@pytest.mark.parametrize('relative,affected', [
    ('src/china_stock_sim/strategies/rules.py', {'S1', 'S1-LT'}),
    ('src/china_stock_sim/strategies/llm_prompt.py', {'LLM-Daily'}),
    ('src/china_stock_sim/strategies/llm_call.py', {'LLM-Daily'}),
    ('src/china_stock_sim/application/strategy.py', {'S1', 'S1-LT', 'LLM-Daily'}),
    ('src/china_stock_sim/application/strategy_inputs.py', {'S1', 'S1-LT', 'LLM-Daily'}),
    ('src/china_stock_sim/common/sizing.py', {'S1', 'S1-LT', 'LLM-Daily'}),
    ('src/china_stock_sim/common/strategy_definitions.py', {'S1', 'S1-LT', 'LLM-Daily'}),
    ('src/china_stock_sim/common/strategy_transitions.py', {'S1', 'S1-LT', 'LLM-Daily'}),
])
def test_program_template_and_shared_changes_detected_without_import(
        implementation_tree, relative, affected):
    before = identities()
    # Not executable Python: computing an identity must read bytes, never import.
    (implementation_tree / relative).write_text('this cannot be imported as Python!\n')
    after = identities()
    assert {name for name in before if before[name] != after[name]} == affected


def test_unrelated_files_and_per_account_configuration_do_not_change_program_identity(
        implementation_tree, settings):
    before = definitions.get_definition('S1', settings)
    (implementation_tree / 'unrelated.txt').write_text('not an implementation dependency')
    settings.raw['strategies']['s1']['target_count'] = 8
    after = definitions.get_definition('S1', settings)
    assert before['implementation_hash'] == after['implementation_hash']
    assert before['defaults']['target_count'] == 10
    assert after['defaults']['target_count'] == 8


def test_missing_required_file_fails_closed(implementation_tree):
    (implementation_tree / 'src/china_stock_sim/strategies/rules.py').unlink()
    with pytest.raises(ValueError, match='实现文件缺失'):
        definitions.get_definition('S1')
    assert definitions.get_definition('LLM-Daily')['implementation_scope'] == 'current_source_files'


def test_source_manifest_rejects_escape_from_explicit_root(implementation_tree, tmp_path):
    source = implementation_tree / 'src/china_stock_sim/strategies/rules.py'
    outside = tmp_path / 'unrelated_source.py'
    outside.write_text('outside the registered source tree')
    source.unlink()
    source.symlink_to(outside)
    with pytest.raises(ValueError, match='路径越界'):
        definitions.get_definition('S1')


@pytest.mark.parametrize('name', ['FutureModel', 'LLM-Daily'])
def test_explicit_registered_hash_keeps_test_and_future_strategy_compatibility(
        implementation_tree, monkeypatch, name):
    definition = deepcopy(definitions.STRATEGY_DEFINITIONS['LLM-Daily'])
    definition['implementation_hash'] = 'a' * 64
    monkeypatch.setitem(definitions.STRATEGY_DEFINITIONS, name, definition)
    monkeypatch.setattr(definitions, '_implementation_files',
                        lambda *_: pytest.fail('explicit registry identity must not read builtin files'))
    actual = definitions.get_definition(name)
    assert actual['implementation_hash'] == 'a' * 64
    assert actual['implementation_scope'] == 'explicit_registered_hash'


@pytest.mark.parametrize('value', ['short', 'A' * 64, None, 5])
def test_invalid_explicit_identity_rejected(implementation_tree, monkeypatch, value):
    definition = deepcopy(definitions.STRATEGY_DEFINITIONS['S1'])
    definition['implementation_hash'] = value
    monkeypatch.setitem(definitions.STRATEGY_DEFINITIONS, 'S1', definition)
    with pytest.raises(ValueError, match='64位小写SHA256'):
        definitions.get_definition('S1')


def test_old_contract_policy_readable_but_new_planning_needs_explicit_future_policy(
        implementation_tree, settings, monkeypatch):
    import socket

    monkeypatch.setattr(socket.socket, 'connect', lambda *_: pytest.fail('no network permitted'))
    current = deepcopy(definitions.STRATEGY_DEFINITIONS['S1'])
    monkeypatch.setitem(definitions.STRATEGY_DEFINITIONS, 'S1', {
        **current, 'strategy_version': 'S1-contract-1', 'implementation_hash': 'a' * 64,
        'implementation_scope': 'registered_descriptor'})
    now = datetime.fromisoformat('2026-10-29T20:00:00+08:00')
    book = LedgerService(settings, market=Facts(), clock=lambda: now, test_mode=True)
    book.initialize()
    before_accounts = book.list_accounts()
    old = book.get_account_policy('S1', DAY)
    old_document = (book.root / old['document']['path']).read_bytes()
    monkeypatch.setitem(definitions.STRATEGY_DEFINITIONS, 'S1', current)
    before_read = settings.database.read_bytes()
    assert book.get_account_policy('S1', DAY) == old
    assert book.list_accounts() == before_accounts
    assert settings.database.read_bytes() == before_read

    assert call(book, 'activate_simulation', {'target_session': DAY}, account='S1')['state'] == 'APPLIED'

    def prepare(operation):
        return call(book, 'prepare_strategy_decision', {
            'target_session': DAY, 'market_as_of': '2026-10-29', 'input_cutoff': now.isoformat(),
            'expected_sequence': 0, 'input_bundle': {'account_revision': revision(book, 'S1'),
                'market_snapshot_ids': [], 'data_evidence': []}}, account='S1', op=operation)

    before_failed_prepare = settings.database.read_bytes()
    rejected = prepare('old-contract-must-not-plan')
    assert rejected['state'] == 'CONFLICT' and 'CONFIG_CHANGED' in rejected['reason']
    assert settings.database.read_bytes() == before_failed_prepare
    assert book.get_strategy_decision('S1', target_session=DAY) is None
    update = call(book, 'schedule_account_policy', {'effective_session': DAY,
        'reason': 'explicit adoption of current strategy implementation'}, account='S1')
    assert update['state'] == 'APPLIED'
    assert book.get_account_policy('S1', '2026-10-29') == old
    new = book.get_account_policy('S1', DAY)
    assert new['policy']['strategy_version'] == current['strategy_version']
    assert new['definition']['implementation_hash'] == definitions.get_definition('S1')['implementation_hash']
    assert new['policy_hash'] != old['policy_hash']
    assert (book.root / old['document']['path']).read_bytes() == old_document
    assert len(book.account_policies('S1')) == 2
    assert prepare('new-policy-may-plan')['state'] == 'APPLIED'
    for account in book.list_accounts():
        assert account['initial_cash_fen'] == account['cash_fen'] == 5_000_000
        assert account['opening_entries'] == 1
