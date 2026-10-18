"""HTTP 入口。

只做两件事：把请求体交给 `TurnService`，把回复原样返回。超时、会话、重试都在
agent 内部处理；这里不碰业务，也不碰飞书协议。

服务用 getter 而不是直接传实例：真实服务需要异步列 MCP 工具，只能在 lifespan 里
装配；路由在请求时才读 `app.state.service`。
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, HTTPException

from feishubot_agent import __version__
from feishubot_agent.app.contracts import TurnReply, TurnRequest
from feishubot_agent.app.service import TurnService


def build_router(get_service: Callable[[], TurnService | None]) -> APIRouter:
    router = APIRouter()

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        """只证明进程活着，不检查模型或 MCP 依赖。"""
        return {"status": "ok", "version": __version__}

    @router.post("/turn", response_model=TurnReply)
    async def turn(request: TurnRequest) -> TurnReply:
        service = get_service()
        if service is None:
            # 骨架阶段明确失败，比返回一个假的成功回复更容易排查。
            raise HTTPException(status_code=503, detail="agent 回合逻辑尚未实现")
        return await service.handle(request)

    return router
