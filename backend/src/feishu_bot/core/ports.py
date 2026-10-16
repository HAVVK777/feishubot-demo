"""端口：核心向外要什么，用 Protocol 声明。

依赖方向是单向外指的——核心只认识 Protocol，不认识实现。`Messenger` 由
`adapters/feishu/outbound.py` 实现，`SeenStore` 由 `adapters/memory.py` 实现，
被路由命中的 `Service` 由 `adapters/inproc.py` 实现，具体接哪一个在
`bootstrap.py` 里决定。
"""

from __future__ import annotations

from typing import Protocol

from feishu_bot.core.domain import Reply, ServiceRequest


class Messenger(Protocol):
    """把一条回复送到某个会话。"""

    async def send(self, chat_id: str, reply: Reply) -> None: ...


class Service(Protocol):
    """一个能被路由命中的能力。

    `async` 是刻意的：现在都是进程内实现，但换成 HTTP 或消息队列时签名一个字都
    不用改——调用方已经在 `await` 了。

    服务内部出错请**抛异常**，由用例兜底成"服务暂时不可用"的卡片。区分标准是
    "基础设施坏了"（异常）还是"没查到结果"（一条正常的 `Reply`，语气由服务自己
    决定）——绝大多数情况都属于后者。
    """

    async def handle(self, request: ServiceRequest) -> Reply: ...


class SeenStore(Protocol):
    """幂等标记。

    只需要一个方法，且必须是**原子**的：`claim` 把"检查是否见过"和"标记为已见"
    放在同一步完成。拆成 `seen()` + `mark()` 两步会在并发重复投递时留下竞态——
    两个协程都读到"没见过"，然后都去回复。
    """

    def claim(self, key: str) -> bool:
        """第一次见到这个 key 返回 True 并标记；之后返回 False。"""
        ...
