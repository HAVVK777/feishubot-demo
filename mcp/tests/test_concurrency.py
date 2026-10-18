"""多个 agent 同时调工具时会怎样。

这里钉的不是"设计意图"，是**实测出来的行为**，而且刻意不用计时来断言——用"集合点"：
N 个调用必须同时在跑才能一起返回。要是哪天有人给工具加了一把全局锁、或者把同步
工具挪回事件循环上跑，这些测试会直接红，而不是在线上变成偶发的慢。

对应的说明在 README 的「多个 agent 同时调工具」。
"""

from __future__ import annotations

import asyncio
import threading

from mcp import Client

from feishubot_mcp.server import build_server

PARTIES = 5

#: 集合点的等待上限。到不齐就超时 → 工具报错 → 断言失败，而不是把测试挂死。
TIMEOUT_SECONDS = 5.0


async def test_async_tools_overlap_within_one_session():
    """同一个会话里的并发调用真的在重叠。

    服务端对同一个会话**不做串行**：MCP 的 streamable HTTP 允许一个会话里有多条
    在飞的请求。
    """
    server = build_server()
    arrived = 0
    all_here = asyncio.Event()

    async def rendezvous() -> str:
        """集合点：所有调用者都到齐了才一起返回。"""
        nonlocal arrived
        arrived += 1
        if arrived == PARTIES:
            all_here.set()
        await asyncio.wait_for(all_here.wait(), timeout=TIMEOUT_SECONDS)
        return "到齐了"

    server.add_tool(rendezvous)

    async with Client(server) as client:
        results = await asyncio.gather(
            *[client.call_tool("rendezvous", {}) for _ in range(PARTIES)]
        )

    assert [result.is_error for result in results] == [False] * PARTIES


async def test_async_tools_overlap_across_sessions():
    """不同 agent（不同会话）之间也是并发的，没有按会话排队。"""
    server = build_server()
    parties = 4
    arrived = 0
    all_here = asyncio.Event()

    async def rendezvous() -> str:
        """集合点：所有调用者都到齐了才一起返回。"""
        nonlocal arrived
        arrived += 1
        if arrived == parties:
            all_here.set()
        await asyncio.wait_for(all_here.wait(), timeout=TIMEOUT_SECONDS)
        return "到齐了"

    server.add_tool(rendezvous)

    async with Client(server) as first, Client(server) as second:
        results = await asyncio.gather(
            *[first.call_tool("rendezvous", {}) for _ in range(2)],
            *[second.call_tool("rendezvous", {}) for _ in range(2)],
        )

    assert [result.is_error for result in results] == [False] * parties


async def test_sync_tools_run_off_the_event_loop():
    """同步工具跑在工作线程上（SDK 走 `anyio.to_thread.run_sync`，默认上限 40）。

    这里用 `threading.Barrier`：只有真的在不同线程上跑，5 个调用才能到齐。要是它们
    被放在事件循环上执行，第一个等待就会把整个循环卡住，barrier 超时抛
    `BrokenBarrierError`，测试红。
    """

    def rendezvous() -> str:
        """集合点：所有调用者都到齐了才一起返回。"""
        barrier.wait(timeout=TIMEOUT_SECONDS)
        return "到齐了"

    server = build_server()
    barrier = threading.Barrier(PARTIES, timeout=TIMEOUT_SECONDS)
    server.add_tool(rendezvous)

    async with Client(server) as client:
        results = await asyncio.gather(
            *[client.call_tool("rendezvous", {}) for _ in range(PARTIES)]
        )

    assert [result.is_error for result in results] == [False] * PARTIES
