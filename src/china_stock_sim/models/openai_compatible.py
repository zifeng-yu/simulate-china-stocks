"""OpenAI 格式 HTTP 适配器，可接百炼等服务；不依赖 OpenAI SDK。"""

import json
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .audit import ModelAudit, validated_output_tokens
from .base import ChatMessage, ModelError, ModelResult
from .settings import ModelSettings


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OpenAICompatibleClient:
    def __init__(self, settings: ModelSettings):
        self.settings = settings

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        max_output_tokens: int | None = None,
        json_mode: bool = False,
    ) -> ModelResult:
        audit = ModelAudit(self.settings)
        safe_messages = audit.messages(messages)
        tokens = validated_output_tokens(self.settings, max_output_tokens)
        if type(json_mode) is not bool:
            raise ModelError("模型JSON模式必须为布尔值")
        payload = {
            "model": self.settings.model,
            "messages": safe_messages,
            "stream": False,
            "max_tokens": tokens,
            "temperature": self.settings.temperature,
            **self.settings.options,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            request = Request(
                self.settings.base_url + "/chat/completions",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + self.settings.api_key,
                },
                method="POST",
            )
            # No retries here. Daily retry budgets belong to the strategy orchestration.
            with build_opener(NoRedirect).open(
                request, timeout=self.settings.timeout_seconds
            ) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ModelError("模型返回内容超过大小限制")
            result = json.loads(raw)
            choice = result["choices"][0]
            text = choice["message"]["content"]
            if not isinstance(text, str) or not text.strip():
                raise ModelError("模型没有返回可用文本")
            return audit.result(ModelResult(
                text,
                result.get("model", self.settings.model),
                choice.get("finish_reason", "unknown"),
                result.get("usage", {}),
            ))
        except HTTPError as exc:
            # Never print server bodies, request headers, or credentials.
            code = exc.code
            exc.close()
            raise ModelError(f"模型服务返回 HTTP {code}；未自动重试") from None
        except (URLError, TimeoutError, OSError, HTTPException):
            raise ModelError("模型连接失败或超时；未自动重试，请检查网络与服务配置") from None
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise ModelError("模型响应格式不符合当前适配器协议") from None
