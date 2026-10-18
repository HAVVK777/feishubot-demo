"""MCP 客户端的纯逻辑：错误归一、结果取文本、会话标签绑定。"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import httpx2
import pytest

from feishubot_agent.harness.errors import ToolCallError
from feishubot_agent.harness.mcp_client import (
    McpToolClient,
    SessionToolExecutor,
    _leaf,
    _result_text,
    _to_tool_error,
)
from feishubot_agent.harness.model import ToolCall, ToolResult


class _McpError(Exception):
    def __init__(self, message: str, data: dict) -> None:
        super().__init__(message)
        self.data = data


def test_connect_error_is_retryable() -> None:
    error = _to_tool_error(httpx2.ConnectError("refused"))

    assert error.kind == "connection"
    assert error.retryable is True


def test_read_timeout_is_not_retryable() -> None:
    error = _to_tool_error(httpx2.ReadTimeout("timed out"))

    assert error.kind == "timeout"
    assert error.retryable is False


def test_network_error_is_not_blindly_retried() -> None:
    error = _to_tool_error(httpx2.ReadError("reset"))

    assert error.kind == "connection"
    assert error.retryable is False


@pytest.mark.parametrize(
    ("kind", "retryable"),
    [
        ("rate_limited", True),
        ("timeout", False),
        ("unauthorized", False),
        ("missing_meta", False),
    ],
)
def test_structured_kinds_take_precedence(kind: str, retryable: bool) -> None:
    error = _to_tool_error(_McpError("boom", {"kind": kind}))

    assert error.kind == kind
    assert error.retryable is retryable


def test_rate_limit_carries_a_retry_hint() -> None:
    error = _to_tool_error(_McpError("too fast", {"kind": "rate_limited", "limit_per_minute": 60}))

    assert error.retry_after_seconds == 1.0


def test_rate_limit_without_a_limit_has_no_retry_hint() -> None:
    error = _to_tool_error(_McpError("too fast", {"kind": "rate_limited"}))

    assert error.retry_after_seconds is None


def test_leaf_unwraps_exception_groups() -> None:
    inner = ValueError("inner")
    grouped = ExceptionGroup("outer", [ExceptionGroup("middle", [inner])])

    assert _leaf(grouped) is inner


def test_result_text_renders_text_and_marks_other_blocks() -> None:
    result = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="hello"),
            SimpleNamespace(type="image"),
        ],
        structured_content=None,
    )

    assert _result_text(result) == "hello\n[image 类型的内容未渲染]"


def test_result_text_falls_back_to_structured_content() -> None:
    result = SimpleNamespace(content=[], structured_content={"a": 1})

    assert _result_text(result) == '{"a": 1}'


def test_result_text_can_be_empty() -> None:
    result = SimpleNamespace(content=[], structured_content=None)

    assert _result_text(result) == ""


async def test_list_tools_renders_hints(monkeypatch) -> None:
    tools_result = SimpleNamespace(
        tools=[
            SimpleNamespace(
                name="search",
                description="检索",
                input_schema={"type": "object"},
                annotations=SimpleNamespace(
                    read_only_hint=True,
                    idempotent_hint=None,
                    destructive_hint=None,
                ),
            )
        ]
    )

    async def fake_run(action):
        return tools_result

    client = McpToolClient(url="http://example/mcp")
    monkeypatch.setattr(client, "_run", fake_run)

    specs = await client.list_tools()

    assert len(specs) == 1
    assert specs[0].name == "search"
    assert specs[0].description == "检索（只读）"


async def test_call_converts_tool_error(monkeypatch) -> None:
    result = SimpleNamespace(content=[], structured_content=None, is_error=True)

    async def fake_run(action):
        return result

    client = McpToolClient(url="http://example/mcp")
    monkeypatch.setattr(client, "_run", fake_run)

    output = await client.call(ToolCall("t1", "search", {}))

    assert output.is_error is True
    assert "search" in output.content


async def test_call_logs_the_server_reported_execution_time(monkeypatch, caplog) -> None:
    result = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="ok")],
        structured_content=None,
        is_error=False,
        meta={"execution_time_ms": 123.4},
    )

    async def fake_run(action):
        return result

    client = McpToolClient(url="http://example/mcp")
    monkeypatch.setattr(client, "_run", fake_run)

    with caplog.at_level(logging.INFO, logger="feishubot_agent.harness.mcp_client"):
        await client.call(ToolCall("t1", "search", {}))

    assert "服务端 123 ms" in caplog.text


async def test_call_tags_the_error_with_the_tool_name(monkeypatch) -> None:
    async def fake_run(action):
        raise ToolCallError("timeout", kind="timeout", retryable=False)

    client = McpToolClient(url="http://example/mcp")
    monkeypatch.setattr(client, "_run", fake_run)

    with pytest.raises(ToolCallError) as raised:
        await client.call(ToolCall("t1", "search", {}))

    assert raised.value.tool == "search"


async def test_session_executor_binds_the_labels() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.received: tuple | None = None

        async def call(self, call, *, session_id, chat_type) -> ToolResult:
            self.received = (call.name, session_id, chat_type)
            return ToolResult(call.id, call.name, "ok")

    fake = FakeClient()
    executor = SessionToolExecutor(fake, session_id="oc_1", chat_type="group")

    result = await executor.call(ToolCall("t1", "search", {}))

    assert fake.received == ("search", "oc_1", "group")
    assert result.content == "ok"
