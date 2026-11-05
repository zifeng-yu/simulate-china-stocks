import io
import json
import os
from dataclasses import replace
from urllib.error import HTTPError, URLError

import pytest

from china_stock_sim.cli import main
from china_stock_sim.models import (
    ChatMessage,
    ModelError,
    ModelResult,
    create_model_client,
    factory,
    load_model_settings,
    openai_compatible,
)
from china_stock_sim.models.openai_compatible import NoRedirect, OpenAICompatibleClient
from china_stock_sim.models.settings import local_values, model_status

# These deliberately fake values are used only in temporary files and mock requests.
FAKE_CREDENTIAL = "unit-test-only-credential"
CONFIG = """schema_version = 1
[models.daily_planner]
provider = "openai_compatible"
model = "test-model"
model_env = "CHINA_STOCK_SIM_LLM_MODEL"
base_url_env = "CHINA_STOCK_SIM_LLM_BASE_URL"
api_key_env = "CHINA_STOCK_SIM_LLM_API_KEY"
timeout_seconds = 30
max_output_tokens = 128
temperature = 0.0
[models.daily_planner.options]
enable_thinking = false
"""


@pytest.fixture(autouse=True)
def prevent_network_and_real_credentials(monkeypatch):
    for key in list(os.environ):
        if key.startswith("CHINA_STOCK_SIM_LLM_"):
            monkeypatch.delenv(key)

    def no_network(*args, **kwargs):
        raise AssertionError("This unit test must not construct a real HTTP client")

    monkeypatch.setattr(openai_compatible, "build_opener", no_network)


@pytest.fixture
def model_project(project):
    (project / "configs/models.toml").write_text(CONFIG)
    private = project / ".env"
    private.write_text(
        "CHINA_STOCK_SIM_LLM_BASE_URL=https://model.invalid/v1/\n"
        "CHINA_STOCK_SIM_LLM_MODEL=test-model-local\n"
        "CHINA_STOCK_SIM_LLM_API_KEY=" + FAKE_CREDENTIAL + "\n"
    )
    private.chmod(0o600)
    return project


@pytest.fixture
def model_settings(model_project):
    return load_model_settings(model_project)


def test_private_env_requires_0600_and_is_never_exposed(model_project):
    private = model_project / ".env"
    settings = load_model_settings(model_project)
    assert settings.api_key == FAKE_CREDENTIAL
    assert FAKE_CREDENTIAL not in repr(settings)
    assert settings.base_url == "https://model.invalid/v1"
    assert settings.model == "test-model-local"
    private.chmod(0o644)
    with pytest.raises(ModelError, match="chmod 600") as error:
        load_model_settings(model_project)
    assert FAKE_CREDENTIAL not in str(error.value)


def test_process_environment_overrides_private_file(model_project, monkeypatch):
    monkeypatch.setenv("CHINA_STOCK_SIM_LLM_BASE_URL", "https://other.invalid/compatible/v1/")
    monkeypatch.setenv("CHINA_STOCK_SIM_LLM_MODEL", "replacement-model")
    monkeypatch.setenv("CHINA_STOCK_SIM_LLM_API_KEY", "another-unit-test-credential")
    settings = load_model_settings(model_project)
    assert settings.base_url == "https://other.invalid/compatible/v1"
    assert settings.model == "replacement-model"
    assert settings.api_key == "another-unit-test-credential"
    assert settings.api_key not in repr(settings)


def test_env_parser_is_literal_and_ignores_unrelated_names(model_project):
    marker = model_project / "must_not_exist"
    private = model_project / ".env"
    private.write_text(
        "# only literal text\n"
        f'CHINA_STOCK_SIM_LLM_MODEL="$(touch {marker})"\n'
        "UNRELATED_NAME=ignored\n"
    )
    assert local_values(model_project) == {"CHINA_STOCK_SIM_LLM_MODEL": f"$(touch {marker})"}
    assert not marker.exists()


@pytest.mark.parametrize(
    "content",
    [
        "a line without an assignment",
        'CHINA_STOCK_SIM_LLM_MODEL="unterminated',
        "CHINA_STOCK_SIM_LLM_MODEL=first\nCHINA_STOCK_SIM_LLM_MODEL=second",
    ],
)
def test_malformed_private_file_has_sanitized_errors(model_project, content):
    (model_project / ".env").write_text(content + " " + FAKE_CREDENTIAL)
    with pytest.raises(ModelError) as error:
        load_model_settings(model_project)
    assert FAKE_CREDENTIAL not in str(error.value)


