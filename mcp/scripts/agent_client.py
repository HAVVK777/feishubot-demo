"""调用方（agent）这边怎么连这个 MCP 服务——参考实现，可以直接跑。

    MCP_TOKEN=<token> uv run python scripts/agent_client.py
    MCP_URL=http://127.0.0.1:8765/mcp MCP_TOKEN=<token> uv run python scripts/agent_client.py

演示的是最小可用的一套：带 token 连接 → 列工具 → 调一个工具 → 把结果取成文本。
真正接进调用方时，对应的是"收到一条请求 → 决定调哪个工具 → 拿到文本 → 回给用户"
中间那一段。

## 关于连接的生命周期

这里**每次调用都新建一个会话**（连接 → initialize → 调用 → 关闭）。对"来一条消息
处理一条"的调用方（比如飞书机器人），本地握手的开销是毫秒级，换来的是**没有需要维护
的长连接状态**——服务端重启不会让调用方卡在一个僵死的会话上。

要长连接的话，把 `Client` 实例养在进程里、断线时重建即可。但那是在调用频率高到
握手开销看得见的时候才值得的优化，代价是要自己处理重连、并发和生命周期。
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TypeVar

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

DEFAULT_URL = "http://127.0.0.1:8765/mcp"

#: 单次调用的上限。工具本身不设超时（见 README「服务端的并发上限」），所以调用方
#: 必须设一个——否则对面卡住，这边会一直等。
DEFAULT_TIMEOUT_SECONDS = 30.0

T = TypeVar("T")


class McpToolError(RuntimeError):
    """工具**有意**报回来的失败（`ToolError`）。

    消息是写给人看的，可以直接回给用户。
    """


class McpConnectionError(RuntimeError):
    """除了"工具自己报错"之外的一切失败：连不上、token 不对、被限流、超时、对面挂了。

    这类不给用户看细节，回一句"服务暂时不可用"就够，原文进日志。

    `data` 是服务端给的结构化失败信息（`kind` / `scope` / `may_still_be_running` 等）。
    **重试决策靠它，不靠解析 message**——消息是给人看的，措辞随时会改。
    """

    def __init__(self, message: str, *, data: dict | None = None) -> None:
        super().__init__(message)
        self.data = data or {}


class McpToolbox:
    """一个 MCP 服务的调用入口。"""

    def __init__(
        self,
        url: str = DEFAULT_URL,
        token: str = "",
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._url = url
        self._token = token
        self._timeout = timeout_seconds

    async def list_tools(self) -> list[str]:
        """列出对面提供了哪些工具。用来做自检，或者把工具清单喂给选工具的那一步。"""
        result = await self._run(lambda client: client.list_tools())
        return [tool.name for tool in result.tools]

    async def call(
        self,
        tool: str,
        arguments: dict,
        *,
        session_id: str | None = None,
        chat_type: str | None = None,
    ) -> str:
        """调一个工具，返回它给的文本。

        `session_id` 是**这次对话**的标识：每个飞书会话一个，同一个会话的多次调用用
        同一个值。`chat_type` 是会话类型，取值跟飞书一致：`"p2p"`（单聊）或
        `"group"`（群聊）——服务端要据此决定用哪套额度（群聊的额度是全群共享的）。

        两个都随 `_meta` 发过去，服务端用它们做归因和限流——所以**必须稳定**，
        每次请求现生成一个的话，限流等于没限。服务端配了对应额度时，不带会被拒绝
        （拒绝消息里会说清要带什么）。
        """
        meta = {k: v for k, v in (("session_id", session_id), ("chat_type", chat_type)) if v}
        result = await self._run(
            lambda client: client.call_tool(tool, arguments, meta=meta or None)
        )

        text = _text(result)
        if result.is_error:
            # 工具报错：这句话是工具作者写给模型/用户看的，原样带出去。
            raise McpToolError(text or f"工具 {tool} 返回了错误但没有说明")
        return text

    async def _run(self, action: Callable[[Client], Awaitable[T]]) -> T:
        """所有调用的公共外壳：开会话、跑动作、把基础设施类的失败归一。

        包装放在**会话这一层**而不是各别方法里，因为连接失败（连不上、token 不对、
        对面没起来）是在进入会话时就发生的，根本没走到具体方法那一步。
        """
        try:
            async with self._session() as client:
                return await action(client)
        except McpToolError:
            raise
        except Exception as exc:
            # 其余的全部归一成"调用失败"。客户端 SDK 把错误埋在 asyncio/anyio 的
            # ExceptionGroup 里，直接抛出去调用方只会看到一句
            # "unhandled errors in a TaskGroup"，看不出是 401 还是对面没起来。
            #
            # 用**剥到最里面那层**做 cause：结构化数据（`MCPError.data`）在那一层上，
            # 用它当 cause 才能让 `except ... as e: e.__cause__.data` 读得到。
            #
            # 捕 `Exception` 而不是 `BaseException`：Ctrl-C 得能正常打断，不该被
            # 变成一句"服务不可用"。
            leaf = _leaf(exc)
            raise McpConnectionError(
                str(leaf) or type(leaf).__name__,
                data=getattr(leaf, "data", None),
            ) from leaf

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[Client]:
        """开一个会话。

        `httpx2` 是 mcp 自带的 HTTP 客户端（它没依赖 httpx）。要带自定义请求头就
        必须自己建一个传进去——`Client("http://...")` 那个简写没法带 header。
        """
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        async with httpx2.AsyncClient(headers=headers, timeout=self._timeout) as http:
            transport = streamable_http_client(self._url, http_client=http)
            async with Client(transport, read_timeout_seconds=self._timeout) as client:
                yield client


def _text(result) -> str:
    return "\n".join(block.text for block in result.content if getattr(block, "type", "") == "text")


def _leaf(exc: BaseException) -> BaseException:
    """把 SDK 套的那几层 ExceptionGroup 剥到最里面那层。

    最里面那层才是带信息的那层：`MCPError` 的 `data` 就在它身上。只要字符串的话
    `str(_leaf(exc))` 就够了；这里返回异常本身，是因为调用方不止要消息。
    """
    seen: set[int] = set()
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions and id(exc) not in seen:
        seen.add(id(exc))
        exc = exc.exceptions[0]
    return exc


async def main() -> int:
    token = os.environ.get("MCP_TOKEN", "")
    if not token:
        print("先设 MCP_TOKEN（服务端 MCP_AUTH_TOKENS 里那一行冒号后面的值）", file=sys.stderr)
        return 2

    # 会话标签。真实用法里**每个飞书会话一个**，同一个会话的多次调用用同一个值——
    # 服务端按它做归因和限流，所以不能每次现生成。这里给个固定值是为了能直接跑。
    session = os.environ.get("MCP_SESSION_ID", "demo-session")
    chat = os.environ.get("MCP_CHAT_TYPE", "p2p")

    toolbox = McpToolbox(os.environ.get("MCP_URL", DEFAULT_URL), token)
    try:
        print(f"工具列表：{'、'.join(await toolbox.list_tools())}")
        print(f"whoami → {await toolbox.call('whoami', {}, session_id=session, chat_type=chat)}")
        said = await toolbox.call("ping", {"text": "连通性 OK"}, session_id=session, chat_type=chat)
        print(f"ping   → {said}")
        print(f"（会话：{session}，类型：{chat}）")
    except McpConnectionError as exc:
        print(f"连不上或没通过鉴权：{exc}", file=sys.stderr)
        return 1
    except McpToolError as exc:
        print(f"工具报错：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
