"""SM01 independent consumer acceptance, using fake settings and fake HTTP only."""

import io
import json
import os
from dataclasses import replace
from http.client import HTTPException
from urllib.error import HTTPError, URLError

import pytest

from china_stock_sim.cli import main
from china_stock_sim.models import (
    ChatMessage,
    ModelAudit,
    ModelError,
    ModelResult,
    openai_compatible,
)
from china_stock_sim.models import settings as settings_module
from china_stock_sim.models.openai_compatible import OpenAICompatibleClient
from china_stock_sim.models.settings import ModelSettings, load_model_settings, model_status

FAKE_KEY = "sm01-fake-credential-never-use"
FAKE_HOST = "private-model-unit-test.invalid"
FAKE_URL = "https://" + FAKE_HOST + "/compatible-mode/v1"
PUBLIC_URL = "https://public-reference.invalid/market"
CONFIGURATION_FIELDS = {
    "model_ref", "provider", "model", "timeout_seconds", "max_output_tokens",
    "temperature", "options", "json_mode",
}


@pytest.fixture(autouse=True)
def prevent_real_network_and_environment(monkeypatch):
    for key in list(os.environ):
        if key.startswith("CHINA_STOCK_SIM_LLM_"):
            monkeypatch.delenv(key)

    def prohibited_network(*args, **kwargs):
        raise AssertionError("SM01 tests must use fake HTTP responses")

    monkeypatch.setattr(openai_compatible, "build_opener", prohibited_network)


@pytest.fixture
def fake_settings():
    return ModelSettings(
        "openai_compatible", "fake-planner-model", FAKE_URL, FAKE_KEY,
        30, 128, 0.0, {"enable_thinking": False},
    )


def assert_private_values_absent(value):
    rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    assert FAKE_KEY not in rendered
    assert FAKE_HOST not in rendered.lower()
    assert FAKE_URL not in rendered


def install_http(monkeypatch, body=None, error=None):
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            assert limit == 2_000_001
            return json.dumps(body, ensure_ascii=False).encode()

    class Opener:
        def open(self, request, timeout):
            calls.append((request, timeout))
            if error is not None:
                raise error
            return Response()

    monkeypatch.setattr(openai_compatible, "build_opener", lambda *args: Opener())
    return calls


@pytest.mark.parametrize(
    "private_echo",
    [FAKE_KEY, FAKE_URL, FAKE_URL + "/chat/completions", FAKE_HOST, FAKE_HOST.upper()],
)
def test_text_redacts_known_private_values_and_keeps_business_meaning(fake_settings, private_echo):
    original = {
        "actions": [{"symbol": "600000", "side": "buy", "price": 10.25, "quantity": 100}],
        "note": "参考 " + PUBLIC_URL + "，内部连接 " + private_echo,
        "cash": 50000.0,
    }
    redacted = ModelAudit(fake_settings).text(json.dumps(original, ensure_ascii=False))
    assert_private_values_absent(redacted)
    parsed = json.loads(redacted)
    assert parsed["actions"] == original["actions"]
    assert parsed["cash"] == 50000.0
    assert PUBLIC_URL in parsed["note"]
    assert "参考" in parsed["note"]


def test_common_json_slash_escapes_are_redacted_without_breaking_json(fake_settings):
    encoded = json.dumps({"endpoint": FAKE_URL + "/chat/completions", "price": 9.87})
    escaped = encoded.replace("/", "\\/")
    safe = ModelAudit(fake_settings).text(escaped)
    assert_private_values_absent(safe)
    assert json.loads(safe)["price"] == 9.87
    assert json.loads(safe)["endpoint"] == "[REDACTED]"


def test_quoted_key_inside_json_is_redacted_and_json_still_parses(fake_settings):
    key = 'fake-quoted-"credential"-\\-测试'
    audit = ModelAudit(replace(fake_settings, api_key=key))
    for ascii_only in (True, False):
        safe = audit.text(json.dumps({"note": key, "side": "sell"}, ensure_ascii=ascii_only))
        assert json.loads(safe) == {"note": "[REDACTED]", "side": "sell"}


def test_normal_text_is_byte_for_byte_unchanged(fake_settings):
    text = '{"actions":[{"symbol":"600000","side":"sell","quantity":100}],"why":"止盈"}'
    assert ModelAudit(fake_settings).text(text) == text


