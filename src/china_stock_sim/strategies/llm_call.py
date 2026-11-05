"""One bounded model call in a disposable process; attempt ownership stays outside.

The application claims before this call and persists RECEIVED before parsing. It
supplies the remaining budget from the first claim's fixed target-day deadline.
No retries, ledger access, settings loading, or provider-specific HTTP here.
"""

import math
import multiprocessing
import os
import time
from contextlib import redirect_stderr, redirect_stdout

from china_stock_sim.models.base import ChatMessage, ModelError, ModelResult

_MAX_TEXT_BYTES = 2_000_000


class ModelCallError(ModelError):
    """A fixed, public failure that never carries a raw provider exception."""

    def __init__(self, code):
        self.code = code
        super().__init__('模型调用达到总时限，未自动重试' if code == 'TIMEOUT'
                         else '模型调用失败，未自动重试')


def _invoke(connection, client, messages, max_output_tokens, json_mode):
    # Client/third-party print output must not expose prompts, credentials or URLs.
    try:
        with open(os.devnull, 'w') as sink, redirect_stdout(sink), redirect_stderr(sink):
            result = client.complete(messages, max_output_tokens=max_output_tokens,
                                     json_mode=json_mode)
            if (not isinstance(result, ModelResult) or not isinstance(result.text, str)
                    or not isinstance(result.model, str) or not isinstance(result.finish_reason, str)
                    or not isinstance(result.usage, dict)
                    or len(result.text.encode('utf-8')) > _MAX_TEXT_BYTES
                    or len(result.model) > 1000 or len(result.finish_reason) > 1000):
                connection.send(('FAILED', None))
                return
            usage = {key: value for key, value in result.usage.items()
                     if key in {'prompt_tokens', 'completion_tokens', 'total_tokens'}
                     and type(value) is int and value >= 0}
            connection.send(('RECEIVED', ModelResult(result.text, result.model,
                                                      result.finish_reason, usage)))
    except BaseException:
        # This process must never print or serialize the exception and its secrets.
        try:
            connection.send(('FAILED', None))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()


def _reap(process):
    if process.is_alive():
        process.terminate()
        process.join(timeout=0.2)
    if process.is_alive():
        process.kill()
        process.join(timeout=0.2)
    else:
        process.join(timeout=0)
    process.close()


def call_with_deadline(client, messages, *, timeout_seconds, max_output_tokens=2048,
                       json_mode=True):
    """Call once with a hard remaining budget, including child startup.

    Real clients and test doubles must be spawn-pickleable. Tests may instead
    inject this entire caller at the application boundary. A socket/Future timeout
    is never substituted for the default process deadline. The caller sanitizes
    the returned ModelResult with ModelAudit before persistence or parsing.
    """
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 120
            or type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 8192
            or type(json_mode) is not bool or not isinstance(messages, list) or not messages
            or any(not isinstance(m, ChatMessage) or m.role not in {'system', 'user', 'assistant'}
                   or not isinstance(m.content, str) or not m.content.strip() for m in messages)):
        raise ValueError('模型调用预算或标准消息无效')
    deadline = time.monotonic() + timeout_seconds
    context = multiprocessing.get_context('spawn')
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=_invoke,
                              args=(send, client, messages, max_output_tokens, json_mode))
    started = False
    try:
        try:
            process.start()
            started = True
        except Exception:
            raise ModelCallError('FAILED') from None
        finally:
            send.close()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ModelCallError('TIMEOUT')
            if receive.poll(min(remaining, 0.02)):
                try:
                    state, result = receive.recv()
                except (EOFError, OSError, ValueError, TypeError):
                    raise ModelCallError('FAILED') from None
                if time.monotonic() >= deadline:
                    raise ModelCallError('TIMEOUT')
                if state != 'RECEIVED' or not isinstance(result, ModelResult):
                    raise ModelCallError('FAILED')
                return result
            if not process.is_alive():
                # poll again after exit: a successfully sent small response may
                # become visible between the preceding poll and the exit check.
                if receive.poll(0):
                    continue
                raise ModelCallError('FAILED')
    except (OSError, EOFError):
        raise ModelCallError('FAILED') from None
    finally:
        receive.close()
        if started:
            _reap(process)
        else:
            process.close()
