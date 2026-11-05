"""Read-only commands, explicit output and application recovery boundaries."""

import argparse
import json
from pathlib import Path

import pytest
from execution_fixtures import FIRST, advance, execution_book, proposal

from china_stock_sim.application.execution import ExecutionApplication, _error
from china_stock_sim.execution import cli
from china_stock_sim.execution.utils import to_wire
from china_stock_sim.ledger.kernel import LedgerError

assert execution_book


def app(book):
    return ExecutionApplication(book.settings, market=book.market, ledger=book,
                                clock=lambda: book.test_now[0], test_mode=True)


def args(*values):
    parser = argparse.ArgumentParser()
    cli.register_commands(parser.add_subparsers(dest='command', required=True))
    return parser.parse_args(values)


def tree(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def input_file(book, content=None):
    path = book.root / 'intent.json'
    path.write_text(content if content is not None else json.dumps(to_wire(proposal(book))))
    return path


def test_validation_cli_default_is_read_only_and_does_not_load_model(execution_book, monkeypatch):
    book = execution_book
    path = input_file(book)
    monkeypatch.setattr(cli, 'ExecutionApplication', lambda root: app(book))
    import china_stock_sim.models.settings as models
    monkeypatch.setattr(models, 'load_model_settings', lambda *a, **k: pytest.fail('no model settings'))
    before = tree(book.root)
    result, code = cli.execute(args('execution-validate', '--input', str(path)), book.root)
    assert code == 0 and result['status'] == 'VALID', result
    assert result['plan_envelope']['orders']
    assert tree(book.root) == before


def test_match_cli_default_calculates_without_booking(execution_book, monkeypatch):
    book = execution_book
    frozen = app(book).freeze(proposal(book), operation_id='cli-freeze')['plan']
    advance(book, FIRST)
    monkeypatch.setattr(cli, 'ExecutionApplication', lambda root: app(book))
    before = tree(book.root)
    result, code = cli.execute(args('execution-match', '--plan-id', frozen['plan_id']), book.root)
    assert code == 0 and result['results'][0]['state'] == 'SIMULATED_FILL', result
    assert result['submission_status'] == 'NOT_SUBMITTED'
    assert book.fills('LLM')['total'] == 0
    assert tree(book.root) == before


def test_explicit_atomic_output_is_only_report_change(execution_book, monkeypatch):
    book = execution_book
    path = input_file(book)
    monkeypatch.setattr(cli, 'ExecutionApplication', lambda root: app(book))
    before = tree(book.root)
    result, code = cli.execute(args('execution-validate', '--input', str(path),
                                   '--output', 'reports/execution/validation.json'), book.root)
    assert code == 0, result
    after = tree(book.root)
    assert set(after) - set(before) == {'reports/execution/validation.json'}
    assert all(after[k] == v for k, v in before.items())
    assert json.loads(after['reports/execution/validation.json'])['status'] == 'VALID'


@pytest.mark.parametrize('destination', ['configs/current.json', 'data/plans/frozen/replace.json',
                                       '../outside.json', 'reports/execution/output.txt'])
def test_output_cannot_overwrite_business_or_external_files(execution_book, monkeypatch, destination):
    book = execution_book
    path = input_file(book)
    monkeypatch.setattr(cli, 'ExecutionApplication', lambda root: app(book))
    before = tree(book.root)
    result, code = cli.execute(args('execution-validate', '--input', str(path),
                                   '--output', destination), book.root)
    assert code == 2 and result['status'] == 'VALID' and 'output_error' in result
    assert tree(book.root) == before


@pytest.mark.parametrize('content', ['{"account_id":"LLM","account_id":"S1"}',
                                   '{"value":NaN}', '{"value":10.1}',
                                   '{"api_key":"synthetic-secret"}', '[]', '{bad'])
def test_invalid_or_credential_json_does_not_echo_or_write(execution_book, monkeypatch, content):
    book = execution_book
    path = input_file(book, content)
    monkeypatch.setattr(cli, 'ExecutionApplication', lambda root: app(book))
    before = tree(book.root)
    result, code = cli.execute(args('execution-validate', '--input', str(path)), book.root)
    assert code == 2 and result['state'] == 'INVALID_INPUT'
    assert 'synthetic-secret' not in json.dumps(result)
    assert tree(book.root) == before


def test_atomic_output_failure_preserves_prior_report(execution_book, monkeypatch):
    book = execution_book
    path = input_file(book)
    report = book.root / 'reports/execution/latest.json'
    report.parent.mkdir(parents=True)
    report.write_text('{"previous":true}')
    monkeypatch.setattr(cli, 'ExecutionApplication', lambda root: app(book))
    monkeypatch.setattr(cli.os, 'replace', lambda *a: (_ for _ in ()).throw(OSError('disk failure')))
    before = tree(book.root)
    result, code = cli.execute(args('execution-validate', '--input', str(path),
                                   '--output', str(report)), book.root)
    assert code == 2 and result['status'] == 'VALID' and 'output_error' in result
    assert tree(book.root) == before


def test_repeated_freeze_reuses_normalized_proposal_without_new_write(execution_book):
    book = execution_book
    request = proposal(book)
    first = app(book).freeze(request, operation_id='freeze-once')
    assert first['state'] == 'APPLIED', first
    before = tree(book.root)
    second = app(book).freeze(request, operation_id='freeze-retry')
    assert second['state'] == 'ALREADY_APPLIED', second
    assert second['plan']['plan_id'] == first['plan']['plan_id']
    assert tree(book.root) == before


def test_step_receipt_failure_does_not_undo_trade_and_retry_only_repairs_step(execution_book, monkeypatch):
    book = execution_book
    plan = app(book).freeze(proposal(book), operation_id='step-plan')['plan']
    advance(book, FIRST)
    real = book.record_step_result
    monkeypatch.setattr(book, 'record_step_result', lambda *a, **k: {'state': 'IO_ERROR'})
    first = app(book).settle('LLM', plan['plan_id'], operation_id='step-first')
    assert first['completion_status'] == 'COMPLETE'
    assert first['workflow_warning']['kind'] == 'STEP_RECORD_FAILED'
    assert book.fills('LLM')['total'] == 1
    monkeypatch.setattr(book, 'record_step_result', real)
    second = app(book).settle('LLM', plan['plan_id'], operation_id='step-again')
    assert second['step_receipt']['state'] == 'APPLIED'
    assert book.fills('LLM')['total'] == 1
    before = tree(book.root)
    third = app(book).settle('LLM', plan['plan_id'], operation_id='step-third')
    assert third['step_receipt']['state'] == 'ALREADY_APPLIED'
    assert tree(book.root) == before


@pytest.mark.parametrize('damage', ['hash', 'missing', 'json', 'encoding'])
def test_original_frozen_file_damage_blocks_match(execution_book, damage):
    book = execution_book
    plan = app(book).freeze(proposal(book), operation_id='damage-plan')['plan']
    advance(book, FIRST)
    path = book.root / plan['document']['path']
    body = json.loads(path.read_text())
    body['buy_budget_fen'] += 1
    path.write_text(json.dumps(body))
    if damage == 'missing':
        path.unlink()
    elif damage == 'json':
        path.write_text('{bad')
    elif damage == 'encoding':
        path.write_bytes(b'\xff')
    before = book.settings.database.read_bytes()
    result = app(book).settle('LLM', plan['plan_id'], operation_id='damaged')
    assert result['state'] == 'INTEGRITY_ERROR', result
    assert book.settings.database.read_bytes() == before


@pytest.mark.parametrize('operation', ['', None, {'api_key': 'synthetic-secret'}])
def test_settle_bad_operation_has_structured_error(execution_book, operation):
    result = app(execution_book).settle('LLM', 'absent', operation_id=operation)
    assert result['state'] == 'INVALID_INPUT' and result['completion_status'] == 'ERROR'
    assert 'synthetic-secret' not in json.dumps(result)


def test_application_preserves_ledger_error_status_and_rejects_uncontrolled_clock(execution_book):
    assert _error(LedgerError('STALE_ACCOUNT', 'changed'))['state'] == 'STALE_ACCOUNT'
    with pytest.raises(ValueError):
        ExecutionApplication(execution_book.settings, clock=lambda: execution_book.test_now[0])


def test_cli_has_no_clock_or_commit_switch():
    for flag in ['--now', '--test-mode', '--commit', '--download']:
        with pytest.raises(SystemExit):
            args('execution-match', '--plan-id', 'some-plan', flag)


def test_root_command_routes_execution_before_model_settings(execution_book, monkeypatch, capsys):
    from china_stock_sim.cli import main
    book = execution_book
    path = input_file(book)
    monkeypatch.setattr(cli, 'ExecutionApplication', lambda root: app(book))
    before = tree(book.root)
    assert main(['--root', str(book.root), 'execution-validate', '--input', str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'VALID' and tree(book.root) == before


def test_output_symlink_cannot_escape_project(execution_book, tmp_path):
    book = execution_book
    directory = book.root / 'reports'
    directory.mkdir(exist_ok=True)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (directory / 'execution').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        cli.write_output(book.root, Path('reports/execution/test.json'), {'state': 'READY'})
    assert not list(outside.iterdir())
