"""工具和注册表的行为。

工具是普通函数，这里直接调——不需要 MCP 会话。这正是"工具不依赖 server 实例"
换来的好处：一个工具出错时，这个测试文件能把它单独钉住。
"""

from __future__ import annotations

import types

import pytest
from mcp import Client
from mcp.types import ToolAnnotations

from feishubot_mcp.server import build_server
from feishubot_mcp.tools import MODULES, hints, register_all
from feishubot_mcp.tools.example import add, ping
from feishubot_mcp.tools.hints import annotations_of, describe


async def test_ping_echoes_its_argument():
    assert await ping("你好") == "你好"


async def test_add_returns_the_sum():
    assert await add(2, 3) == 5


def test_every_listed_module_declares_tools():
    """清单里写了模块、模块里却没写 TOOLS，是加工具时最容易漏的一步。"""
    for module in MODULES:
        assert module.TOOLS, f"{module.__name__} 里一个工具都没有"


def test_duplicate_tool_names_refuse_to_start():
    """重名必须**拒绝启动**，不能只是 warning。

    真跑起来的话，被盖掉的那个名字调起来会打到另一个实现上——现象是"这个工具
    返回值不对"，没人会想到是重名。
    """
    fake = types.ModuleType("fake")
    fake.TOOLS = (ping, ping)  # 同名函数，模拟两个模块里的同名工具

    with pytest.raises(RuntimeError, match="工具重名：ping"):
        register_all(build_server(), modules=(fake,))


def test_registering_nothing_is_survivable():
    """空清单不该崩——它只是让客户端看到一张空列表（并且会打一条 warning）。"""
    register_all(build_server(), modules=())


async def test_hints_reach_the_client():
    """声明要真的出现在 `tools/list` 里。

    调用方据它决定能不能重试，模型也看得到——只挂在函数上而没传出去等于没声明。
    """
    async with Client(build_server()) as client:
        result = await client.list_tools()

    ping = next(tool for tool in result.tools if tool.name == "ping")
    assert ping.annotations is not None
    assert ping.annotations.read_only_hint is True


def test_hints_can_be_stacked():
    """分成几行写的时候按字段合并，不是后者盖掉前者。"""

    @hints(read_only=True)
    @hints(open_world=True)
    async def tool() -> str:
        """叠着声明。"""
        return "x"

    declared = annotations_of(tool)
    assert declared is not None
    assert declared.read_only_hint is True
    assert declared.open_world_hint is True


async def test_declaring_hints_leaves_a_plain_function():
    """声明只是往函数上挂了个属性——工具还是普通函数，照样直接调、直接单测。"""

    @hints(idempotent=True)
    async def tool(text: str) -> str:
        """原样返回。"""
        return text

    assert await tool("你好") == "你好"


@pytest.mark.parametrize(
    ("annotations", "expected"),
    [
        (None, "未声明"),
        (ToolAnnotations(read_only_hint=True), "只读"),
        (ToolAnnotations(idempotent_hint=True), "有副作用、可重复调用"),
        (
            ToolAnnotations(idempotent_hint=True, destructive_hint=True),
            "有副作用、可重复调用、破坏性",
        ),
        (ToolAnnotations(), "有副作用、不可重复调用"),
    ],
)
def test_describe_says_what_the_startup_log_should_say(
    annotations: ToolAnnotations | None, expected: str
):
    assert describe(annotations) == expected
