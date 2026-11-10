"""Fixed-input equivalence and read boundaries; all accounts/sources are isolated."""

import json
from collections import Counter

import pytest
from execution_fixtures import FIRST, PREVIOUS, advance, execution_book
from test_ledger_trading import call
from test_market_strategy_contract import add_dividend, seed_history
from test_strategy_acceptance import strategy_world, tree_state

from china_stock_sim.application.strategy_inputs import assemble_inputs
from china_stock_sim.ledger.decision_evidence import read_business_document
from china_stock_sim.ledger.kernel import LedgerError, Transaction
from china_stock_sim.market.contracts import DataResult

assert execution_book and strategy_world


def prepared(facts, size):
    market, cutoff = facts.reader, facts.cutoff
    window = market.calendar_operation('window', facts.end, size=size,
                                       knowledge_cutoff=cutoff)
    return {'code': 'SH600000', 'as_of': facts.end, 'size': size,
        'knowledge_cutoff': cutoff, 'window': window,
        'prices': market.prices('SH600000', window.data[0], facts.end,
                                knowledge_cutoff=cutoff),
        'events': market.events('SH600000', knowledge_cutoff=cutoff),
        'event_coverage': market.event_coverage('SH600000', window.data[0], facts.end,
                                               knowledge_cutoff=cutoff)}


@pytest.mark.parametrize('size', [21, 121, 130])
@pytest.mark.parametrize('dividend', [False, True])
def test_prepared_signal_exactly_matches_public_query_without_reads(tmp_path, monkeypatch,
                                                                   size, dividend):
    facts = seed_history(tmp_path, dividend=dividend)
    if dividend:
        add_dividend(facts, fetched=facts.cutoff)
    inputs = prepared(facts, size)
    expected = facts.reader.signal_prices('SH600000', facts.end, size=size,
                                          knowledge_cutoff=facts.cutoff)
    before = tree_state(facts.repository.root)

    def forbidden(*args, **kwargs):
        pytest.fail('prepared signal repeated an already-consumed market query')

    for name in ('calendar_operation', 'prices', 'events', 'event_coverage'):
        monkeypatch.setattr(facts.reader, name, forbidden)
    result = facts.reader.signal_prices('SH600000', facts.end, size=size,
        knowledge_cutoff=facts.cutoff, prepared_inputs=inputs)
    assert result == expected
    assert tree_state(facts.repository.root) == before


@pytest.mark.parametrize('failure', ['window', 'prices', 'events', 'event_coverage'])
def test_prepared_signal_preserves_failure_precedence(tmp_path, monkeypatch, failure):
    facts = seed_history(tmp_path)
    inputs = prepared(facts, 121)
    invalid = DataResult('CORRUPT' if failure == 'events' else 'MISSING', [],
                         ({'kind': 'ISOLATED_' + failure.upper()},), ('isolated-failure',))
    inputs[failure] = invalid
    methods = {'window': 'calendar_operation', 'prices': 'prices',
               'events': 'events', 'event_coverage': 'event_coverage'}
    for key, method in methods.items():
        monkeypatch.setattr(facts.reader, method,
                            lambda *a, result=inputs[key], **k: result)
    expected = facts.reader.signal_prices('SH600000', facts.end, size=121,
                                          knowledge_cutoff=facts.cutoff)
    result = facts.reader.signal_prices('SH600000', facts.end, size=121,
        knowledge_cutoff=facts.cutoff, prepared_inputs=inputs)
    assert result == expected
    assert result.state != 'READY'


def test_prepared_signal_does_not_consume_later_inputs_after_early_failure(tmp_path):
    facts = seed_history(tmp_path)
    inputs = prepared(facts, 121)
    missing = DataResult('MISSING', [], ({'kind': 'MISSING_BAR'},))
    inputs['prices'] = missing
    inputs['events'] = DataResult('CORRUPT', {'unexpected': 'ignored'})
    inputs['event_coverage'] = DataResult('READY', {'unexpected': 'ignored'})
    assert facts.reader.signal_prices('SH600000', facts.end, size=121,
        knowledge_cutoff=facts.cutoff, prepared_inputs=inputs) == missing


