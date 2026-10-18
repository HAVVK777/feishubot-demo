"""会话表：同一会话同一对象，空闲且未上锁才回收。"""

from __future__ import annotations

from feishubot_agent.harness.session import SessionRegistry


def test_get_returns_the_same_session_and_touches_it() -> None:
    registry = SessionRegistry(idle_ttl_seconds=10)

    first = registry.get("oc_1", now=1)
    second = registry.get("oc_1", now=2)

    assert first is second
    assert second.last_active == 2


def test_sweep_drops_only_idle_sessions() -> None:
    registry = SessionRegistry(idle_ttl_seconds=10)
    registry.get("idle", now=0)
    registry.get("fresh", now=5)

    removed = registry.sweep(now=11)

    assert removed == 1
    assert "idle" not in registry
    assert "fresh" in registry


async def test_sweep_keeps_a_locked_session() -> None:
    registry = SessionRegistry(idle_ttl_seconds=10)
    session = registry.get("busy", now=0)

    async with session.lock:
        removed = registry.sweep(now=100)

    assert removed == 0
    assert "busy" in registry
