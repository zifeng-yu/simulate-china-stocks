"""与供应商无关的模型输入/输出。无数据库、行情或执行依赖。"""

from dataclasses import dataclass, field
from typing import Literal, Protocol


class ModelError(RuntimeError):
    """可展示的、已去除凭据与原始 HTTP 响应的错误。"""


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class ModelResult:
    text: str
    model: str
    finish_reason: str
    usage: dict[str, int] = field(default_factory=dict)


class ModelClient(Protocol):
    def complete(
        self,
        messages: list[ChatMessage],
        *,
        max_output_tokens: int | None = None,
        json_mode: bool = False,
    ) -> ModelResult: ...
