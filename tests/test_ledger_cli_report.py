"""Public DTO, command and renderer acceptance; no model configuration needed."""

import json
from dataclasses import fields
from datetime import datetime
from html.parser import HTMLParser

import pytest
from test_ledger_trading import (
    DAY,
    Facts,
    book,
    buy_and_close,
    call,
    plan_payload,
)

from china_stock_sim import domain
from china_stock_sim.cli import main
from china_stock_sim.ledger import LedgerService
from china_stock_sim.ledger.store import read_snapshot
from china_stock_sim.market.contracts import DataResult
from china_stock_sim.reporting.ledger import write_ledger_report

assert book  # Expose the shared pytest fixture to this consumer module.


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    monkeypatch.setattr('china_stock_sim.cli.model_status', lambda *a, **k: pytest.fail('ledger must not load model config'))
    monkeypatch.setattr(Facts, 'index_quote', lambda *a, **k: DataResult('MISSING', []), raising=False)


def test_public_read_commands_do_not_write_or_load_model(settings, capsys):
    LedgerService(settings).initialize()
    before = settings.database.read_bytes()
    for command in ('ledger-status', 'ledger-query', 'ledger-audit'):
        assert main(['--root', str(settings.root), command]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result
        assert settings.database.read_bytes() == before


def test_missing_ledger_query_does_not_initialize(project, capsys):
    assert main(['--root', str(project), 'ledger-status']) == 2
    assert json.loads(capsys.readouterr().err)['state'] == 'ERROR'
    assert not (project / 'data/state/ledger.sqlite3').exists()


def test_ledger_cli_apply_and_audit_protocol(book, monkeypatch, capsys):
    monkeypatch.setattr('china_stock_sim.ledger.cli.LedgerService', lambda root: book)
    payload = plan_payload(book, orders=[])
    path = book.root / 'plan-input.json'
    path.write_text(json.dumps(payload))
    args = ['--root', str(book.root), 'ledger-apply', '--action', 'freeze_plan',
            '--account', 'LLM', '--input', str(path), '--operation-id', 'cli-plan', '--expected-revision', '1']
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)['state'] == 'APPLIED'
    before = book.settings.database.read_bytes()
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)['state'] == 'ALREADY_APPLIED'
    assert book.settings.database.read_bytes() == before


def test_output_contracts_match_real_report_and_statement(book):
    buy_and_close(book)
    statement = book.daily_statement('LLM', DAY)
    report = book.report_snapshot('LLM', DAY)
    assert set(domain.DailyStatement.__required_keys__) <= set(statement)
    assert set(domain.ReportSnapshot.__required_keys__) <= set(report)
    state = book.account_state('LLM', '2026-11-02')
    assert {f.name for f in fields(domain.AccountState)} <= set(state)
    position = book.positions('LLM', DAY)[0]
    assert {f.name for f in fields(domain.Position)} <= set(position)


@pytest.mark.parametrize('keyword_call', [False, True])
def test_public_plan_dataclass_is_accepted(book, keyword_call):
    value = plan_payload(book, orders=[])
    dto = domain.PlanEnvelope(account_snapshot=value['account_snapshot'],
        account_snapshot_id=value['account_snapshot_id'], account_snapshot_hash=value['account_snapshot_hash'],
        ruleset=value['ruleset'], plan_id=value['plan_id'], account_id='LLM', strategy_id='LLM-Daily',
        strategy_version='test', target_session=datetime.fromisoformat(DAY).date(),
        market_as_of=datetime.fromisoformat(value['market_as_of']).date(),
        input_cutoff=datetime.fromisoformat(value['input_cutoff']),
        generated_at=datetime.fromisoformat(value['generated_at']), account_revision=1,
        market_snapshot_ids=tuple(value['market_snapshot_ids']), orders=(),
        fee_policy=book.settings.raw['fees'], rule_policy=book.settings.raw['execution'], no_order_reason='HOLD')
    result = (book.freeze_plan('LLM', payload=dto, operation_id='keyword-plan', expected_revision=1)
              if keyword_call else call(book, 'freeze_plan', dto))
    assert result['state'] == 'APPLIED'


def test_received_response_persisted_before_validation_and_no_reasking(book):
    first = call(book, 'claim_model_attempt', {'target_session': DAY})
    response = {'text': 'invalid-json'}
    assert call(book, 'finish_model_attempt', {'target_session': DAY, 'attempt_id': first['attempt_id'],
        'status': 'RECEIVED', 'response': response})['state'] == 'APPLIED'
    assert book.pending_work('LLM')['model_attempts'][0]['status'] == 'RECEIVED'
    assert call(book, 'claim_model_attempt', {'target_session': DAY})['state'] == 'CONFLICT'
    original = book.report_snapshot('LLM')['model_attempts'][0]['response_document']
    assert call(book, 'finish_model_attempt', {'target_session': DAY, 'attempt_id': first['attempt_id'],
        'status': 'INVALID', 'reason': 'validation rejected'})['state'] == 'APPLIED'
    assert book.report_snapshot('LLM')['model_attempts'][0]['response_document'] == original
    assert call(book, 'claim_model_attempt', {'target_session': DAY})['attempt_number'] == 2


class EmbeddedReport(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.text = ''
        self.external = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.active = tag == 'script' and attrs.get('id') == 'report-data'
        if 'src' in attrs:
            self.external.append(attrs['src'])

    def handle_endtag(self, tag):
        if tag == 'script':
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.text += data


def test_daily_report_csv_use_same_snapshot_and_do_not_change_ledger(book):
    buy_and_close(book)
    before = book.settings.database.read_bytes()
    published = write_ledger_report(book.settings, read_snapshot(book.settings), ledger=book)
    assert published['state'] == 'LEDGER_REPORT'
    page = book.settings.report.read_text()
    parsed = EmbeddedReport()
    parsed.feed(page)
    reports = json.loads(parsed.text)
    assert parsed.external == []
    llm = next(r for r in reports if r['account']['account_id'] == 'LLM')
    assert llm['statements'][-1]['closing_cash_fen'] == 3999490
    csv = (book.settings.report.parent / f'LLM-r{llm["report_revision"]}.csv').read_text()
    assert '3999490' in csv and DAY in csv
    assert '1000' in page and '39,994.90' in page
    assert book.settings.database.read_bytes() == before
    assert not list(book.settings.report.parent.glob('*.tmp'))


def test_failed_report_leaves_existing_page_and_ledger(book, monkeypatch):
    buy_and_close(book)
    book.settings.report.parent.mkdir(parents=True, exist_ok=True)
    book.settings.report.write_text('last successful page')
    before = book.settings.database.read_bytes()
    monkeypatch.setattr('china_stock_sim.reporting.ledger.atomic_text',
        lambda *a, **k: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):
        write_ledger_report(book.settings, read_snapshot(book.settings), ledger=book)
    assert book.settings.report.read_text() == 'last successful page'
    assert book.settings.database.read_bytes() == before
