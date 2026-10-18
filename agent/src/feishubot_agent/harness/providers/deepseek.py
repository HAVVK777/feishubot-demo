"""DeepSeek 适配层（OpenAI 兼容 API）。

这是整个 agent 里**唯一** import `openai` 的地方。DeepSeek 的 OpenAI 兼容端点是
`https://api.deepseek.com`。默认用 `deepseek-v4-pro`：本机实测 `deepseek-flash`
多次出现输出拼接/乱码；需要低延迟时再自行切换。

和 Anthropic 的差异：

* 系统提示是 `messages` 里的第一条 `system` 消息，不是单独参数；
* 工具结果不是一条 user 消息里的多个 block，而是**每个结果一条** `tool` 消息；
* 工具调用参数在响应里是 JSON **字符串**，必须 `json.loads`，不能当对象用；
* 工具错误没有 `is_error` 字段，所以在 `tool` 消息内容前加 `[工具错误]`；
* 没有需要回传的 thinking blocks。`reasoning_content` 是输出，不放进下一轮输入。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

import openai

from feishubot_agent.harness.errors import ModelError
from feishubot_agent.harness.model import (
    AssistantMessage,
    Message,
    StopReason,
    ToolCall,
    ToolResultsMessage,
    ToolSpec,
    Usage,
    UserMessage,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_REASONING_EFFORT = "high"
REASONING_EFFORTS = frozenset({"none", "low", "high", "max"})

_STOP_REASONS: dict[str, StopReason] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "content_filter": "refusal",
}


class DeepSeekModelClient:
    """`ModelClient` 的 DeepSeek 实现。"""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        max_tokens: int = 16_000,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
        timeout_seconds: float | None = None,
        client: openai.AsyncOpenAI | None = None,
    ) -> None:
        if not model:
            raise ValueError("model 不能为空")
        if max_tokens <= 0:
            raise ValueError("max_tokens 必须大于 0")
        if reasoning_effort not in REASONING_EFFORTS:
            raise ValueError("reasoning_effort 必须是 " + " / ".join(sorted(REASONING_EFFORTS)))
        if client is None and not api_key:
            raise ValueError("api_key 不能为空")

        self._model = model
        self._max_tokens = max_tokens
        self._reasoning_effort = reasoning_effort
        self._client = (
            client
            if client is not None
            else openai.AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout_seconds,
            )
        )

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
    ) -> AssistantMessage:
        request: dict[str, Any] = {
            "model": self._model,
            "messages": _to_openai_messages(system, messages),
            "max_tokens": self._max_tokens,
            "reasoning_effort": self._reasoning_effort,
        }
        if tools:
            request["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": dict(spec.input_schema),
                    },
                }
                for spec in tools
            ]

        try:
            response = await self._client.chat.completions.create(**request)
        except openai.RateLimitError as exc:
            raise ModelError("DeepSeek 接口限流，请稍后重试") from exc
        except openai.APIStatusError as exc:
            # 400/401/404 的正文通常直接说明原因（模型名写错、key 不对），
            # 带上它才能从日志里看出来；这条消息只进日志，不会回给用户。
            detail = getattr(exc, "message", "") or str(exc)
            raise ModelError(f"DeepSeek 接口返回 {exc.status_code}：{detail}") from exc
        except openai.APIConnectionError as exc:
            raise ModelError(f"连不上 DeepSeek 接口：{exc}") from exc
        except openai.APIError as exc:
            raise ModelError(f"DeepSeek 接口调用失败：{type(exc).__name__}") from exc

        return _to_assistant(response)

    async def aclose(self) -> None:
        await self._client.close()


def _to_openai_messages(system: str, messages: Sequence[Message]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = [{"role": "system", "content": system}]

    for message in messages:
        if isinstance(message, UserMessage):
            converted.append({"role": "user", "content": message.text})
        elif isinstance(message, AssistantMessage):
            item: dict[str, Any] = {
                "role": "assistant",
                # 有 tool_calls 时 content 可以是 None；空字符串在某些兼容实现里
                # 会被当成"有内容但为空"，所以这里用 None。
                "content": message.text or None,
            }
            if message.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(dict(call.arguments), ensure_ascii=False),
                        },
                    }
                    for call in message.tool_calls
                ]
            converted.append(item)
        elif isinstance(message, ToolResultsMessage):
            for result in message.results:
                content = result.content
                if result.is_error:
                    # OpenAI 兼容协议没有 is_error 字段，只能把错误写进内容。
                    content = f"[工具错误] {content}"
                converted.append(
                    {
                        "role": "tool",
                        "tool_call_id": result.call_id,
                        "content": content,
                    }
                )
        else:  # pragma: no cover - 联合类型已经穷尽
            raise TypeError(f"未知的消息类型：{type(message).__name__}")

    return converted


def _to_assistant(response: Any) -> AssistantMessage:
    if not response.choices:
        raise ModelError("DeepSeek 没有返回任何 choice")

    choice = response.choices[0]
    message = choice.message
    text = message.content or ""

    tool_calls: list[ToolCall] = []
    for call in message.tool_calls or []:
        if call.type != "function":
            raise ModelError(f"DeepSeek 返回了不支持的工具类型：{call.type}")
        try:
            arguments = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError as exc:
            raise ModelError(f"DeepSeek 返回的工具参数不是合法 JSON：{call.function.name}") from exc
        if not isinstance(arguments, dict):
            raise ModelError(f"DeepSeek 返回的工具参数不是对象：{call.function.name}")
        tool_calls.append(ToolCall(id=call.id, name=call.function.name, arguments=arguments))

    stop_reason = _STOP_REASONS.get(choice.finish_reason, "other")
    if stop_reason == "refusal":
        logger.warning("DeepSeek 因内容过滤结束生成（finish_reason=content_filter）")

    usage = None
    if response.usage is not None:
        usage = Usage(
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
        )

    return AssistantMessage(
        text=text,
        tool_calls=tuple(tool_calls),
        stop_reason=stop_reason,
        usage=usage,
    )


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_REASONING_EFFORT",
    "REASONING_EFFORTS",
    "DeepSeekModelClient",
]
