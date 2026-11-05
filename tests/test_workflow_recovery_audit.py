"""Independent archive, report-commit and clock-boundary recovery checks."""

import json
import sqlite3
from datetime import datetime

import pytest
from execution_fixtures import FIRST, advance, execution_book
from test_ledger_kernel import legacy
from test_ledger_strategy_migration import NOW, schema2, service
from test_strategy_acceptance import strategy_world, tree_state
from test_workflow_application import application

from china_stock_sim.ledger.kernel import ledger_lock
from china_stock_sim.ledger.maintenance import file_hash
from china_stock_sim.ledger.schema import SCHEMA_VERSION
from china_stock_sim.ledger.store import initialize
from china_stock_sim.market.contracts import DataResult

assert execution_book and strategy_world


@pytest.mark.parametrize('version', [1, 2, 3, SCHEMA_VERSION])
def test_verify_backup_uses_own_configuration_and_is_zero_write(settings, version):
    if version == 1:
        legacy(settings)
    elif version == 2:
        schema2(settings, extra=True, evidence=True)
    else:
        initialize(settings, recorded_at=NOW)
        if version == 3:
            with sqlite3.connect(settings.database) as db:
                db.execute('PRAGMA user_version=3')
    book = service(settings)
    with ledger_lock(settings.root):
        backup = book._backup_locked('migration', legacy=True)
    assert backup['state'] == 'COMPLETE'
    (settings.root / 'configs/default.toml').write_text('invalid = [')
    before = tree_state(settings.root)
    result = book.verify_backup(backup['backup_id'])
    assert result['state'] == 'READY' and result['schema_version'] == version, result
    assert result['read_only'] is True
    assert tree_state(settings.root) == before


@pytest.mark.parametrize('damage', [
    'missing_manifest', 'database_hash', 'configuration_hash', 'evidence_hash',
    'omitted_evidence', 'policy_index', 'balances', 'schema',
])
def test_verify_backup_rejects_archive_damage_without_repair(settings, damage):
    initialize(settings, recorded_at=NOW)
    book = service(settings)
    backup = book.backup(label='manual')
    folder = settings.root / 'data/backups/ledger' / backup['backup_id']
    manifest_path = folder / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    if damage == 'missing_manifest':
        manifest_path.unlink()
    elif damage == 'database_hash':
        with (folder / 'ledger.sqlite3').open('ab') as stream:
            stream.write(b'corrupt')
    elif damage == 'configuration_hash':
        (folder / 'configuration/configs/default.toml').write_text('unexpected = true')
    elif damage == 'evidence_hash':
        (folder / 'evidence' / manifest['evidence']['files'][0]['path']).write_text('{}')
    else:
        if damage == 'omitted_evidence':
            manifest['evidence']['files'].pop()
        elif damage == 'policy_index':
            with sqlite3.connect(folder / 'ledger.sqlite3') as db:
                db.execute("DELETE FROM account_policies WHERE account_id='S1'")
            manifest['database_sha256'] = file_hash(folder / 'ledger.sqlite3')
        elif damage == 'balances':
            manifest['balances_fen']['S1'] += 1
        else:
            manifest['schema_version'] = 2
        manifest_path.write_text(json.dumps(manifest))
    before = tree_state(settings.root)
    assert book.verify_backup(backup['backup_id'])['state'] == 'INTEGRITY_ERROR'
    assert tree_state(settings.root) == before


def test_verify_backup_distinguishes_partial_and_missing_without_creating_tree(settings):
    book = service(settings)
    before = tree_state(settings.root)
    for identity in ('daily-missing', '..', '../outside', ''):
        assert book.verify_backup(identity)['state'] == 'INTEGRITY_ERROR'
    assert tree_state(settings.root) == before
    initialize(settings, recorded_at=NOW)
    backup = book.backup(label='manual')
    path = settings.root / backup['manifest']
    manifest = json.loads(path.read_text())
    manifest['state'] = 'PARTIAL'
    manifest['evidence']['state'] = 'PARTIAL'
    manifest['evidence']['issues'] = [{'kind': 'ISOLATED_INCOMPLETE_ARCHIVE'}]
    path.write_text(json.dumps(manifest))
    before = tree_state(settings.root)
    assert book.verify_backup(backup['backup_id'])['state'] == 'PARTIAL'
    assert tree_state(settings.root) == before