@pytest.mark.parametrize('key,value', [
    ('code', 'SZ000001'), ('as_of', '2024-01-01'), ('size', 21),
    ('knowledge_cutoff', '2024-01-01T18:12:00+08:00'), ('window', {}),
])
def test_prepared_signal_rejects_other_query_identity(tmp_path, key, value):
    facts = seed_history(tmp_path)
    inputs = prepared(facts, 121)
    inputs[key] = value
    with pytest.raises(ValueError, match='同一证券'):
        facts.reader.signal_prices('SH600000', facts.end, size=121,
            knowledge_cutoff=facts.cutoff, prepared_inputs=inputs)


@pytest.mark.parametrize('size', [0, -1, True, 1.5, '21'])
def test_prepared_signal_keeps_public_window_parameter_guard(tmp_path, size):
    facts = seed_history(tmp_path)
    inputs = prepared(facts, 121)
    inputs['size'] = size
    inputs['window'] = DataResult('MISSING')
    for reuse in (None, inputs):
        with pytest.raises(ValueError, match='正整数'):
            facts.reader.signal_prices('SH600000', facts.end, size=size,
                knowledge_cutoff=facts.cutoff, prepared_inputs=reuse)


@pytest.mark.parametrize('field', ['prices', 'events', 'event_coverage', 'window', 'cutoff'])
def test_prepared_signal_does_not_accept_relabelled_foreign_facts(tmp_path, field):
    facts = seed_history(tmp_path)
    inputs = prepared(facts, 121)
    if field == 'window':
        inputs['window'] = DataResult('READY', inputs['window'].data[:-1])
    elif field == 'cutoff':
        body = dict(inputs['prices'].data[0])
        body['_provenance'] = {**body['_provenance'],
                               'available_at': facts.end + 'T18:13:00+08:00'}
        inputs['prices'] = DataResult('READY', [body, *inputs['prices'].data[1:]])
    else:
        rows = inputs[field].data
        if field == 'events':
            rows = [{'code': 'SZ000001'}]
        elif isinstance(rows, list):
            rows = [{**rows[0], 'code': 'SZ000001'}, *rows[1:]]
        else:
            rows = {**rows, 'code': 'SZ000001'}
        inputs[field] = DataResult('READY', rows)
    with pytest.raises(ValueError, match='复用'):
        facts.reader.signal_prices('SH600000', facts.end, size=121,
            knowledge_cutoff=facts.cutoff, prepared_inputs=inputs)


def test_prepared_signal_keeps_same_cutoff_even_after_new_facts(tmp_path):
    facts = seed_history(tmp_path, dividend=True)
    inputs = prepared(facts, 121)
    before = facts.reader.signal_prices('SH600000', facts.end, size=121,
                                        knowledge_cutoff=facts.cutoff)
    add_dividend(facts, fetched=facts.end + 'T18:13:00+08:00')
    assert facts.reader.signal_prices('SH600000', facts.end, size=121,
        knowledge_cutoff=facts.cutoff, prepared_inputs=inputs) == before
    later = facts.reader.signal_prices('SH600000', facts.end, size=121,
        knowledge_cutoff=facts.end + 'T18:13:00+08:00')
    assert later != before


