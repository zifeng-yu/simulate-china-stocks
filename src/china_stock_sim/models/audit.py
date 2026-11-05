"""Known private-value redaction and explicit public model audit snapshots.

This module does not persist records or manage strategy attempts. Never serialize
ModelSettings for audit: construct the allowed fields here instead.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from .base import ChatMessage, ModelError, ModelResult

if TYPE_CHECKING:
    from .settings import ModelSettings


def validated_output_tokens(settings: ModelSettings, max_output_tokens: int | None) -> int:
    tokens = settings.max_output_tokens if max_output_tokens is None else max_output_tokens
    if (
        type(settings.max_output_tokens) is not int
        or not 1 <= settings.max_output_tokens <= 8192
        or type(tokens) is not int
        or not 1 <= tokens <= settings.max_output_tokens
    ):
        raise ModelError("单次输出上限超出模型配置范围")
    return tokens


class ModelAudit:
    """Sanitize only configured secrets/endpoints, preserving response text structure."""

    def __init__(self, settings: ModelSettings):
        self._settings = settings
        try:
            host = urlsplit(settings.base_url).hostname
            literals = {settings.api_key, settings.base_url, settings.base_url + "/chat/completions"}
            variants = set()
            for value in literals:
                if not isinstance(value, str) or not value:
                    raise ValueError
                variants.add(value)
                for ascii_only in (True, False):
                    escaped = json.dumps(value, ensure_ascii=ascii_only)[1:-1]
                    variants.update((escaped, escaped.replace("/", "\\/")))
            patterns = [(len(value), re.escape(value)) for value in variants]
            if host:
                patterns.append((len(host), "(?i:" + re.escape(host) + ")"))
            # One substitution avoids redacting pieces of the replacement marker.
            self._private = re.compile("|".join(p for _, p in sorted(patterns, reverse=True)))
        except (AttributeError, TypeError, ValueError):
            raise ModelError("模型脱敏配置无效") from None

    def text(self, value: str) -> str:
        if not isinstance(value, str):
            raise ModelError("模型审计文本必须为字符串")
        return self._private.sub("[REDACTED]", value)

    def messages(self, messages: list[ChatMessage]) -> list[dict[str, str]]:
        if not isinstance(messages, list) or not messages or any(
            not isinstance(item, ChatMessage)
            or item.role not in ("system", "user", "assistant")
            or not isinstance(item.content, str)
            or not item.content.strip()
            for item in messages
        ):
            raise ModelError("模型输入必须是非空的标准消息列表")
        return [{"role": item.role, "content": self.text(item.content)} for item in messages]

    def configuration(
        self, model_ref: str, *, max_output_tokens: int | None = None, json_mode: bool = False
    ) -> dict:
        settings = self._settings
        if (
            not isinstance(model_ref, str)
            or not model_ref.strip()
            or type(json_mode) is not bool
            or type(settings.timeout_seconds) is not int
            or not 1 <= settings.timeout_seconds <= 300
            or type(settings.temperature) not in (int, float)
            or not 0 <= settings.temperature <= 2
            or not isinstance(settings.options, dict)
        ):
            raise ModelError("模型审计配置参数无效")
        options = {}
        if "enable_thinking" in settings.options:
            thinking = settings.options["enable_thinking"]
            if type(thinking) is not bool:
                raise ModelError("模型审计配置参数无效")
            options["enable_thinking"] = thinking
        return {
            "model_ref": self.text(model_ref),
            "provider": self.text(settings.provider),
            "model": self.text(settings.model),
            "timeout_seconds": settings.timeout_seconds,
            "max_output_tokens": validated_output_tokens(settings, max_output_tokens),
            "temperature": settings.temperature,
            "options": options,
            "json_mode": json_mode,
        }

    def request(
        self, model_ref: str, messages: list[ChatMessage], *,
        max_output_tokens: int | None = None, json_mode: bool = False,
    ) -> dict:
        return {
            "configuration": self.configuration(
                model_ref, max_output_tokens=max_output_tokens, json_mode=json_mode
            ),
            "messages": self.messages(messages),
        }

    def result(self, model_result: ModelResult) -> ModelResult:
        if not isinstance(model_result, ModelResult) or not isinstance(model_result.usage, dict):
            raise ModelError("模型审计结果格式无效")
        return ModelResult(
            self.text(model_result.text),
            self.text(model_result.model),
            self.text(model_result.finish_reason),
            {
                key: value for key, value in model_result.usage.items()
                if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
                and type(value) is int and value >= 0
            },
        )
