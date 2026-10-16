"""`SeenStore` 的内存实现。

demo 阶段先不碰数据库：进程重启丢会话状态是可以接受的，换来的是零配置启动。
以后要跨重启保留、或者要跑多副本时，在同一组 Protocol 下换个实现即可
（多副本必须换成共享存储——各进程的内存互不可见，去重会失效），核心不用动。
"""

from __future__ import annotations

import time

#: 飞书的重发窗口最长约 7.1 小时（15s → 5min → 1h → 6h，共 4 次），标记必须活得
#: 比它久，否则最后那几次重发会被当成新消息再回一遍。
DEFAULT_TTL_SECONDS = 8 * 3600


class MemorySeenStore:
    """进程内去重，带 TTL。

    不需要加锁：`claim` 全程没有 `await`，在 asyncio 的单线程里就是一个不可分割的
    操作。真实实现（Redis 的 SET NX）靠的是原子的 check-and-set。
    """

    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl = ttl_seconds
        self._seen: dict[str, float] = {}

    def claim(self, key: str) -> bool:
        now = time.monotonic()
        seen_at = self._seen.get(key)
        # 单独判断这一条是否过期，不能只靠 _sweep：清扫是攒够量才做的，光看
        # "key 在不在表里"会让过期的 key 一直压着重发窗口之后的重投。
        if seen_at is not None and now - seen_at < self._ttl:
            return False
        self._seen[key] = now
        # 只在表长起来之后才整体清扫，避免每次调用都退化成 O(n)。
        if len(self._seen) > 1024:
            self._sweep(now)
        return True

    def _sweep(self, now: float) -> None:
        expired = [key for key, at in self._seen.items() if now - at >= self._ttl]
        for key in expired:
            del self._seen[key]

    def __len__(self) -> int:
        return len(self._seen)
