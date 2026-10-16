"""装配：唯一一处把实现接到端口上的地方。

顺序是 settings → 读路由配置 → 建客户端与各服务 → 注入核心 → 交给 `__main__.py`
启动。核心不认识具体实现，所以"这个关键字交给谁""用内存还是数据库"这类决定全部
收敛在这个文件里。

**不建模块级 `app`**：那会让"导入即读环境变量"，测试和工具一 import 就炸。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from feishu_bot.adapters.feishu.inbound import build_router
from feishu_bot.adapters.feishu.outbound import FeishuMessenger
from feishu_bot.adapters.help import HelpService
from feishu_bot.adapters.http_service import HttpService
from feishu_bot.adapters.memory import MemorySeenStore
from feishu_bot.adapters.routes_file import RouteSpec, load_routes
from feishu_bot.core.handler import MessageHandler
from feishu_bot.core.ports import Service
from feishu_bot.core.routing import BUILTIN_RULES, HELP_KEYWORD, Router, Rule, check_collisions
from feishu_bot.settings import Settings

logger = logging.getLogger(__name__)

#: 飞书接口的默认超时。
FEISHU_TIMEOUT_SECONDS = 10.0

#: 外部服务的兜底超时。单个服务可以在 routes.json 里覆盖。
SERVICE_TIMEOUT_SECONDS = 10.0


def create_app(
    settings: Settings | None = None,
    *,
    feishu_transport: httpx.AsyncBaseTransport | None = None,
    service_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """装配出一个可以跑的 app。

    两个 `*_transport` 是留给测试的接缝：注入 `httpx.MockTransport` 就能在不联网、
    不需要真实凭据的情况下走完"发送卡片"和"转发到外部服务"两条路。分成两个是因为
    它们指向不同的主机，以后也会有各自的超时和重试策略。
    """
    resolved = settings if settings is not None else Settings.from_environment()

    if not resolved.verification_token:
        logger.warning("未配置 Verification Token，回调来源将不做校验（仅供本地演示）")

    feishu_client = httpx.AsyncClient(
        base_url=resolved.base_url,
        timeout=FEISHU_TIMEOUT_SECONDS,
        transport=feishu_transport,
    )
    # 外部服务用的是配置文件里的绝对 URL，所以这个客户端**不设** base_url。
    service_client = httpx.AsyncClient(
        timeout=SERVICE_TIMEOUT_SECONDS,
        transport=service_transport,
    )

    routes = load_routes(resolved.routes_file)
    if not routes:
        logger.warning("%s 里一条路由都没有，机器人只会回 /帮助", resolved.routes_file)

    rules = tuple(_to_rule(spec) for spec in routes) + BUILTIN_RULES
    check_collisions(rules)

    router = Router(rules)
    services: Mapping[str, Service] = {
        spec.keyword: HttpService(
            keyword=spec.keyword,
            endpoint=spec.endpoint,
            client=service_client,
            headers=spec.headers,
            timeout_seconds=spec.timeout_seconds,
        )
        for spec in routes
    }
    # 内置的帮助是本地的，和外部服务走同一个注册表，handler 里不需要特例分支。
    services = {**services, HELP_KEYWORD: HelpService(router)}
    _check_services_cover_rules(rules, services)

    handler = MessageHandler(
        messenger=FeishuMessenger(resolved, feishu_client),
        seen=MemorySeenStore(),
        router=router,
        services=services,
    )

    app = FastAPI(title="Feishu Bot Demo", lifespan=_lifespan)
    app.state.settings = resolved
    app.state.feishu_client = feishu_client
    app.state.service_client = service_client
    app.state.handler = handler
    app.include_router(build_router(handler, resolved))
    return app


def _to_rule(spec: RouteSpec) -> Rule:
    """配置的一条 → 路由表的一条。关键字同时是注册表里的键。"""
    return Rule(
        keyword=spec.keyword,
        aliases=spec.aliases,
        description=spec.description,
    )


def _check_services_cover_rules(rules: Sequence[Rule], services: Mapping[str, Service]) -> None:
    """规则表和注册表对不上时，**在启动时**就说清楚。

    两边都是这个文件按同一份配置生成的，正常不会对不上——这道检查是防止以后有人改了
    其中一边忘了另一边。

    * 有规则没有实现 —— 直接拒绝启动。否则要等第一条消息才炸，而且是在响应之后的
      后台任务里，日志上看起来像"某条消息处理失败"，而不是"配置漏了"。
    * 有实现没有规则 —— 只警告。这个服务永远调不到，通常是规则漏了一条。
    """
    reachable = {rule.keyword for rule in rules}

    missing = sorted(reachable - services.keys())
    if missing:
        raise RuntimeError(
            "这些关键字有规则但没有服务实现：" + "、".join(missing) + "（检查 bootstrap 的装配）"
        )

    unreachable = sorted(services.keys() - reachable)
    if unreachable:
        logger.warning("这些服务没有任何规则能触发：%s", "、".join(unreachable))


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await app.state.feishu_client.aclose()
        await app.state.service_client.aclose()