@pytest.mark.parametrize(
    "template",
    [' {"quantity": 100, "quantity": 200, "note": "PRIVATE"}  ',
     '{"actions": [{"symbol": "600000", "why": "PRIVATE',
     'not JSON: {"why": "PRIVATE",,} price = 10.25'],
)
def test_text_redaction_preserves_duplicate_keys_and_incomplete_json_literals(
    fake_settings, template,
):
    private = FAKE_KEY + " " + FAKE_URL + " " + FAKE_HOST.upper()
    original = template.replace("PRIVATE", private)
    safe = ModelAudit(fake_settings).text(original)
    assert safe == template.replace("PRIVATE", "[REDACTED] [REDACTED] [REDACTED]")
    assert_private_values_absent(safe)


@pytest.mark.parametrize("value", [None, True, 4, {}, []])
def test_invalid_audit_text_is_a_controlled_error(fake_settings, value):
    with pytest.raises(ModelError) as error:
        ModelAudit(fake_settings).text(value)
    assert_private_values_absent(str(error.value))


def test_request_snapshot_is_allowlisted_detached_and_does_not_serialize_settings(fake_settings):
    class NotSerializable:
        def __deepcopy__(self, memo):
            raise AssertionError("The complete settings object must never be serialized")

        def __repr__(self):
            raise AssertionError("Unsupported options must never be stringified")

    options = {
        "enable_thinking": False,
        "unsupported_private_option": NotSerializable(),
        "api_key": FAKE_KEY,
        "base_url": FAKE_URL,
    }
    configured = replace(fake_settings, options=options)
    messages = [ChatMessage("system", "按规则输出 JSON"), ChatMessage("user", FAKE_KEY + FAKE_URL)]
    snapshot = ModelAudit(configured).request(
        "daily_planner", messages, max_output_tokens=64, json_mode=True,
    )
    assert set(snapshot) == {"configuration", "messages"}
    assert set(snapshot["configuration"]) == CONFIGURATION_FIELDS
    assert snapshot["configuration"] == {
        "model_ref": "daily_planner", "provider": "openai_compatible",
        "model": "fake-planner-model", "timeout_seconds": 30,
        "max_output_tokens": 64, "temperature": 0.0,
        "options": {"enable_thinking": False}, "json_mode": True,
    }
    assert snapshot["messages"][0] == {"role": "system", "content": "按规则输出 JSON"}
    assert_private_values_absent(snapshot)
    snapshot["configuration"]["options"]["enable_thinking"] = True
    snapshot["messages"][0]["content"] = "changed"
    assert configured.options["enable_thinking"] is False
    assert messages[0].content == "按规则输出 JSON"
    assert messages[1].content == FAKE_KEY + FAKE_URL


def test_configuration_redacts_all_public_string_metadata(fake_settings):
    configured = replace(fake_settings, model=FAKE_URL, provider=FAKE_KEY)
    snapshot = ModelAudit(configured).configuration(FAKE_HOST.upper())
    assert_private_values_absent(snapshot)
    assert snapshot["max_output_tokens"] == 128
    assert snapshot["json_mode"] is False


@pytest.mark.parametrize("budget", [0, 129, True, 1.5, "64"])
def test_request_and_transport_share_invalid_budget_rejection(fake_settings, budget):
    messages = [ChatMessage("user", "输出 JSON")]
    with pytest.raises(ModelError) as audit_error:
        ModelAudit(fake_settings).request("daily_planner", messages, max_output_tokens=budget)
    with pytest.raises(ModelError) as client_error:
        OpenAICompatibleClient(fake_settings).complete(messages, max_output_tokens=budget)
    assert str(audit_error.value) == str(client_error.value)


@pytest.mark.parametrize(
    "messages",
    [None, [], (), [{"role": "user", "content": "text"}],
     [ChatMessage("tool", "text")], [ChatMessage("user", " ")],
     [ChatMessage("user", None)]],
)
def test_invalid_messages_fail_without_network(fake_settings, messages):
    with pytest.raises(ModelError):
        ModelAudit(fake_settings).messages(messages)
    with pytest.raises(ModelError):
        OpenAICompatibleClient(fake_settings).complete(messages)


