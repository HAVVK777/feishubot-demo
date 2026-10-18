"""端到端冒烟：起一个真的 HTTP 服务，用真的 MCP 客户端连上去。

pytest 那套是进程内直连（快、覆盖协议往返），但覆盖不到传输层——uvicorn 的
lifespan、鉴权中间件、Host 头校验。这个脚本补的就是那一段。

客户端部分直接调 `agent_client.McpToolbox`（调用方的参考实现），**不是另写一份**：
这样那份示例每次冒烟都被跑一遍，不会悄悄腐烂成过时的代码。用两个不同名字的 token
各连一次并调 `whoami`，因为"调用方是谁"这条链路（中间件 → scope["user"] →
contextvar → 工具）在进程内测不出来。

    uv run python scripts/smoke_http.py

它自己起服务、自己收尾，不需要另开终端。退出码非 0 就是哪一步没过。
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time

import httpx2
from agent_client import McpConnectionError, McpToolbox

#: 故意用假 token：这个脚本不碰任何真实凭据，跑完就扔。
#: 两个是为了顺带验出"名字确实分得清"——一个 token 表里放两个调用方。
CALLERS = {"smoke-a": "smoke-test-token-a", "smoke-b": "smoke-test-token-b"}

#: 表里没有的 token，用来验拒绝。
WRONG_TOKEN = "not-in-the-table"

#: 限流额度。调小是为了让冒烟能在几次调用内打到限，别用线上的量级。
RATE_LIMIT_PER_MINUTE = 2

#: 等服务起来的上限。uvicorn 冷启动通常不到 1 秒，给足余量是为了慢机器上别误报。
STARTUP_TIMEOUT_SECONDS = 15.0


def _free_port() -> int:
    """让内核挑一个空闲端口，避免撞上开发时正开着的实例。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_until_healthy(base_url: str) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            if httpx2.get(f"{base_url}/healthz").status_code == 200:
                return
        except httpx2.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"{STARTUP_TIMEOUT_SECONDS:.0f} 秒内没起来：{base_url}/healthz")


def main() -> int:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "MCP_HOST": "127.0.0.1",
        "MCP_PORT": str(port),
        "MCP_AUTH_TOKENS": ",".join(f"{name}:{token}" for name, token in CALLERS.items()),
        "MCP_RATE_LIMIT_PER_MINUTE": str(RATE_LIMIT_PER_MINUTE),
    }

    server = subprocess.Popen([sys.executable, "-m", "feishubot_mcp"], env=env)
    try:
        _wait_until_healthy(base_url)
        print(f"✓ 服务起来了：{base_url}/mcp")

        _check_health(base_url)
        _check_unauthorized(base_url)
        _check_response_format(base_url)
        _check_client(base_url)
        _check_rate_limit(base_url)
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    print("\n全部通过。")
    return 0


def _check_health(base_url: str) -> None:
    payload = httpx2.get(f"{base_url}/healthz").json()
    assert payload["status"] == "ok", payload
    print(f"✓ 探活不需要 token：{payload}")


def _check_response_format(base_url: str) -> None:
    """成功的响应是**纯 JSON**，不套 SSE 的 event/data 外壳。

    这是 `server.build_http_app` 里 `json_response=True` 的效果，是个**有意的取舍**
    （换来的是关掉了调用过程中的进度推送）。钉在这里，免得哪天被无声改回默认。
    """
    _, token = next(iter(CALLERS.items()))
    response = httpx2.post(
        f"{base_url}/mcp",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "smoke", "version": "0"},
            },
        },
    )
    content_type = response.headers.get("content-type", "")
    assert content_type.startswith("application/json"), f"响应不是纯 JSON，而是 {content_type!r}"
    assert response.json()["result"]["serverInfo"]["name"] == "feishubot-mcp"
    print(f"✓ 成功的响应是纯 JSON（{content_type}），没有 SSE 外壳")


def _check_unauthorized(base_url: str) -> None:
    for label, headers in (
        ("没带 token", {}),
        ("token 不在表里", {"Authorization": f"Bearer {WRONG_TOKEN}"}),
    ):
        response = httpx2.post(f"{base_url}/mcp", json={}, headers=headers)
        assert response.status_code == 401, f"{label}竟然放行了：HTTP {response.status_code}"
    print("✓ 没带 token / token 不在表里的请求都被挡在 401")


def _check_client(base_url: str) -> None:
    async def run() -> None:
        for name, token in CALLERS.items():
            toolbox = McpToolbox(f"{base_url}/mcp", token)
            tool_names = await toolbox.list_tools()
            assert "whoami" in tool_names, tool_names

            # 这一条是整条链路（中间件 → scope → contextvar → 工具）的证明：
            # 拿到的是 token 表里的名字，说明装配顺序和归因都对。
            seen = await toolbox.call("whoami", {})
            assert seen == name, f"调用方应该是 {name!r}，工具里看到的是 {seen!r}"
            print(f"✓ 调用方 {name}：连上了，工具里认出自己是 {seen!r}")

        # 参考实现要把"没通过鉴权"归到 McpConnectionError（而不是抛裸的 ExceptionGroup）
        # ——调用方靠这个区分"能回给用户的工具报错"和"回一句服务不可用"。
        try:
            await McpToolbox(f"{base_url}/mcp", WRONG_TOKEN).list_tools()
        except McpConnectionError as exc:
            assert "未授权" in str(exc), str(exc)
            print(f"✓ 不在表里的 token：{exc}")
        else:
            raise AssertionError("不在表里的 token 竟然连上了")

    asyncio.run(run())


def _check_rate_limit(base_url: str) -> None:
    """限流真的装在服务上了。

    逻辑本身在 `tests/test_middleware.py` 里测得细；这里只验**它确实挂上了 server**——
    单测里 `build_middleware` 是对的，不代表 `build_server` 装配时没漏掉它。
    """

    async def run() -> None:
        toolbox = McpToolbox(f"{base_url}/mcp", CALLERS["smoke-b"])
        # 额度是 RATE_LIMIT_PER_MINUTE，前面的检查已经用掉一些，所以连打到被拒为止。
        for _ in range(RATE_LIMIT_PER_MINUTE + 2):
            try:
                await toolbox.call("whoami", {})
            except McpConnectionError as exc:
                assert "调用过于频繁" in str(exc), exc
                print(f"✓ 超过额度被限流：{exc}")
                return
        raise AssertionError("连打这么多次都没触发限流，中间件是不是没装上？")

    asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
