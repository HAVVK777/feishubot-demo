"""MCP 客户端适配。

这是 harness 里唯一 import MCP 的地方。它负责三件事：

1. 连上 MCP 服务（Streamable HTTP），列出工具；
2. 把 MCP 的 `Tool` 变成中立的 `ToolSpec`，并把 `annotations` 渲染进描述；
3. 调用工具，把协议错误归一成 `ToolCallError`。

连接策略和 `mcp/scripts/agent_client.py` 一致：**每次调用新建一个会话**。对
「来一条消息处理一条」的机器人，本地握手的开销是毫秒级，换来的是没有需要维护的
长连接状态——MCP 服务重启不会让 agent 卡在一个僵死的会话上。

`session_id` / `chat_type` 只是标签，用于 MCP 侧的归因和限流，不参与鉴权。真正的
身份来自 `AGENT_MCP_TOKEN`。它们由 app 层提供，harness 不解释业务含义。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import TypeVar

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from feishubot_agent.harness.errors import ToolCallError
from feishubot_agent.harness.model import ToolCall, ToolResult, ToolSpec
from feishubot_agent.harness.tools import with_hints

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 35.0

#: MCP 服务在 `CallToolResult._meta` 里返回的服务端执行耗时（毫秒）。
#: 与 `feishubot_mcp.timing.EXECUTION_TIME_META_KEY` 保持一致；只用于日志/分析，
#: **不能**放进 ToolResult.content，否则模型会把它当成工具结果的一部分。
EXECUTION_TIME_META_KEY = "execution_time_ms"

T = TypeVar("T")


class McpToolClient:
    """一个 MCP 服务的调用入口。"""

    def __init__(
        self,
        *,
        url: str,
        token: str = "",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        self._url = url
        self._token = token
        self._timeout = timeout_seconds

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        result = await self._run(lambda client: client.list_tools())

        specs: list[ToolSpec] = []
        for tool in result.tools:
            annotations = tool.annotations
            spec = ToolSpec(
                name=tool.name,
                description=tool.description or "",
                input_schema=tool.input_schema or {},
            )
            specs.append(
                with_hints(
                    spec,
                    read_only=getattr(annotations, "read_only_hint", None),
                    idempotent=getattr(annotations, "idempotent_hint", None),
                    destructive=getattr(annotations, "destructive_hint", None),
                )
            )
        return tuple(specs)

    async def call(
        self,
        call: ToolCall,
        *,
        session_id: str = "",
        chat_type: str = "",
    ) -> ToolResult:
        meta = {
            key: value
            for key, value in (("session_id", session_id), ("chat_type", chat_type))
            if value
        }
        started = time.monotonic()

        try:
            result = await self._run(
                lambda client: client.call_tool(
                    call.name,
                    dict(call.arguments),
                    meta=meta or None,
                )
            )
        except ToolCallError as exc:
            exc.tool = exc.tool or call.name
            logger.warning(
                "工具 %s 调用失败（kind=%s，%.0f ms）：%s",
                call.name,
                exc.kind,
                (time.monotonic() - started) * 1000,
                exc,
            )
            raise

        text = _result_text(result)
        elapsed_ms = (time.monotonic() - started) * 1000
        server_ms = _execution_time_of(result)
        server_timing = f"{server_ms:.0f} ms" if server_ms is not None else "未上报"

        if result.is_error:
            logger.warning(
                "工具 %s 返回 is_error（客户端 %.0f ms，服务端 %s）",
                call.name,
                elapsed_ms,
                server_timing,
            )
            content = text or f"工具 {call.name} 返回了错误但没有说明"
        else:
            logger.info(
                "工具 %s 完成（客户端 %.0f ms，服务端 %s，%d 字符）",
                call.name,
                elapsed_ms,
                server_timing,
                len(text),
            )
            content = text or "工具没有返回内容。"

        return ToolResult(
            call_id=call.id,
            name=call.name,
            content=content,
            is_error=bool(result.is_error),
        )

    async def aclose(self) -> None:
        """每次调用都自建会话，没有常驻连接需要关闭。"""

    async def _run(self, action: Callable[[Client], Awaitable[T]]) -> T:
        """所有调用的公共外壳：开会话、跑动作、把基础设施类的失败归一。"""
        try:
            async with self._session() as client:
                return await action(client)
        except ToolCallError:
            raise
        except Exception as exc:
            # SDK 把错误埋在 asyncio/anyio 的 ExceptionGroup 里；直接抛出去调用方只
            # 会看到 "unhandled errors in a TaskGroup"，看不出是 401 还是对面没起来。
            leaf = _leaf(exc)
            raise _to_tool_error(leaf) from leaf

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[Client]:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        async with httpx2.AsyncClient(headers=headers, timeout=self._timeout) as http:
            transport = streamable_http_client(self._url, http_client=http)
            async with Client(transport, read_timeout_seconds=self._timeout) as client:
                yield client


class SessionToolExecutor:
    """把一次对话的标签绑定到一个 `ToolExecutor` 上。

    app 层为每个回合创建一个，`loop.py` 看到的仍然只是 `executor.call(call)`。
    """

    def __init__(
        self,
        client: McpToolClient,
        *,
        session_id: str,
        chat_type: str,
    ) -> None:
        self._client = client
        self._session_id = session_id
        self._chat_type = chat_type

    async def call(self, call: ToolCall) -> ToolResult:
        return await self._client.call(
            call,
            session_id=self._session_id,
            chat_type=self._chat_type,
        )


def _execution_time_of(result) -> float | None:
    """从 `CallToolResult._meta` 里取服务端上报的执行耗时（毫秒）。

    读不到就返回 None：旧版 MCP 服务没有这个字段，调用方不该因此失败。
    """
    meta = getattr(result, "meta", None)
    if not isinstance(meta, Mapping):
        return None
    value = meta.get(EXECUTION_TIME_META_KEY)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _result_text(result) -> str:
    """把工具返回的 content blocks 取成文本。

    本项目的工具都返回文本；其他类型（图片、资源）不在这里渲染，留一个明确的占位符，
    避免模型以为工具什么都没返回。
    """
    parts: list[str] = []
    for block in result.content:
        block_type = getattr(block, "type", "")
        if block_type == "text":
            parts.append(block.text)
        else:
            parts.append(f"[{block_type or 'unknown'} 类型的内容未渲染]")

    text = "\n".join(part for part in parts if part)
    if text:
        return text

    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False, default=str)
    return ""


def _to_tool_error(exc: BaseException) -> ToolCallError:
    """按 MCP 的结构化 `data.kind` 和 HTTP 异常类型决定能否重试。"""
    data = getattr(exc, "data", None)
    kind = data.get("kind") if isinstance(data, dict) else None
    message = str(exc) or type(exc).__name__

    if kind == "rate_limited":
        limit = data.get("limit_per_minute") if isinstance(data, dict) else None
        retry_after = 60.0 / limit if isinstance(limit, (int, float)) and limit > 0 else None
        return ToolCallError(
            message,
            kind=kind,
            retryable=True,
            retry_after_seconds=retry_after,
        )
    if kind in {"timeout", "unauthorized", "missing_meta"}:
        return ToolCallError(message, kind=kind, retryable=False)

    if isinstance(exc, (httpx2.ConnectError, httpx2.ConnectTimeout)):
        # 连接都没建立，请求肯定没发出去，重试是安全的。
        return ToolCallError(message, kind="connection", retryable=True)
    if isinstance(exc, httpx2.TimeoutException):
        # 读/写超时：不知道请求是否已经到达工具，和 MCP 的 timeout 同类。
        return ToolCallError(message, kind="timeout", retryable=False)
    if isinstance(exc, httpx2.NetworkError):
        # 连接中途断开：请求可能已经发出去了，不盲目重试。
        return ToolCallError(message, kind="connection", retryable=False)

    return ToolCallError(message, kind=kind or "unknown", retryable=False)


def _leaf(exc: BaseException) -> BaseException:
    """把 SDK 套的 ExceptionGroup 剥到最里面那层。

    最里面那层才是带信息的那层：`MCPError` 的 `data` 就在它身上。
    """
    seen: set[int] = set()
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions and id(exc) not in seen:
        seen.add(id(exc))
        exc = exc.exceptions[0]
    return exc
