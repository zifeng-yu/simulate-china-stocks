"""SI01 consumer acceptance: persisted identity, public audits and dynamic commands.

Only isolated ledgers and immutable fixture decisions are used. This proves the
consumer contract, not the pending SL01 account creation or SL02 policy registry.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from execution_fixtures import FIRST, advance, execution_book, proposal
from test_execution_decision_audit import application, freeze, publish, strategy_request, tree

from china_stock_sim.cli import main
from china_stock_sim.config import load_settings
from china_stock_sim.ledger import LedgerService

assert execution_book


def audit_module():
    from china_stock_sim.reporting import audit
    return audit


def audit(book, report=None):
    return audit_module().audit_account(book.root, report or book.report_snapshot('LLM', FIRST))


def identified_plan(book, *, empty=False):
    request, core = strategy_request(book, empty=empty)
    core['calculation']['identity'] = {
        'strategy_name': '可审计策略', 'program_revision': 'program-at-decision',
        'prompt_revision': 'prompt-at-decision', 'uses_model': True,
        'model': {'model': 'fixture-model', 'provider': 'fixture-provider',
                  'generation_parameters': {'temperature': '0.2', 'max_output_tokens': 512}},
    }
    request = publish(book, request, core)
    return freeze(book, request), request, core


@pytest.fixture(autouse=True)
def forbid_network_and_model_settings(monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('no network'))
    monkeypatch.setattr('china_stock_sim.cli.model_status',
                        lambda *a, **k: pytest.fail('no current model config'))
    monkeypatch.setattr('china_stock_sim.models.settings.load_model_settings',
                        lambda *a, **k: pytest.fail('no model credentials'))


def test_audit_uses_frozen_policy_and_model_identity_not_current_config(execution_book):
    book = execution_book
    plan, request, core = identified_plan(book)
    book.settings.raw['strategies']['llm']['model_ref'] = 'unrelated-current-profile'
    book.settings.raw['strategies']['llm']['max_holdings'] = 99
    snapshot = book.report_snapshot('LLM', FIRST)
    before = tree(book.root)
    result = audit(book, snapshot)
    row = result['plans'][0]
    identity = row['identity']
    assert result['scope'] == 'FROZEN_PLANS_ONLY' and result['state'] == 'COMPLETE'
    assert result['account_revision'] == snapshot['account_revision']
    assert result['report_revision'] == snapshot['report_revision']
    assert row['plan_id'] == plan['plan_id'] and row['audit_state'] == 'COMPLETE'
    assert identity['strategy_version'] == plan['strategy_version']
    assert identity['account_policy_id'] == core['account_policy']['policy_id']
    assert identity['account_policy_hash'] == request['decision_ref']['account_policy_hash']
    assert identity['parameters'] == core['account_policy']['parameters']
    assert identity['program_revision'] == 'program-at-decision'
    assert identity['prompt_revision'] == 'prompt-at-decision'
    assert identity['uses_model'] is True
    assert identity['model']['model'] == 'fixture-model'
    assert row['calculation']['ranking'] == core['calculation']['ranking']
    assert result['orders'][0]['strategy_identity'] == identity
    assert 'unrelated-current-profile' not in json.dumps(result)
    assert tree(book.root) == before
    identity['parameters']['lookback'] = 1
    assert audit(book)['plans'][0]['identity']['parameters']['lookback'] == 121


def test_fill_and_day_identity_come_from_their_original_plan(execution_book):
    book = execution_book
    plan, _, _ = identified_plan(book)
    advance(book, FIRST)
    assert application(book).settle('LLM', plan['plan_id'], operation_id='si01:settle')['completion_status'] == 'COMPLETE'
    before = tree(book.root)
    result = audit(book)
    identity = result['plans'][0]['identity']
    assert result['fills'] and result['fills'][0]['strategy_identity'] == identity
    assert audit_module().identity_for_session(result, FIRST) == identity
    missing = audit_module().identity_for_session(result, '2026-11-02')
    assert not missing or not missing.get('strategy_version')
    assert tree(book.root) == before


@pytest.mark.parametrize('empty', [False, True])
def test_missing_optional_metadata_is_unknown_not_inferred_from_llm_account(execution_book, empty):
    book = execution_book
    request, _ = strategy_request(book, empty=empty)
    freeze(book, request)
    result = audit(book)
    identity = result['plans'][0]['identity']
    assert result['state'] == 'COMPLETE'
    assert identity.get('uses_model') is None and identity.get('model') is None
    assert identity.get('program_revision') is None and identity.get('prompt_revision') is None
    if empty:
        assert result['orders'] == []
        assert 'fixture HOLD' in str(result['plans'][0])


def test_manual_plan_explicitly_has_no_strategy_decision(execution_book):
    book = execution_book
    plan = freeze(book, proposal(book))
    before = tree(book.root)
    result = audit(book)
    row = result['plans'][0]
    assert row['plan_id'] == plan['plan_id'] and row['audit_state'] == 'MANUAL'
    assert row['identity']['provenance_mode'] == 'MANUAL'
    assert row['identity']['decision_id'] is None
    assert row['identity']['parameters'] is None
    assert tree(book.root) == before


@pytest.mark.parametrize('damage', ['missing_core', 'missing_plan', 'missing_nested', 'corrupt_core'])
def test_corrupt_audit_original_is_partial_and_does_not_break_ledger(execution_book, damage):
    book = execution_book
    request, core = strategy_request(book, nested=True)
    plan = freeze(book, request)
    ref = (plan['document'] if damage == 'missing_plan' else
           core['calculation']['audit_attachment'] if damage == 'missing_nested' else
           request['decision_ref']['decision_core'])
    path = book.root / ref['path']
    if damage == 'corrupt_core':
        path.write_text('{"untrusted_secret":"not-for-display"}')
    else:
        path.unlink()
    before = tree(book.root)
    result = audit(book)
    assert result['state'] == 'PARTIAL'
    row = result['plans'][0]
    assert row['audit_state'] == 'AUDIT_UNAVAILABLE' and row['audit_issue']
    assert row['identity']['parameters'] is None
    assert 'not-for-display' not in json.dumps(result)
    assert book.account_state('LLM', FIRST)['cash_fen'] == 5_000_000
    assert tree(book.root) == before


def test_audit_keeps_the_supplied_transaction_snapshot(execution_book):
    book = execution_book
    snapshot = book.report_snapshot('LLM', FIRST)
    identified_plan(book)
    before = tree(book.root)
    old = audit(book, snapshot)
    assert old['plans'] == [] and old['orders'] == []
    assert old['account_revision'] == snapshot['account_revision']
    assert audit(book)['plans']
    assert tree(book.root) == before


def parse_cli(capsys, expected, arguments):
    assert main(arguments) == expected
    captured = capsys.readouterr()
    return json.loads(captured.out or captured.err)


def new_dynamic_book(project):
    path = project / 'configs/default.toml'
    source = path.read_text().replace('id = "S1"', 'id = "Z-custom"', 1)
    source = source.replace('initial_cash_fen = 5000000', 'initial_cash_fen = 7300123', 1)
    path.write_text(source)
    service = LedgerService(load_settings(project))
    service.initialize()
    return service


@pytest.mark.parametrize('command', ['account-profile', 'account-audit', 'ledger-status', 'ledger-query'])
def test_dynamic_account_cli_is_read_only_and_does_not_initialize_from_config(project, capsys, command):
    book = new_dynamic_book(project)
    # The persisted extra account remains available even if TOML returns to the default seeds.
    config = project / 'configs/default.toml'
    config.write_text(config.read_text().replace('id = "Z-custom"', 'id = "S1"', 1))
    before = tree(project)
    result = parse_cli(capsys, 0, ['--root', str(project), command, '--account', 'Z-custom'])
    assert 'Z-custom' in json.dumps(result)
    assert tree(project) == before
    assert book.settings.database.is_file()


def test_account_list_reports_registered_accounts_and_never_adds_seed(project, capsys):
    new_dynamic_book(project)
    before = tree(project)
    result = parse_cli(capsys, 0, ['--root', str(project), 'account-list'])
    assert [row['account_id'] for row in result['accounts']] == ['LLM', 'S1-LT', 'Z-custom']
    assert next(row for row in result['accounts'] if row['account_id'] == 'Z-custom')['initial_cash_fen'] == 7300123
    assert tree(project) == before


@pytest.mark.parametrize('command', ['account-profile', 'account-audit', 'ledger-status', 'ledger-query'])
def test_explicit_unknown_account_is_rejected_without_fallback(settings, capsys, command):
    LedgerService(settings).initialize()
    before = tree(settings.root)
    result = parse_cli(capsys, 2, ['--root', str(settings.root), command, '--account', 'not-registered'])
    assert result['state'] == 'INVALID_INPUT'
    assert tree(settings.root) == before


def test_default_account_fallback_is_recorded_but_does_not_change_config(settings, monkeypatch, capsys):
    service = LedgerService(settings)
    service.initialize()
    service.settings = replace(settings, default_account='absent')
    monkeypatch.setattr('china_stock_sim.ledger.cli.LedgerService', lambda root: service)
    before = tree(settings.root)
    result = parse_cli(capsys, 0, ['--root', str(settings.root), 'account-profile'])
    assert result['account']['account_id'] == 'LLM'
    assert result['account_selection']['fallback'] is True
    assert tree(settings.root) == before


def test_audit_cli_export_matches_read_only_projection(execution_book, monkeypatch, capsys):
    book = execution_book
    identified_plan(book)
    monkeypatch.setattr('china_stock_sim.ledger.cli.LedgerService', lambda root: book)
    before = tree(book.root)
    result = parse_cli(capsys, 0, ['--root', str(book.root), 'account-audit', '--account', 'LLM'])
    assert result['plans'][0]['identity']['program_revision'] == 'program-at-decision'
    assert tree(book.root) == before
    output = Path('reports/strategies/si01.json')
    exported = parse_cli(capsys, 0, ['--root', str(book.root), 'account-audit', '--account', 'LLM',
                                   '--output', str(output)])
    saved = json.loads((book.root / output).read_text())
    assert saved['plans'] == result['plans']
    assert exported['output'] == output.as_posix()
    after = tree(book.root)
    assert {key: value for key, value in after.items() if key != output.as_posix()} == before


@pytest.mark.parametrize('path', ['data/state/overwrite.json', 'reports/strategies/../../data/overwrite.json',
                                  'reports/strategies/result.txt'])
def test_audit_export_cannot_escape_its_output_directory(execution_book, path):
    book = execution_book
    before = tree(book.root)
    with pytest.raises(ValueError):
        audit_module().write_audit_output(book.root, Path(path), audit(book))
    assert tree(book.root) == before


def test_audit_export_rejects_symlink_escape(execution_book, tmp_path):
    book = execution_book
    (book.root / 'reports').mkdir(exist_ok=True)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (book.root / 'reports/strategies').symlink_to(outside, target_is_directory=True)
    before = tree(book.root)
    with pytest.raises(ValueError):
        audit_module().write_audit_output(book.root, Path('reports/strategies/leak.json'), audit(book))
    assert not list(outside.iterdir()) and tree(book.root) == before


def test_export_rejects_credentials_without_creating_output(execution_book):
    book = execution_book
    output = Path('reports/strategies/private.json')
    value = audit(book)
    value['authorization'] = 'synthetic-private-value'
    before = tree(book.root)
    with pytest.raises(ValueError):
        audit_module().write_audit_output(book.root, output, value)
    assert tree(book.root) == before


@pytest.mark.parametrize('identity', ['../outside', '/tmp/outside', '<script>alert(1)</script>', '=1+1'])
def test_account_export_filenames_do_not_interpret_user_ids(identity):
    name = audit_module().account_filename(identity)
    assert '/' not in name and '\\' not in name and '..' not in name
    assert '<' not in name and '>' not in name and not name.startswith('=')
    assert name == audit_module().account_filename(identity)


def test_dynamic_execution_entry_accepts_registered_ids_and_rejects_unknown_safely(project, capsys):
    new_dynamic_book(project)
    before = tree(project)
    # Reaching the execution service's controlled missing-plan result proves argparse
    # no longer rejects a real non-default account before registry validation.
    result = parse_cli(capsys, 2, ['--root', str(project), 'execution-match', '--account', 'Z-custom',
                                  '--plan-id', 'not-yet-created'])
    assert result.get('state', result.get('status'))
    result = parse_cli(capsys, 2, ['--root', str(project), 'execution-match', '--account', 'absent',
                                  '--plan-id', 'not-yet-created'])
    assert result['state'] == 'INVALID_INPUT'
    assert tree(project) == before


def test_audit_model_metadata_only_exposes_public_fields(execution_book):
    book = execution_book
    request, core = strategy_request(book)
    core['calculation']['identity'] = {
        'uses_model': True,
        'model': {'model': 'fixture', 'provider': 'provider',
                  'base_url': 'https://fixture-private.invalid/v1',
                  'generation_parameters': {'temperature': '0.2', 'max_output_tokens': 256,
                                            'transport_host': 'fixture-private.invalid'}},
    }
    request = publish(book, request, core)
    freeze(book, request)
    result = audit(book)
    assert result['state'] == 'COMPLETE'
    model = result['plans'][0]['identity']['model']
    assert model == {'model': 'fixture', 'provider': 'provider',
                     'generation_parameters': {'temperature': '0.2', 'max_output_tokens': 256}}
    assert 'fixture-private.invalid' not in json.dumps(result)


@pytest.mark.parametrize('uses_model', [False, None])
def test_model_metadata_does_not_enable_model_display_without_frozen_capability(execution_book, uses_model):
    book = execution_book
    request, core = strategy_request(book)
    core['calculation']['identity'] = {'model': {'model': 'must-not-infer', 'provider': 'fixture'}}
    if uses_model is not None:
        core['calculation']['identity']['uses_model'] = uses_model
    request = publish(book, request, core)
    freeze(book, request)
    result = audit(book)
    assert result['state'] == 'COMPLETE'
    assert result['plans'][0]['identity']['model'] is None
    assert 'must-not-infer' not in json.dumps(result)


def test_broken_manual_original_is_not_reported_as_a_complete_manual_audit(execution_book):
    book = execution_book
    plan = freeze(book, proposal(book))
    (book.root / plan['document']['path']).unlink()
    result = audit(book)
    assert result['state'] == 'PARTIAL'
    assert result['plans'][0]['audit_state'] == 'AUDIT_UNAVAILABLE'


def test_no_plan_record_is_visible_without_inventing_a_decision(execution_book):
    from test_ledger_trading import call
    book = execution_book
    assert call(book, 'record_no_plan', {'target_session': FIRST, 'reason': 'MODEL_FAILED'})['state'] == 'APPLIED'
    before = tree(book.root)
    result = audit(book)
    assert result['plans'] == [] and result['orders'] == []
    assert result['no_plan_records'] and 'MODEL_FAILED' in str(result['no_plan_records'])
    assert tree(book.root) == before


def test_read_commands_on_missing_ledger_do_not_create_it(project, capsys):
    before = tree(project)
    for command in ('account-list', 'account-profile', 'account-audit'):
        result = parse_cli(capsys, 2, ['--root', str(project), command])
        assert result['state'] == 'ERROR'
        assert tree(project) == before


def test_two_frozen_days_keep_independent_policy_and_revision_labels(execution_book):
    from copy import deepcopy

    from china_stock_sim.execution.utils import to_wire
    from china_stock_sim.execution.validation import normalize_proposal

    book = execution_book
    first, _, original_core = identified_plan(book, empty=True)
    advance(book, FIRST)
    from test_ledger_trading import call
    assert call(book, 'close_session', {'session': FIRST, 'knowledge_cutoff': book._now().isoformat()})['state'] == 'APPLIED'
    later = '2026-11-02'
    request = to_wire(normalize_proposal(proposal(book, target=later, intents=[],
                                                no_order_reason='later HOLD')))
    request['provenance_mode'] = 'STRATEGY'
    request['strategy_version'] = 'later-strategy-revision'
    core = deepcopy(original_core)
    from test_execution_decision_audit import prepare_registered, register_proposed

    from china_stock_sim.common.strategy_definitions import STRATEGY_DEFINITIONS
    from china_stock_sim.ledger.strategy_decisions import decision_identity
    definition = deepcopy(STRATEGY_DEFINITIONS['LLM-Daily'])
    definition['strategy_version'] = request['strategy_version']
    book.test_monkeypatch.setitem(STRATEGY_DEFINITIONS, 'LLM-Daily', definition)
    policy_result = call(book, 'schedule_account_policy', {'effective_session': later,
        'strategy_parameters': {'lookback': 200, 'max_symbols': 3}, 'reason': 'future fixture revision'})
    assert policy_result['state'] == 'APPLIED', policy_result
    policy = book.get_account_policy('LLM', later)
    core['decision_id'] = decision_identity('LLM', later)
    core['proposal'] = {key: value for key, value in request.items() if key != 'decision_ref'}
    core['account_policy'] = policy['policy']
    core['strategy_implementation_hash'] = policy['definition']['implementation_hash']
    core['input_bundle'] = {'market_snapshot_ids': [], 'data_evidence': [],
                            'account_revision': request['account_revision']}
    core['calculation']['identity']['program_revision'] = 'later-program'
    prepare_registered(book, request, core)
    request = publish(book, request, core)
    register_proposed(book, request)
    result = application(book).freeze(request, operation_id='si01:later-plan')
    assert result['state'] == 'APPLIED', json.dumps(result, ensure_ascii=False)
    result = audit(book)
    assert len(result['plans']) == 2
    earlier = audit_module().identity_for_session(result, FIRST)
    latest = audit_module().identity_for_session(result, later)
    assert earlier['strategy_version'] == first['strategy_version']
    assert earlier['parameters']['lookback'] == 121
    assert earlier['program_revision'] == 'program-at-decision'
    assert latest['strategy_version'] == 'later-strategy-revision'
    assert latest['parameters']['lookback'] == 200
    assert latest['program_revision'] == 'later-program'


def test_audit_export_unicode_survives_local_viewer_encoding_detection(execution_book):
    book = execution_book
    identified_plan(book)
    result = audit(book)
    result['account']['display_name'] = '中文账户🧾 "引号" \\路径'
    result['plans'][0]['calculation']['中文依据'] = ['持有𠀀', '换行\n制表\t', 2**63 - 1]
    original = json.loads(json.dumps(result))
    before = tree(book.root)
    relative = audit_module().write_audit_output(book.root, 'reports/strategies/unicode.json', result)
    raw = (book.root / relative).read_bytes()
    assert raw.isascii() and not raw.startswith(b'\xef\xbb\xbf')
    for encoding in ('utf-8', 'gb18030', 'cp1252'):
        assert json.loads(raw.decode(encoding)) == original
    assert result == original
    after = tree(book.root)
    after.pop(relative)
    assert after == before


def test_audit_cli_failed_publication_preserves_calculation_and_old_output(execution_book, monkeypatch, capsys):
    book = execution_book
    identified_plan(book)
    monkeypatch.setattr('china_stock_sim.ledger.cli.LedgerService', lambda root: book)
    output = book.root / 'reports/strategies/audit.json'
    output.parent.mkdir(parents=True)
    output.write_text('{"previous":true}')
    monkeypatch.setattr(audit_module(), 'atomic_text',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('disk full')))
    before = tree(book.root)
    result = parse_cli(capsys, 2, ['--root', str(book.root), 'account-audit', '--account', 'LLM',
                                  '--output', str(output)])
    assert result['state'] == 'COMPLETE' and result['plans'][0]['audit_state'] == 'COMPLETE'
    assert result['output_error']['state'] == 'REPORT_ERROR'
    assert tree(book.root) == before


def test_audit_cli_partial_evidence_has_nonzero_status_but_readable_record(execution_book, monkeypatch, capsys):
    book = execution_book
    _, request, _ = identified_plan(book)
    (book.root / request['decision_ref']['decision_core']['path']).unlink()
    monkeypatch.setattr('china_stock_sim.ledger.cli.LedgerService', lambda root: book)
    before = tree(book.root)
    result = parse_cli(capsys, 2, ['--root', str(book.root), 'account-audit', '--account', 'LLM'])
    assert result['state'] == 'PARTIAL' and result['plans'][0]['audit_state'] == 'AUDIT_UNAVAILABLE'
    assert tree(book.root) == before


@pytest.mark.parametrize('location', ['calculation', 'input_bundle', 'nested_document'])
def test_public_audit_omits_transport_and_authentication_fields_from_all_evidence(execution_book, location):
    from china_stock_sim.ledger.evidence import document

    book = execution_book
    private = {
        'client_secret': 'synthetic-private-client-value',
        'access-token': 'synthetic-private-access-value',
        'refresh_token': 'synthetic-private-refresh-value',
        'cookie': 'synthetic-private-cookie-value',
        'Set-Cookie': 'synthetic-private-session-value',
        'host': 'synthetic-private-host.invalid',
        'hostname': 'synthetic-private-name.invalid',
    }
    payload = {'public_reason': 'retain this decision reason', 'transport': private}
    request, core = strategy_request(book, input_extra=payload if location == 'input_bundle' else None)
    if location == 'nested_document':
        core['calculation']['audit_attachment'] = document(book.root, 'strategy-input', payload)
    else:
        core[location]['extra_audit'] = payload
    request = publish(book, request, core)
    freeze(book, request)
    before = tree(book.root)
    result = audit(book)
    assert result['state'] == 'COMPLETE'
    encoded = json.dumps(result, ensure_ascii=False)
    assert 'retain this decision reason' in encoded
    assert 'synthetic-private-' not in encoded
    assert tree(book.root) == before


def test_explicit_export_also_removes_authentication_and_host_fields(execution_book):
    book = execution_book
    value = audit(book)
    value['extra'] = {'client_secret': 'synthetic-private-client-value',
                      'access_token': 'synthetic-private-access-value',
                      'cookie': 'synthetic-private-cookie-value',
                      'host': 'synthetic-private-host.invalid', 'public_reason': 'retain me'}
    output = Path('reports/strategies/public-only.json')
    audit_module().write_audit_output(book.root, output, value)
    saved = (book.root / output).read_text()
    assert 'synthetic-private-' not in saved and 'retain me' in saved
    # Publishing does not mutate the supplied in-memory result.
    assert value['extra']['client_secret'] == 'synthetic-private-client-value'


@pytest.mark.parametrize('field,value', [
    ('temperature', {'value': '0.2'}), ('temperature', ['0.2']), ('temperature', True),
    ('max_output_tokens', {'value': 512}), ('max_output_tokens', [512]), ('max_output_tokens', True),
    ('json_mode', 'true'), ('top_p', {'value': '0.8'}), ('seed', {'value': 123}),
])
def test_model_generation_metadata_wrong_types_are_unavailable_without_leaking_values(
    execution_book, field, value,
):
    book = execution_book
    request, core = strategy_request(book)
    core['calculation']['identity'] = {
        'uses_model': True,
        'model': {'model': 'fixture', 'provider': 'provider',
                  'generation_parameters': {field: value}},
    }
    request = publish(book, request, core)
    freeze(book, request)
    before = tree(book.root)
    result = audit(book)
    assert result['state'] == 'PARTIAL'
    assert result['plans'][0]['audit_state'] == 'AUDIT_UNAVAILABLE'
    assert result['plans'][0]['identity']['model'] is None
    assert tree(book.root) == before