def test_private_file_symlink_is_rejected(model_project, tmp_path):
    private = model_project / ".env"
    external = tmp_path / "external-credentials"
    external.write_text("CHINA_STOCK_SIM_LLM_API_KEY=" + FAKE_CREDENTIAL)
    external.chmod(0o600)
    private.unlink()
    private.symlink_to(external)
    with pytest.raises(ModelError, match="普通文件"):
        load_model_settings(model_project)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://model.invalid/v1",
        "https://user:credential@model.invalid/v1",
        "https://model.invalid/v1?api_key=anything",
        "https://model.invalid/v1#credential",
    ],
)
def test_credentials_cannot_be_sent_to_insecure_or_credential_urls(
    model_project, monkeypatch, base_url
):
    monkeypatch.setenv("CHINA_STOCK_SIM_LLM_BASE_URL", base_url)
    with pytest.raises(ModelError):
        load_model_settings(model_project)


def test_local_http_is_supported_without_network(model_project, monkeypatch):
    monkeypatch.setenv("CHINA_STOCK_SIM_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    assert load_model_settings(model_project).base_url == "http://127.0.0.1:11434/v1"


def test_model_check_and_doctor_do_not_reveal_secrets_or_contact_service(model_project, capsys):
    assert main(["--root", str(model_project), "model-check"]) == 0
    output = capsys.readouterr().out
    status = json.loads(output)
    assert status["configured"] is True
    assert status["network_tested"] is False
    assert status["strategy_enabled"] is False
    assert FAKE_CREDENTIAL not in output
    assert "model.invalid" not in output
    assert main(["--root", str(model_project), "doctor"]) == 2
    output = capsys.readouterr().out
    assert json.loads(output)["checks"]["model"]["credential_present"] is True
    assert FAKE_CREDENTIAL not in output
    assert "model.invalid" not in output


def test_unconfigured_model_status_is_read_only(project):
    before = {p.relative_to(project): p.read_bytes() for p in project.rglob("*") if p.is_file()}
    status = model_status(project)
    assert status["configured"] is False
    assert status["network_tested"] is False
    assert {
        p.relative_to(project): p.read_bytes() for p in project.rglob("*") if p.is_file()
    } == before


def test_factory_can_replace_adapter_without_business_layer_changes(model_settings, monkeypatch):
    calls = []

    class StubClient:
        def __init__(self, settings):
            assert settings is model_settings

        def complete(self, messages, *, max_output_tokens=None, json_mode=False):
            calls.append((messages, max_output_tokens, json_mode))
            return ModelResult('{"ok":true}', "replacement", "stop")

    monkeypatch.setattr(factory, "OpenAICompatibleClient", StubClient)
    client = create_model_client(model_settings)
    messages = [ChatMessage("user", "只输出 JSON")]
    result = client.complete(messages, max_output_tokens=32, json_mode=True)
    assert calls == [(messages, 32, True)]
    assert isinstance(result, ModelResult)
    assert json.loads(result.text) == {"ok": True}
    with pytest.raises(ModelError, match="provider"):
        create_model_client(replace(model_settings, provider="unsupported"))


class MockResponse:
    def __init__(self, raw):
        self.raw = raw
        self.read_limits = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        self.read_limits.append(limit)
        return self.raw[:limit]


def mock_http(monkeypatch, *, body=None, error=None):
    calls = []
    response = MockResponse(body)

    class Opener:
        def open(self, request, *, timeout):
            calls.append((request, timeout))
            if error is not None:
                raise error
            return response

    def build(*handlers):
        assert handlers == (NoRedirect,)
        return Opener()

    monkeypatch.setattr(openai_compatible, "build_opener", build)
    return calls, response


def response_body(content='{"ok":true}', **extra):
    return json.dumps(
        {
            "model": "reported-model",
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            **extra,
        }
    ).encode()


def test_http_payload_and_response_use_standard_contract(model_settings, monkeypatch):
    calls, response = mock_http(
        monkeypatch,
        body=response_body(
            usage={
                "prompt_tokens": 9,
                "completion_tokens": 3,
                "total_tokens": 12,
                "unexpected": FAKE_CREDENTIAL,
            }
        ),
    )
    result = create_model_client(model_settings).complete(
        [ChatMessage("system", "只返回 JSON"), ChatMessage("user", "连接测试")],
        max_output_tokens=32,
        json_mode=True,
    )
    assert len(calls) == 1
    request, timeout = calls[0]
    assert request.full_url == "https://model.invalid/v1/chat/completions"
    assert request.method == "POST"
    assert request.get_header("Authorization") == "Bearer " + FAKE_CREDENTIAL
    assert request.get_header("Content-type") == "application/json"
    payload = json.loads(request.data)
    assert payload["messages"] == [
        {"role": "system", "content": "只返回 JSON"},
        {"role": "user", "content": "连接测试"},
    ]
    assert payload["model"] == "test-model-local"
    assert payload["stream"] is False
    assert payload["max_tokens"] == 32
    assert payload["temperature"] == 0.0
    assert payload["enable_thinking"] is False
    assert payload["response_format"] == {"type": "json_object"}
    assert FAKE_CREDENTIAL not in request.data.decode()
    assert timeout == 30
    assert response.read_limits == [2_000_001]
    assert json.loads(result.text) == {"ok": True}
    assert result.model == "reported-model"
    assert result.finish_reason == "stop"
    assert result.usage == {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12}


def test_response_echo_of_secret_is_redacted(model_settings, monkeypatch):
    body = json.dumps(
        {
            "model": FAKE_CREDENTIAL,
            "choices": [
                {"message": {"content": FAKE_CREDENTIAL}, "finish_reason": FAKE_CREDENTIAL}
            ],
            "usage": {"prompt_tokens": True, "completion_tokens": -1, "total_tokens": 7},
        }
    ).encode()
    mock_http(monkeypatch, body=body)
    result = OpenAICompatibleClient(model_settings).complete([ChatMessage("user", "test")])
    assert FAKE_CREDENTIAL not in repr(result)
    assert result.text == result.model == result.finish_reason == "[REDACTED]"
    assert result.usage == {"total_tokens": 7}


def test_http_failure_does_not_expose_body_or_key_and_is_not_retried(model_settings, monkeypatch):
    class UnreadableBody(io.BytesIO):
        def read(self, *args):
            raise AssertionError("An HTTP error response body must not be read")

    body = UnreadableBody(("sensitive response " + FAKE_CREDENTIAL).encode())
    error = HTTPError(
        "https://model.invalid/v1",
        401,
        FAKE_CREDENTIAL,
        {},
        body,
    )
    calls, _ = mock_http(monkeypatch, error=error)
    with pytest.raises(ModelError, match="HTTP 401") as captured:
        OpenAICompatibleClient(model_settings).complete([ChatMessage("user", "test")])
    assert FAKE_CREDENTIAL not in str(captured.value)
    assert "sensitive response" not in str(captured.value)
    assert captured.value.__suppress_context__ is True
    assert len(calls) == 1
    assert body.closed


@pytest.mark.parametrize("error", [TimeoutError(FAKE_CREDENTIAL), URLError(FAKE_CREDENTIAL)])
def test_transport_failure_is_sanitized_and_not_retried(model_settings, monkeypatch, error):
    calls, _ = mock_http(monkeypatch, error=error)
    with pytest.raises(ModelError, match="未自动重试") as captured:
        OpenAICompatibleClient(model_settings).complete([ChatMessage("user", "test")])
    assert FAKE_CREDENTIAL not in str(captured.value)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not-json",
        b"\xff",
        b"{}",
        b'{"choices":[]}',
        b'{"choices":null}',
        response_body(None),
        response_body("  "),
        response_body(usage=None),
    ],
)
def test_malformed_or_empty_response_is_a_controlled_error(model_settings, monkeypatch, body):
    calls, _ = mock_http(monkeypatch, body=body)
    with pytest.raises(ModelError):
        OpenAICompatibleClient(model_settings).complete([ChatMessage("user", "test")])
    assert len(calls) == 1


def test_oversized_response_is_rejected(model_settings, monkeypatch):
    calls, _ = mock_http(monkeypatch, body=b"x" * 2_000_001)
    with pytest.raises(ModelError, match="大小限制"):
        OpenAICompatibleClient(model_settings).complete([ChatMessage("user", "test")])
    assert len(calls) == 1


@pytest.mark.parametrize("tokens", [0, 129, True, 1.5])
def test_invalid_token_budget_is_rejected_before_http(model_settings, tokens):
    with pytest.raises(ModelError, match="输出上限"):
        OpenAICompatibleClient(model_settings).complete(
            [ChatMessage("user", "test")],
            max_output_tokens=tokens,
        )


def test_redirects_cannot_forward_credentials():
    assert (
        NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://other.invalid")
        is None
    )