@pytest.mark.parametrize('account', ['S1', 'S1-LT', 'LLM'])
def test_entire_input_bundle_equal_with_exactly_one_market_read_per_code(strategy_world,
        monkeypatch, account, record_property):
    book, _, _ = strategy_world
    calls, reuse = Counter(), [False]
    for name in ('calendar_operation', 'prices', 'events', 'event_coverage', 'signal_prices'):
        original = getattr(book.market, name)

        def counted(*args, method=name, original=original, **kwargs):
            calls[method] += 1
            if method == 'signal_prices' and not reuse[0]:
                kwargs.pop('prepared_inputs', None)
            return original(*args, **kwargs)

        monkeypatch.setattr(book.market, name, counted)
    before = tree_state(book.root)
    expected = assemble_inputs(book.settings, book.market, book, account, FIRST, book._now())
    baseline = dict(calls)
    calls.clear()
    reuse[0] = True
    actual = assemble_inputs(book.settings, book.market, book, account, FIRST, book._now())
    assert actual == expected
    assert tree_state(book.root) == before
    count = len(actual['input_bundle']['context']['securities'])
    for name in ('prices', 'events', 'event_coverage', 'calendar_operation'):
        assert baseline[name] - calls[name] == count
    assert baseline['signal_prices'] == calls['signal_prices'] == count
    assert all('prepared_inputs' not in row['kwargs']
               for row in actual['input_bundle']['data_evidence'])
    record_property('market_call_counts', json.dumps({'account': account, 'codes': count,
                    'before': baseline, 'after': dict(calls)}, sort_keys=True))


def poor_initial_plan(book, make_app):
    advance(book, FIRST)
    assert book.create_account({'account_id': 'POOR', 'strategy_id': 'S1',
        'initial_cash_fen': 100}, operation_id='poor-account')['state'] == 'APPLIED'
    assert call(book, 'activate_simulation', {'target_session': '2026-11-02'},
                account='POOR')['state'] == 'APPLIED'
    assert make_app().publish('POOR', '2026-11-02')['state'] == 'FROZEN'
    return book.get_strategy_decision('POOR', target_session='2026-11-02')


def test_initial_empty_evaluation_uses_verified_core_without_full_history(strategy_world,
                                                                        monkeypatch):
    book, make_app, _ = strategy_world
    prior = poor_initial_plan(book, make_app)
    assert not book.report_snapshot('POOR', '2026-11-03')['rebalance_tasks']
    original = Transaction.list

    def bounded(self, table, **kwargs):
        assert table != 'strategy_decisions', 'initial flag expanded the full decision history'
        return original(self, table, **kwargs)

    monkeypatch.setattr(Transaction, 'list', bounded)
    before = tree_state(book.root)
    assert book.initial_strategy_evaluated('POOR', '2026-11-03') is True
    assert book.initial_strategy_evaluated('POOR', '2026-11-02') is False
    assert book.initial_strategy_evaluated('POOR', FIRST) is False
    assert tree_state(book.root) == before
    core, _ = read_business_document(book.root, prior['decision_core'])
    assert core['calculation']['initial_evaluated'] is True


@pytest.mark.parametrize('damage', ['missing', 'changed'])
def test_initial_evaluation_does_not_accept_damaged_core(strategy_world, damage):
    book, make_app, _ = strategy_world
    prior = poor_initial_plan(book, make_app)
    path = book.root / prior['decision_core']['path']
    if damage == 'missing':
        path.unlink()
    else:
        path.write_text('{}\n')
    with pytest.raises((LedgerError, ValueError, OSError)):
        book.initial_strategy_evaluated('POOR', '2026-11-03')


def test_saved_parent_before_core_retains_legacy_initial_fact(strategy_world, monkeypatch):
    book, make_app, _ = strategy_world
    app = make_app()
    monkeypatch.setattr(app, '_proposal', lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError('isolated saved-parent interruption')))
    assert app.plan('S1', FIRST)['state'] == 'INVALID_INPUT'
    row = book.get_strategy_decision('S1', target_session=FIRST)
    assert row['state'] == 'PREPARED' and not row.get('decision_core')
    before = tree_state(book.root)
    assert book.initial_strategy_evaluated('S1', FIRST) is True
    assert book.initial_strategy_evaluated('S1', PREVIOUS) is False
    assert tree_state(book.root) == before


def test_prior_model_core_without_initial_evaluation_stays_false(strategy_world):
    book, make_app, _ = strategy_world
    assert make_app().publish('LLM', FIRST)['state'] == 'FROZEN'
    assert book.initial_strategy_evaluated('LLM', '2026-11-02') is False
