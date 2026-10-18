"""会话历史与每会话串行化。

`chatId` 是会话键。同一个会话的消息必须串行处理：群聊里两条消息并发跑，历史会互相
污染（A 的模型回复还没写回，B 已经基于旧历史开始了）。锁不是性能优化，是语义要求。

回收规则：空闲超过 TTL 且**没有正在处理的回合**才删。删掉一把正在用的锁等于允许
同一会话并跑。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from feishubot_agent.harness.model import Message


@dataclass(slots=True)
class Session:
    chat_id: str
    messages: list[Message] = field(default_factory=list)
    #: 同一会话的回合串行执行。
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_active: float = field(default_factory=time.monotonic)

    def touch(self, *, now: float | None = None) -> None:
        self.last_active = time.monotonic() if now is None else now


class SessionRegistry:
    """进程内的会话表。

    多副本部署时同一会话可能落到不同实例，历史和锁都不共享。需要跨实例时在
    `Session` 后面加一个 `SessionStore` 协议实现，不要把存储细节漏进 loop。
    """

    def __init__(self, *, idle_ttl_seconds: float) -> None:
        if idle_ttl_seconds <= 0:
            raise ValueError("idle_ttl_seconds 必须大于 0")
        self._idle_ttl_seconds = idle_ttl_seconds
        self._sessions: dict[str, Session] = {}

    def get(self, chat_id: str, *, now: float | None = None) -> Session:
        session = self._sessions.get(chat_id)
        if session is None:
            session = Session(chat_id=chat_id)
            self._sessions[chat_id] = session
        session.touch(now=now)
        return session

    def sweep(self, *, now: float | None = None) -> int:
        """删掉空闲且未上锁的会话，返回删除数量。"""
        current = time.monotonic() if now is None else now
        expired = [
            chat_id
            for chat_id, session in self._sessions.items()
            if not session.lock.locked() and current - session.last_active >= self._idle_ttl_seconds
        ]
        for chat_id in expired:
            del self._sessions[chat_id]
        return len(expired)

    def __len__(self) -> int:
        return len(self._sessions)

    def __contains__(self, chat_id: str) -> bool:
        return chat_id in self._sessions
