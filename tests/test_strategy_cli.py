"""New CLI boundary acceptance using isolated real services and no network."""

import json

import pytest
from execution_fixtures import FIRST, execution_book
from test_strategy_acceptance import strategy_world, tree_state

from china_stock_sim.cli import main

assert execution_book and strategy_world


@pytest.fixture
def strategy_cli(strategy_world, monkeypatch):
    from china_stock_sim.strategies import cli

    book, app, model = strategy_world
    monkeypatch.setattr(cli, 'StrategyApplication', lambda settings: app())

    def run(command, *options):
        return main(['--root', str(book.root), command, *options])

    return book, app, model, run


def test_inspect_and_audit_are_read_only_with_dynamic_accounts(strategy_cli, capsys):
    book, app, model, run = strategy_cli
    before = tree_state(book.root)
    assert run('strategy-inspect', '--account', 'S1-LT', '--target-session', FIRST) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['state'] == 'READY' and result['input_summary']['required_history_sessions'] == 121
    assert run('strategy-audit', '--strategy-id', 'S1-LT') == 0
    audit = json.loads(capsys.readouterr().out)
    assert audit['accounts'] == ['S1-LT'] and audit['decisions'] == []
    assert tree_state(book.root) == before and not model.calls


def test_offline_evaluate_has_no_new_files_model_calls_or_ledger_mutations(strategy_cli, capsys):
    book, app, model, run = strategy_cli
    bundle = app().inspect('S1', FIRST, include_input=True)['input_bundle']
    path = book.root / 'reports/strategies/fixed-input.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bundle))
    before = tree_state(book.root)
    assert run('strategy-evaluate', '--input', 'reports/strategies/fixed-input.json') == 0
    outcome = json.loads(capsys.readouterr().out)
    assert outcome['state'] == 'READY' and outcome['ordered_intents']
    assert tree_state(book.root) == before and not model.calls


def test_plan_publish_and_recover_are_distinct_and_replay_has_no_model_call(strategy_cli, capsys):
    book, app, model, run = strategy_cli
    options = ('--account', 'LLM', '--target-session', FIRST)
    assert run('strategy-plan', *options) == 0
    planned = json.loads(capsys.readouterr().out)
    assert planned['state'] == 'PROPOSED'
    assert book.get_plan('LLM', target_session=FIRST) is None
    assert len(model.calls) == 1
    assert run('strategy-publish', *options) == 0
    published = json.loads(capsys.readouterr().out)
    assert published['state'] == 'FROZEN'
    assert run('strategy-recover', *options) == 0
    recovered = json.loads(capsys.readouterr().out)
    assert recovered['plan_id'] == published['plan_id'] and len(model.calls) == 1
    assert book.report_snapshot('LLM', FIRST)['fills'] == []


def test_invalid_output_path_rejected_before_explicit_plan_has_side_effects(strategy_cli, capsys):
    book, app, model, run = strategy_cli
    before = tree_state(book.root)
    assert run('strategy-publish', '--account', 'LLM', '--target-session', FIRST,
               '--output', 'configs/override.json') == 2
    assert 'reports/strategies' in capsys.readouterr().err
    assert tree_state(book.root) == before and not model.calls


def test_audit_export_preserves_identity_and_filters_revisions(strategy_cli, capsys):
    book, app, model, run = strategy_cli
    result = app().publish('S1', FIRST)
    assert result['state'] == 'FROZEN', result
    row = result['decision']
    assert run('strategy-audit', '--account', 'S1', '--strategy-version', row['strategy_version'],
        '--policy-id', row['policy_id'], '--start', FIRST, '--end', FIRST,
        '--output', 'reports/strategies/decisions.csv', '--format', 'csv') == 0
    assert json.loads(capsys.readouterr().out)['state'] == 'READY'
    content = (book.root / 'reports/strategies/decisions.csv').read_text()
    assert row['strategy_version'] in content and row['policy_id'] in content and 'FROZEN' in content
    assert run('strategy-audit', '--account', 'S1', '--strategy-version', 'unmatched') == 0
    assert json.loads(capsys.readouterr().out)['decisions'] == []


def test_unknown_account_and_secret_input_are_rejected_without_network(strategy_cli, capsys):
    book, app, model, run = strategy_cli
    before = tree_state(book.root)
    assert run('strategy-audit', '--account', 'UNKNOWN') == 2
    assert '账户不存在' in capsys.readouterr().err
    secret = book.root / '.env'
    secret.write_text('PRIVATE_FIXTURE=never-display-me\n')
    assert run('strategy-evaluate', '--input', '.env') == 2
    assert 'never-display-me' not in capsys.readouterr().err
    secret.unlink()
    assert tree_state(book.root) == before and not model.calls


def test_report_failure_does_not_erase_proposal_or_requestion_model(strategy_cli, capsys, monkeypatch):
    from china_stock_sim.strategies import cli

    book, app, model, run = strategy_cli
    planned = app().plan('LLM', FIRST)
    assert planned['state'] == 'PROPOSED'
    with monkeypatch.context() as patch:
        def fail(**kwargs):
            raise OSError('isolated report failure')

        # The ledger shares tempfile; fail read-only report output after planning.
        patch.setattr(cli.tempfile, 'mkstemp', fail)
        assert run('strategy-audit', '--account', 'LLM',
                   '--output', 'reports/strategies/failed.json') == 2
        outcome = json.loads(capsys.readouterr().out)
        assert outcome['state'] == 'READY' and outcome['report_error']['kind'] == 'REPORT_WRITE_FAILED'
    assert run('strategy-recover', '--account', 'LLM', '--target-session', FIRST) == 0
    assert json.loads(capsys.readouterr().out)['state'] == 'FROZEN'
    assert len(model.calls) == 1