@pytest.mark.parametrize("json_mode", [None, 1, "true", []])
def test_non_boolean_json_mode_rejected_by_audit_and_transport(fake_settings, json_mode):
    with pytest.raises(ModelError):
        ModelAudit(fake_settings).configuration("daily_planner", json_mode=json_mode)
    with pytest.raises(ModelError):
        OpenAICompatibleClient(fake_settings).complete([ChatMessage("user", "text")], json_mode=json_mode)


@pytest.mark.parametrize(
    "updates",
    [{"timeout_seconds": True}, {"timeout_seconds": 0}, {"timeout_seconds": 301},
     {"temperature": True}, {"temperature": float("nan")}, {"temperature": 3},
     {"options": []}, {"options": {"enable_thinking": 1}},
     {"provider": None}, {"model": []}],
)
def test_invalid_audit_configuration_metadata_is_a_controlled_error(fake_settings, updates):
    with pytest.raises(ModelError) as error:
        ModelAudit(replace(fake_settings, **updates)).configuration("daily_planner")
    assert_private_values_absent(str(error.value))


def test_result_metadata_and_usage_are_allowlisted_and_detached(fake_settings):
    usage = {
        "prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9,
        "server_echo": FAKE_KEY + FAKE_URL,
    }
    original = ModelResult('{"why":"' + FAKE_URL + '","quantity":100}', FAKE_KEY, FAKE_HOST, usage)
    safe = ModelAudit(fake_settings).result(original)
    assert isinstance(safe, ModelResult)
    assert safe is not original
    assert_private_values_absent(repr(safe))
    assert json.loads(safe.text)["quantity"] == 100
    assert safe.usage == {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}
    safe.usage["prompt_tokens"] = 999
    assert original.usage["prompt_tokens"] == 7
    assert original.model == FAKE_KEY


@pytest.mark.parametrize("invalid_count", [-1, True, 1.5, "10", None])
def test_invalid_token_counts_are_omitted(fake_settings, invalid_count):
    safe = ModelAudit(fake_settings).result(
        ModelResult("ok", "model", "stop", {"prompt_tokens": invalid_count, "total_tokens": 0})
    )
    assert safe.usage == {"total_tokens": 0}


@pytest.mark.parametrize(
    "result",
    [None, {}, ModelResult(None, "model", "stop"), ModelResult("ok", None, "stop"),
     ModelResult("ok", "model", None), ModelResult("ok", "model", "stop", [])],
)
def test_invalid_result_metadata_has_fixed_safe_errors(fake_settings, result):
    with pytest.raises(ModelError) as error:
        ModelAudit(fake_settings).result(result)
    assert_private_values_absent(str(error.value))


