"""装配：唯一一处把实现接到端口上的地方。

顺序是 settings → 模型客户端 / MCP 客户端 / 会话表 → `AgentTurnService` → FastAPI。
MCP 工具列表是异步的，所以真实装配放在 lifespan 里；`create_app` 先把空服务放进去，
路由在请求时读取 `app.state.service`。

**不建模块级 `app`**：那会让「导入即读环境变量」，测试和工具一 import 就炸。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from feishubot_agent import __version__
from feishubot_agent.app.profile import AgentProfile
from feishubot_agent.app.service import AgentTurnService, TurnService
from feishubot_agent.harness.errors import ToolCallError
from feishubot_agent.harness.mcp_client import McpToolClient, SessionToolExecutor
from feishubot_agent.harness.providers.deepseek import DeepSeekModelClient
from feishubot_agent.harness.session import SessionRegistry
from feishubot_agent.harness.tools import ToolCatalog
from feishubot_agent.server import build_router
from feishubot_agent.settings import Settings

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    service: TurnService | None = None,
    build_service: bool = True,
) -> FastAPI:
    """装配出一个可以跑的 app。

    `service` 留给测试注入；`build_service=False` 让测试只测 HTTP 层，不去连
    MCP 或模型。
    """
    resolved = settings if settings is not None else Settings.from_environment()

    if not resolved.mcp_token:
        logger.warning("未配置 AGENT_MCP_TOKEN；MCP 侧若启用了鉴权，工具调用会失败")

    app = FastAPI(
        title="Feishu Bot Agent",
        version=__version__,
        lifespan=_lifespan,
    )
    app.state.settings = resolved
    app.state.service = service
    app.state.build_service = build_service
    app.state.model_client = None
    app.state.mcp_client = None
    app.include_router(build_router(lambda: app.state.service))
    return app


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    if app.state.service is None and app.state.build_service:
        await _build_service(app)

    try:
        yield
    finally:
        model = app.state.model_client
        if model is not None:
            await model.aclose()
        mcp = app.state.mcp_client
        if mcp is not None:
            await mcp.aclose()


async def _build_service(app: FastAPI) -> None:
    settings: Settings = app.state.settings
    if not settings.deepseek_api_key:
        raise RuntimeError("缺少 DEEPSEEK_API_KEY：agent 无法调用模型")

    profile = AgentProfile.load(settings)

    mcp = McpToolClient(
        url=settings.mcp_url,
        token=settings.mcp_token,
        timeout_seconds=settings.mcp_timeout_seconds,
    )
    try:
        tool_specs = await mcp.list_tools()
    except ToolCallError as exc:
        # MCP 起不来不应该让整个 bot 起不来：先以空工具列表运行，日志里留明确的
        # 原因。等 MCP 恢复后重启 agent 即可。
        logger.error("启动时列出 MCP 工具失败：%s（将以空工具列表启动）", exc)
        tool_specs = ()

    catalog = ToolCatalog(tool_specs, allowed=profile.allowed_tools)
    if not catalog.specs:
        logger.warning("MCP 没有提供任何工具，agent 只能纯对话")

    model = DeepSeekModelClient(
        model=profile.model,
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        reasoning_effort=settings.reasoning_effort,
        timeout_seconds=settings.turn_timeout_seconds,
    )
    registry = SessionRegistry(idle_ttl_seconds=settings.session_idle_ttl_seconds)

    app.state.service = AgentTurnService(
        profile=profile,
        registry=registry,
        model=model,
        tools=catalog.specs,
        executor_factory=lambda session_id, chat_type: SessionToolExecutor(
            mcp,
            session_id=session_id,
            chat_type=chat_type,
        ),
        turn_timeout_seconds=settings.turn_timeout_seconds,
        history_max_turns=settings.history_max_turns,
        session_lock_wait_seconds=settings.session_lock_wait_seconds,
    )
    app.state.model_client = model
    app.state.mcp_client = mcp

    logger.info(
        "agent 已装配：model=%s，reasoning_effort=%s，工具 %d 个",
        profile.model,
        settings.reasoning_effort,
        len(catalog.specs),
    )
