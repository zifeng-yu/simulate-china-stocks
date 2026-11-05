"""Default production wrapper really kills slow children; no external HTTP used."""

import multiprocessing
import os
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from china_stock_sim.models.audit import ModelAudit
from china_stock_sim.models.base import ChatMessage, ModelError, ModelResult
from china_stock_sim.models.settings import ModelSettings
from china_stock_sim.strategies.llm_call import ModelCallError, call_with_deadline


@dataclass
class _Client:
    mode: str = 'ok'
    pid_path: str | None = None

    def complete(self, messages, *, max_output_tokens=None, json_mode=False):
        if self.pid_path:
            Path(self.pid_path).write_text(str(os.getpid()))
        if self.mode == 'slow':
            time.sleep(20)
        if self.mode == 'crash':
            os._exit(7)
        if self.mode == 'error':
            print('PRIVATE-KEY-123 https://private.invalid/v1')
            raise ModelError('PRIVATE-KEY-123 https://private.invalid/v1')
        if self.mode == 'malformed':
            return {'text': 'not a ModelResult'}
        if self.mode == 'oversized':
            return ModelResult('x' * 2_000_001, 'fake', 'stop')
        if self.mode == 'echo_secret':
            return ModelResult('PRIVATE-KEY-123 private.invalid', 'fake', 'stop')
        assert len(messages) == 1 and messages[0].content == 'fixture'
        assert max_output_tokens == 2048 and json_mode is True
        return ModelResult('{"decision":"HOLD"}', 'fake', 'stop',
                           {'prompt_tokens': 10, 'completion_tokens': 3, 'total_tokens': 13,
                            'other': 2, 'private': 'secret', 'bool': True})


_MESSAGES = [ChatMessage('user', 'fixture')]


def test_llm_single_process_call_returns_metadata_without_internal_retries(tmp_path):
    path = tmp_path / 'pid'
    result = call_with_deadline(_Client(pid_path=str(path)), _MESSAGES, timeout_seconds=3)
    assert result.text == '{"decision":"HOLD"}'
    assert result.usage == {'prompt_tokens': 10, 'completion_tokens': 3, 'total_tokens': 13}
    with pytest.raises(ProcessLookupError):
        os.kill(int(path.read_text()), 0)


def test_llm_process_total_deadline_terminates_and_reaps_inflight_call(tmp_path):
    before = {p.pid for p in multiprocessing.active_children()}
    path = tmp_path / 'pid'
    started = time.monotonic()
    with pytest.raises(ModelCallError) as info:
        call_with_deadline(_Client('slow', str(path)), _MESSAGES, timeout_seconds=0.7)
    assert info.value.code == 'TIMEOUT'
    assert time.monotonic() - started < 2
    assert path.exists()  # actual model body started, rather than failing only at bootstrap
    with pytest.raises(ProcessLookupError):
        os.kill(int(path.read_text()), 0)
    assert {p.pid for p in multiprocessing.active_children()} == before


@pytest.mark.parametrize('mode', ['error', 'crash', 'malformed', 'oversized'])
def test_llm_errors_are_fixed_safe_messages_and_child_is_reaped(mode, capfd):
    before = {p.pid for p in multiprocessing.active_children()}
    with pytest.raises(ModelCallError) as info:
        call_with_deadline(_Client(mode), _MESSAGES, timeout_seconds=3)
    assert info.value.code == 'FAILED'
    assert str(info.value) == '模型调用失败，未自动重试'
    captured = capfd.readouterr()
    assert 'PRIVATE-KEY' not in captured.out + captured.err + str(info.value)
    assert 'private.invalid' not in captured.out + captured.err + str(info.value)
    assert {p.pid for p in multiprocessing.active_children()} == before


def test_llm_successful_response_is_sanitized_before_application_persists_it():
    result = call_with_deadline(_Client('echo_secret'), _MESSAGES, timeout_seconds=3)
    audit = ModelAudit(ModelSettings('openai_compatible', 'fake', 'https://private.invalid/v1',
                                    'PRIVATE-KEY-123', 120, 2048, 0.0, {}))
    safe = audit.result(result)
    assert safe.text == '[REDACTED] [REDACTED]'


@pytest.mark.parametrize('seconds', [0, -1, True, float('nan'), float('inf'), 121, '1'])
def test_llm_invalid_remaining_deadline_never_spawns_or_calls(seconds):
    before = {p.pid for p in multiprocessing.active_children()}
    with pytest.raises(ValueError):
        call_with_deadline(_Client(), _MESSAGES, timeout_seconds=seconds)
    assert {p.pid for p in multiprocessing.active_children()} == before


@pytest.mark.parametrize('messages', [[], (), ['fixture'], [ChatMessage('tool', 'x')],
                                     [ChatMessage('user', '')]])
def test_llm_only_nonempty_standard_messages_are_sent(messages):
    with pytest.raises(ValueError):
        call_with_deadline(_Client(), messages, timeout_seconds=1)


def test_llm_spawn_configuration_error_is_not_echoed_as_provider_or_secret_error():
    class _NotPickleableClient:
        def complete(self, *args, **kwargs):
            raise AssertionError('never called')
    with pytest.raises(ModelCallError) as info:
        call_with_deadline(_NotPickleableClient(), _MESSAGES, timeout_seconds=1)
    assert info.value.code == 'FAILED'
    assert 'pickle' not in str(info.value)
