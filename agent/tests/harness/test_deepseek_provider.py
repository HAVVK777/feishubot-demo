"""DeepSeek 适配层：中立消息 → OpenAI 兼容请求，响应 → 中立消息。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from feishubot_agent.harness.errors import ModelError
from feishubot_agent.harness.model import (
    AssistantMessage,
    ToolCall,
    ToolResult,
    ToolResultsMessage,
    ToolSpec,
    Usage,
    UserMessage,
)
from feishubot_agent.harness.providers.deepseek import DeepSeekModelClient


class FakeCompletions:
    def __init__(self, response) -> None:
        self.response = response
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FakeChat:
    def __init__(self, response) -> None:
        self.completions = FakeCompletions(response)


class FakeClient:
    def __init__(self, response) -> None:
        self.chat = FakeChat(response)
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _tool_call(call_id: str = "t1", name: str = "search", arguments: str = '{"q": "x"}'):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _response(
    *,
    content="hello",
    tool_calls=None,
    finish_reason="stop",
    prompt_tokens=10,
    completion_tokens=5,
):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=tool_calls),
                finish_reason=finish_reason,
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        ),
    )


async def test_response_is_converted_to_neutral_message() -> None:
    client = FakeClient(
        _response(
            content="先说一句",
            tool_calls=[_tool_call()],
            finish_reason="tool_calls",
        )
    )
    provider = DeepSeekModelClient(model="deepseek-flash", api_key="key", client=client)

    result = await provider.complete(system="sys", messages=[UserMessage("hi")], tools=[])

    assert result.text == "先说一句"
    assert result.tool_calls == (ToolCall("t1", "search", {"q": "x"}),)
    assert result.stop_reason == "tool_use"
    assert result.usage == Usage(input_tokens=10, output_tokens=5)

    request = client.chat.completions.calls[0]
    assert request["model"] == "deepseek-flash"
    assert request["max_tokens"] == 16_000
    assert request["reasoning_effort"] == "high"
    assert request["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]
    assert "tools" not in request


async def test_messages_and_tools_are_converted() -> None:
    client = FakeClient(_response())
    provider = DeepSeekModelClient(model="deepseek-flash", api_key="key", client=client)

    await provider.complete(
        system="sys",
        messages=[
            UserMessage("hi"),
            AssistantMessage(
                text="",
                tool_calls=(ToolCall("t1", "search", {"q": "x"}),),
                stop_reason="tool_use",
            ),
            ToolResultsMessage(
                results=(
                    ToolResult("t1", "search", "ok"),
                    ToolResult("t2", "fetch", "bad", is_error=True),
                )
            ),
        ],
        tools=[ToolSpec(name="search", description="检索", input_schema={"type": "object"})],
    )

    request = client.chat.completions.calls[0]
    assert request["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "t1",
                    "type": "function",
                    "function": {
                        "name": "search",
                        "arguments": json.dumps({"q": "x"}, ensure_ascii=False),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "ok"},
        {"role": "tool", "tool_call_id": "t2", "content": "[工具错误] bad"},
    ]
    assert request["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "search",
                "description": "检索",
                "parameters": {"type": "object"},
            },
        }
    ]


@pytest.mark.parametrize(
    ("finish_reason", "expected"),
    [
        ("stop", "end_turn"),
        ("tool_calls", "tool_use"),
        ("length", "max_tokens"),
        ("content_filter", "refusal"),
        ("insufficient_system_resource", "other"),
        (None, "other"),
    ],
)
async def test_finish_reasons_are_mapped(finish_reason, expected) -> None:
    client = FakeClient(_response(finish_reason=finish_reason))
    provider = DeepSeekModelClient(model="deepseek-flash", api_key="key", client=client)

    result = await provider.complete(system="sys", messages=[UserMessage("hi")], tools=[])

    assert result.stop_reason == expected


async def test_invalid_tool_arguments_raise_model_error() -> None:
    client = FakeClient(
        _response(
            content="",
            tool_calls=[_tool_call(arguments="{not json")],
            finish_reason="tool_calls",
        )
    )
    provider = DeepSeekModelClient(model="deepseek-flash", api_key="key", client=client)

    with pytest.raises(ModelError, match="不是合法 JSON"):
        await provider.complete(system="sys", messages=[UserMessage("hi")], tools=[])


async def test_reasoning_effort_is_configurable() -> None:
    client = FakeClient(_response())
    provider = DeepSeekModelClient(
        model="deepseek-flash",
        api_key="key",
        reasoning_effort="none",
        client=client,
    )

    await provider.complete(system="sys", messages=[UserMessage("hi")], tools=[])

    assert client.chat.completions.calls[0]["reasoning_effort"] == "none"


async def test_aclose_closes_the_sdk_client() -> None:
    client = FakeClient(_response())
    provider = DeepSeekModelClient(model="deepseek-flash", api_key="key", client=client)

    await provider.aclose()

    assert client.closed is True