def test_adapter_cleans_request_and_response_but_preserves_actual_routing_and_auth(
    fake_settings, monkeypatch,
):
    plan = {"actions": [{"symbol": "600000", "side": "buy", "quantity": 100, "price": 10.25}],
            "reason": FAKE_URL + "/chat/completions " + FAKE_KEY}
    calls = install_http(monkeypatch, {
        "model": FAKE_HOST.upper(),
        "choices": [{"message": {"content": json.dumps(plan)}, "finish_reason": FAKE_URL}],
        "usage": {"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50,
                  "private_metadata": FAKE_KEY},
    })
    original_messages = [ChatMessage("system", "遵守交易规则"),
                         ChatMessage("user", "不要复述 " + FAKE_KEY + " " + FAKE_URL)]
    result = OpenAICompatibleClient(fake_settings).complete(
        original_messages, max_output_tokens=64, json_mode=True,
    )
    assert len(calls) == 1
    request, timeout = calls[0]
    assert request.full_url == FAKE_URL + "/chat/completions"
    assert request.get_header("Authorization") == "Bearer " + FAKE_KEY
    assert timeout == 30
    payload = json.loads(request.data)
    assert_private_values_absent(payload)
    assert payload["model"] == "fake-planner-model"
    assert payload["messages"][0]["content"] == "遵守交易规则"
    assert payload["max_tokens"] == 64
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["enable_thinking"] is False
    assert FAKE_KEY in original_messages[1].content
    assert_private_values_absent(repr(result))
    assert json.loads(result.text)["actions"] == plan["actions"]
    assert result.usage == {"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50}


@pytest.mark.parametrize("exception_type", [TimeoutError, URLError, HTTPException])
def test_transport_failure_never_echoes_private_context_or_retries(
    fake_settings, monkeypatch, exception_type,
):
    calls = install_http(monkeypatch, error=exception_type(FAKE_KEY + " " + FAKE_URL))
    with pytest.raises(ModelError) as error:
        OpenAICompatibleClient(fake_settings).complete([ChatMessage("user", "hello")])
    assert len(calls) == 1
    assert_private_values_absent(str(error.value))
    assert "未自动重试" in str(error.value)
    assert error.value.__suppress_context__ is True


def test_http_failure_closes_body_without_reading_or_exposing_it(fake_settings, monkeypatch):
    class UnreadablePrivateBody(io.BytesIO):
        def read(self, *args, **kwargs):
            raise AssertionError("Error response bodies must not be read")

    body = UnreadablePrivateBody((FAKE_KEY + FAKE_URL).encode())
    error = HTTPError(FAKE_URL, 503, FAKE_KEY, {"Private": FAKE_KEY}, body)
    calls = install_http(monkeypatch, error=error)
    with pytest.raises(ModelError) as captured:
        OpenAICompatibleClient(fake_settings).complete([ChatMessage("user", "hello")])
    assert len(calls) == 1
    assert body.closed
    assert_private_values_absent(str(captured.value))
    assert "503" in str(captured.value)


def test_settings_repr_and_read_only_status_do_not_expose_private_metadata(
    fake_settings, project, monkeypatch,
):
    configured = replace(fake_settings, model=FAKE_KEY + FAKE_HOST)
    assert_private_values_absent(repr(configured))
    monkeypatch.setattr(settings_module, "load_model_settings", lambda *args: configured)
    before = {path.relative_to(project): path.read_bytes()
              for path in project.rglob("*") if path.is_file()}
    status = model_status(project)
    assert status["configured"] is True
    assert status["network_tested"] is False
    assert_private_values_absent(status)
    after = {path.relative_to(project): path.read_bytes()
             for path in project.rglob("*") if path.is_file()}
    assert after == before


@pytest.mark.parametrize("command, expected_exit", [("model-check", 0), ("doctor", 2)])
def test_cli_status_redacts_private_values_embedded_in_model_metadata(
    fake_settings, project, monkeypatch, capsys, command, expected_exit,
):
    configured = replace(fake_settings, model="fake-model " + FAKE_KEY + " " + FAKE_URL)
    monkeypatch.setattr(settings_module, "load_model_settings", lambda *args: configured)
    before = {path.relative_to(project): path.read_bytes()
              for path in project.rglob("*") if path.is_file()}
    assert main(["--root", str(project), command]) == expected_exit
    captured = capsys.readouterr()
    assert_private_values_absent(captured.out + captured.err)
    parsed = json.loads(captured.out)
    model = parsed if command == "model-check" else parsed["checks"]["model"]
    assert model["configured"] is True
    assert model["network_tested"] is False
    assert model["model"] == "fake-model [REDACTED] [REDACTED]"
    after = {path.relative_to(project): path.read_bytes()
             for path in project.rglob("*") if path.is_file()}
    assert after == before


@pytest.mark.parametrize(
    "bad_url",
    ["https://private-model-unit-test.invalid:bad/v1",
     "https://private-model-unit-test.invalid:65536/v1",
     "https://private-model-unit-test.invalid/\nv1",
     "https://private-model-unit-test.invalid/\tv1",
     "https://private-model-unit-test.invalid/\x00v1"],
)
def test_invalid_endpoint_configuration_is_rejected_without_echoing_it(
    project, monkeypatch, bad_url,
):
    (project / "configs/models.toml").write_text('''schema_version = 1
[models.daily_planner]
provider = "openai_compatible"
model = "fake-model"
model_env = "CHINA_STOCK_SIM_LLM_MODEL"
base_url_env = "CHINA_STOCK_SIM_LLM_BASE_URL"
api_key_env = "CHINA_STOCK_SIM_LLM_API_KEY"
timeout_seconds = 30
max_output_tokens = 128
''')
    monkeypatch.setattr(settings_module, "local_values", lambda root: {
        "CHINA_STOCK_SIM_LLM_BASE_URL": bad_url,
        "CHINA_STOCK_SIM_LLM_API_KEY": FAKE_KEY,
    })
    with pytest.raises(ModelError) as error:
        load_model_settings(project)
    assert_private_values_absent(str(error.value))
