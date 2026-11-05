"""SI01-C1/C2: real persistent accounts are authoritative and queries are read-only."""

import json
import sqlite3
from contextlib import closing

import pytest

from china_stock_sim.config import MAX_CASH_FEN, load_settings
from china_stock_sim.ledger import LedgerService
from china_stock_sim.ledger.kernel import LedgerError
from china_stock_sim.ledger.store import initialize, read_snapshot


def configure(project, seeds, *, default='LLM'):
    path = project / 'configs/default.toml'
    text = path.read_text()
    start, end = text.index('[[accounts]]'), text.index('[market]')
    account_text = ''.join(
        '[[accounts]]\n' + ''.join(f'{key} = {json.dumps(value, ensure_ascii=False)}\n'
                                 for key, value in account.items()) + '\n'
        for account in seeds)
    text = text[:start] + account_text + text[end:]
    text = text.replace('default_account = "LLM"', f'default_account = {json.dumps(default)}')
    path.write_text(text)
    return load_settings(project)


def seed(identity, principal=5_000_000, strategy='S1'):
    return {'id': identity, 'strategy': strategy, 'initial_cash_fen': principal}


def contents(project):
    return {str(path.relative_to(project)): path.read_bytes()
            for path in project.rglob('*') if path.is_file()}


class NoMarket:
    def __getattr__(self, name):
        raise AssertionError(f'registry query must not access market: {name}')


def test_multiple_same_strategy_accounts_keep_real_independent_principal(project):
    settings = configure(project, [seed('Z', 250_000), seed('A', 8_000_000),
                                   seed('M', 1, 'future-strategy'), seed('LLM', 5_000_000, 'LLM-Daily')])
    assert initialize(settings)['created_accounts'] == 4
    before = contents(project)
    service = LedgerService(settings, market=NoMarket())
    rows = service.list_accounts()
    assert [row['account_id'] for row in rows] == ['A', 'LLM', 'M', 'Z']
    assert {row['account_id']: row['cash_fen'] for row in rows} == {
        'A': 8_000_000, 'LLM': 5_000_000, 'M': 1, 'Z': 250_000}
    assert rows[0]['strategy_id'] == rows[-1]['strategy_id'] == 'S1'
    assert all(row['initial_cash_fen'] == row['cash_fen'] and row['opening_entries'] == 1 for row in rows)
    assert all(row['registration_source'] == 'ledger' and row['display_name'] == row['account_id']
               and row['simulation_start_session'] is None and row['account_revision'] == 0 for row in rows)
    assert all('strategy_version' not in row and 'account_policy_id' not in row for row in rows)
    assert service.get_account_profile('Z') == rows[-1]
    rows[-1]['cash_fen'] = 0
    assert service.get_account_profile('Z')['cash_fen'] == 250_000
    assert contents(project) == before


def test_existing_registry_does_not_change_with_seed_config(project):
    settings = configure(project, [seed('A', 1_000), seed('B', 2_000), seed('C', 3_000), seed('D', 4_000)])
    initialize(settings)
    original = read_snapshot(settings)
    settings = configure(project, [seed('A', 999_999, 'another-strategy'), seed('NEW')], default='absent')
    before = contents(project)
    assert settings.default_account == 'absent'
    assert initialize(settings)['created_accounts'] == 0
    assert read_snapshot(settings) == original
    service = LedgerService(settings, market=NoMarket())
    assert [row['account_id'] for row in service.list_accounts()] == ['A', 'B', 'C', 'D']
    assert service.get_account_profile('A')['initial_cash_fen'] == 1_000
    assert service.get_account_profile('A')['strategy_id'] == 'S1'
    with pytest.raises(LedgerError, match='未知账户') as error:
        service.get_account_profile('NEW')
    assert error.value.state == 'INVALID_INPUT'
    assert contents(project) == before


@pytest.mark.parametrize('identity', ['../reports/<script>"\n测试', 'a' * 256])
def test_opaque_account_ids_are_stored_without_becoming_paths(project, identity):
    settings = configure(project, [seed(identity)], default='not-present')
    initialize(settings)
    before = contents(project)
    assert LedgerService(settings, market=NoMarket()).get_account_profile(identity)['account_id'] == identity
    assert contents(project) == before


@pytest.mark.parametrize('seeds', [
    [], [seed('')], [seed(' ')], [seed('a' * 257)], [seed(1)], [seed(True)],
    [seed('A'), seed('A')], [seed('A', 0)], [seed('A', -1)], [seed('A', True)],
    [seed('A', 1.1)], [seed('A', MAX_CASH_FEN + 1)], [seed('A', strategy='')],
    [seed('A', strategy=False)], [seed('A', strategy='x' * 257)],
])
def test_invalid_seed_config_is_rejected_without_creating_runtime_data(project, seeds):
    with pytest.raises(ValueError):
        configure(project, seeds)
    assert not (project / 'data').exists()


def test_sqlite_maximum_principal_is_supported_without_precision_loss(project):
    settings = configure(project, [seed('maximum', MAX_CASH_FEN)])
    initialize(settings)
    row = LedgerService(settings, market=NoMarket()).get_account_profile('maximum')
    assert row['initial_cash_fen'] == row['cash_fen'] == MAX_CASH_FEN


@pytest.mark.parametrize('statement,parameters', [
    ("DELETE FROM account_state WHERE account_id='S1'", ()),
    ("UPDATE account_state SET data=? WHERE account_id='S1'", ('[]',)),
    ("UPDATE account_state SET data=? WHERE account_id='S1'", ('{}',)),
    ("UPDATE account_state SET data=json_set(data,'$.account_revision',-1) WHERE account_id='S1'", ()),
    ("UPDATE account_state SET data=json_set(data,'$.simulation_start_session',3) WHERE account_id='S1'", ()),
    ("UPDATE account_state SET data=json_set(data,'$.last_processed_at','invalid') WHERE account_id='S1'", ()),
    ("UPDATE account_state SET data=json_set(data,'$.cash_fen',1) WHERE account_id='S1'", ()),
    ("UPDATE accounts SET initial_cash_fen=5000001 WHERE account_id='S1'", ()),
    ("UPDATE accounts SET strategy_id='' WHERE account_id='S1'", ()),
    ("UPDATE cash_entries SET amount_fen=1.5 WHERE account_id='S1'", ()),
])
def test_registered_account_integrity_still_checked_without_seed_equality(settings, statement, parameters):
    initialize(settings)
    with closing(sqlite3.connect(settings.database)) as db:
        db.execute(statement, parameters)
        db.commit()
    before = contents(settings.root)
    with pytest.raises(ValueError):
        LedgerService(settings, market=NoMarket()).list_accounts()
    assert contents(settings.root) == before


def test_registry_query_does_not_create_missing_database(settings):
    before = contents(settings.root)
    service = LedgerService(settings, market=NoMarket())
    with pytest.raises(FileNotFoundError):
        service.list_accounts()
    assert contents(settings.root) == before


def test_legacy_registry_query_needs_no_account_state_or_implicit_migration(settings):
    from test_ledger_kernel import legacy

    legacy(settings)
    before = contents(settings.root)
    service = LedgerService(settings, market=NoMarket())
    assert [row['account_id'] for row in service.list_accounts()] == ['LLM', 'S1', 'S1-LT']
    assert service.get_account_profile('S1')['initial_cash_fen'] == 5_000_000
    assert contents(settings.root) == before
