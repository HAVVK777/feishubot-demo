"""端到端：用 SDK 自带的客户端连上 server，断言工具真的能用。

`Client(server)` 传实例时走**内存流**，不起进程、不占端口，所以跑得和普通单测一样
快；但它跑的是完整的协议往返（initialize → tools/list → tools/call），不是直接调
函数。真正的 HTTP 那一层在 `scripts/smoke_http.py` 里验（要起进程，不进测试套件）。
"""

from __future__ import annotations

import logging
from typing import Any

from mcp import Client

from feishubot_mcp.errors import ToolError
from feishubot_mcp.server import build_server


def _text(result: Any) -> str:
    """把返回内容里的文本块拼起来——模型看到的就是这些字。"""
    return "\n".join(block.text for block in result.content if getattr(block, "type", "") == "text")


async def test_lists_the_registered_tools():
    async with Client(build_server()) as client:
        result = await client.list_tools()

    names = {tool.name for tool in result.tools}
    assert {"ping", "add"} <= names


async def test_tool_description_comes_from_the_docstring():
    """描述是模型选工具的唯一依据，必须真的传过去。"""
    async with Client(build_server()) as client:
        result = await client.list_tools()

    ping = next(tool for tool in result.tools if tool.name == "ping")
    assert "连通性自检" in (ping.description or "")


async def test_calling_a_tool_returns_its_result():
    async with Client(build_server()) as client:
        result = await client.call_tool("add", {"a": 2, "b": 3})

    assert result.is_error is False
    assert "5" in _text(result)


async def test_tool_result_carries_execution_time_in_meta():
    """耗时随响应返回，但只在 `_meta` 里，不属于模型看到的正文。"""
    async with Client(build_server()) as client:
        result = await client.call_tool("ping", {"text": "x"})

    assert result.meta is not None
    elapsed_ms = result.meta.get("execution_time_ms")
    assert isinstance(elapsed_ms, (int, float))
    assert elapsed_ms >= 0
    assert "execution_time_ms" not in _text(result)


async def test_arguments_are_validated_by_the_schema():
    """类型注解生成的 schema 真的在拦参数——这是"工具写错类型"的第一道防线。"""
    async with Client(build_server()) as client:
        result = await client.call_tool("add", {"a": "两", "b": 3})

    assert result.is_error is True


async def test_tool_error_message_reaches_the_model():
    """预期内的失败：抛 ToolError，那句话原样送到模型手里。"""
    server = build_server()

    async def lookup(city: str) -> str:
        """查天气。"""
        raise ToolError(f"查不到 {city} 的天气，换一个城市名试试")

    server.add_tool(lookup)

    async with Client(server) as client:
        result = await client.call_tool("lookup", {"city": "杭州"})

    assert result.is_error is True
    assert "查不到 杭州 的天气" in _text(result)


async def test_unexpected_crash_leaks_nothing(caplog):
    """没预料到的异常：模型只看到一句笼统的话，细节只进日志。

    这条断言是**整个报错约定成立的前提**——异常文本里带 token、SQL、内部主机名
    是常态，漏给调用方就等于泄露。
    """
    server = build_server()

    async def crash() -> str:
        """无论如何都会炸。"""
        raise RuntimeError("数据库密码是 hunter2")

    server.add_tool(crash)

    with caplog.at_level(logging.ERROR):
        async with Client(server) as client:
            result = await client.call_tool("crash", {})

    assert result.is_error is True
    assert "hunter2" not in _text(result)
    # 细节没丢，它在服务端日志里——排查时看这里，不是看客户端返回。
    assert "hunter2" in caplog.text