def test_workflow_crossing_1810_does_not_skip_newly_due_day_to_plan(strategy_world):
    book, app, model = application(strategy_world)
    book.test_now[0] = datetime.fromisoformat(FIRST + 'T18:09:59+08:00')
    original = app._report

    def cross_day(**kwargs):
        book.test_now[0] = datetime.fromisoformat(FIRST + 'T18:10:00+08:00')
        return original(**kwargs)

    app._report = cross_day
    result = app.run()
    assert result['state'] == 'PARTIAL'
    assert any(row['kind'] == 'ACCOUNT_RECOVERY_PENDING' for row in result['reasons'])
    assert book.daily_statement('S1', FIRST) is None
    assert book.get_plan('S1', target_session='2026-11-02') is None
    assert model.calls == []
    app._report = original
    for value in book.settings.raw['strategies'].values():
        value['enabled'] = False
    assert app.run()['state'] == 'COMPLETE'
    assert book.daily_statement('S1', FIRST)['accounting_status'] == 'COMPLETE'


def test_calendar_becoming_unavailable_before_planning_is_not_complete(strategy_world, monkeypatch):
    book, app, model = application(strategy_world)
    original_report, original_calendar = app._report, book.market.calendar
    unavailable = []

    def calendar(*args, **kwargs):
        if unavailable:
            return DataResult('WAITING_DATA', [], ({'kind': 'ISOLATED_CALENDAR_LOSS'},))
        return original_calendar(*args, **kwargs)

    def finish_report(**kwargs):
        result = original_report(**kwargs)
        unavailable.append(True)
        return result

    monkeypatch.setattr(book.market, 'calendar', calendar)
    monkeypatch.setattr(app, '_report', finish_report)
    result = app.run()
    assert result['state'] == 'PARTIAL', result
    assert any(row['step'] == 'planning-calendar' and row['state'] == 'WAITING_CALENDAR'
               for row in result['steps'])
    assert model.calls == []
    for account in ('S1', 'S1-LT', 'LLM'):
        assert book.get_plan(account, target_session=FIRST) is None


def test_published_report_ack_interruption_recovers_without_replanning(strategy_world, monkeypatch):
    book, app, model = application(strategy_world)
    original = book.record_step_result
    interrupted = []

    def lose_ack(account, payload, **kwargs):
        if payload.get('step') == 'report' and book.get_plan('LLM', target_session=FIRST):
            interrupted.append(account)
            raise KeyboardInterrupt('isolated crash after atomic report publication')
        return original(account, payload, **kwargs)

    monkeypatch.setattr(book, 'record_step_result', lose_ack)
    with pytest.raises(KeyboardInterrupt):
        app.run()
    assert interrupted and book.settings.report.is_file()
    assert book.pending_work(interrupted[0])['report_pending']
    plans = {account: book.get_plan(account, target_session=FIRST)['plan_id']
             for account in ('S1', 'S1-LT', 'LLM')}
    assert len(model.calls) == 1
    monkeypatch.setattr(book, 'record_step_result', original)
    assert app.run()['state'] == 'COMPLETE'
    assert len(model.calls) == 1
    for account, identity in plans.items():
        assert book.get_plan(account, target_session=FIRST)['plan_id'] == identity
        assert not book.pending_work(account)['report_pending']
    assert any(row['state'] == 'INTERRUPTED' for row in app.journal.read()['runs'].values())


def test_workflow_replaces_missing_complete_backup_without_financial_replay(execution_book):
    from test_workflow_acceptance import offline_application

    book = execution_book
    advance(book, FIRST)
    app = offline_application(book)
    assert app.run()['state'] == 'COMPLETE'
    folder = book.root / 'data/backups/ledger'
    original = list(folder.glob('daily-*/manifest.json'))
    assert len(original) == 1
    original[0].unlink()
    database = book.settings.database.read_bytes()
    result = app.run()
    assert result['state'] == 'COMPLETE', result
    assert book.settings.database.read_bytes() == database
    replacement = list(folder.glob('daily-*/manifest.json'))
    assert len(replacement) == 1 and replacement[0] != original[0]
    manifest = json.loads(replacement[0].read_text())
    assert book.verify_backup(manifest['backup_id'])['state'] == 'READY'
