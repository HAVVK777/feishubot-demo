"""模型中立的消息类型与 `ModelClient` 协议。

`loop.py` 只依赖这里的类型，不 import 任何模型 SDK。具体 provider 负责把这些中立
类型翻译成 SDK 的请求形状。

**`AssistantMessage.provider_state` 是一个刻意的例外。** 有些 provider 需要把上一轮
的原始响应原样回传（例如 Anthropic 的 thinking blocks）。harness 不解释这个字段，
只把它随历史一起保存、原样回传；不需要它的 provider（比如当前的 DeepSeek）留空即可。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal", "other"]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """模型看到的工具定义。描述里应该已经带上 hints（见 `tools.with_hints`）。"""

    name: str
    description: str
    input_schema: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class UserMessage:
    text: str


@dataclass(frozen=True, slots=True)
class AssistantMessage:
    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: StopReason = "end_turn"
    usage: Usage | None = None
    #: provider 私有续接数据。harness 只保存和回传，不读取。
    provider_state: Any = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class ToolResultsMessage:
    """一次模型回复里所有工具结果。必须作为**一条**消息回传。"""

    results: tuple[ToolResult, ...]


Message = UserMessage | AssistantMessage | ToolResultsMessage


class ModelClient(Protocol):
    """模型客户端。实现方持有模型 id、max_tokens、SDK 客户端等配置。"""

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
    ) -> AssistantMessage:
        """请求一次模型补全，返回文本、工具调用和续接数据。"""
        ...
