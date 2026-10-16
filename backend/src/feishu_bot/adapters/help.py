"""内置的 `/帮助`。

本地实现，不对外呼——它只是把路由表渲染成一张卡片。放在 `adapters/` 是因为
`core/` 只定义 Protocol、实现一律在 adapters，这样它才能和外部服务一样被注册表
一视同仁地分发，`MessageHandler` 里不需要为"帮助"开特例分支。
"""

from __future__ import annotations

from feishu_bot.core.domain import Reply, ServiceRequest
from feishu_bot.core.routing import Router


class HelpService:
    def __init__(self, router: Router) -> None:
        self._router = router

    async def handle(self, request: ServiceRequest) -> Reply:
        # 帮助内容完全由规则表决定，用不上 request——但签名要保持和其他服务一致，
        # 这样 MessageHandler 才能一视同仁地分发。
        return self._router.help_reply()
