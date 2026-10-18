"""中间件：审计日志、限流、超时。

都不需要 MCP 会话：中间件的输入就是一个 `ctx`（只看 `method` 和 `params`）和一个
`call_next`，所以这里直接喂假的，测得又快又能精确控制时间。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import pytest
from mcp.shared.exceptions import MCPError

from feishubot_mcp.middleware import (
    AuditLog,
    RateLimit,
    ToolTimeout,
    _TokenBucket,
    build_middleware,
)
from feishubot_mcp.session import SessionContext
from feishubot_mcp.settings import Settings


@dataclass
class FakeCtx:
    """只带中间件真正会读的两个字段。"""

    method: str = "tools/call"
    params: dict[str, Any] | None = field(default_factory=lambda: {"name": "工具"})


@dataclass
class FakeResult:
    is_error: bool = False


async def _ok(_ctx) -> FakeResult:
    return FakeResult()


def _caller(monkeypatch: pytest.MonkeyPatch, name: str | None) -> None:
    """冒充某个调用方。真实现里这个名字来自已验证的 token。"""
    monkeypatch.setattr("feishubot_mcp.middleware.caller_name", lambda: name)


def _session(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    """冒充会话标签。真实现里它来自调用方在 `_meta` 里自称的值。"""
    monkeypatch.setattr("feishubot_mcp.middleware.session_id", lambda: value)


def _chat(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    """冒充会话类型（p2p / group）。"""
    monkeypatch.setattr("feishubot_mcp.middleware.chat_type", lambda: value)


class Clock:
    """可控时钟，用来精确验"令牌是随时间补回来的"。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


# --------------------------------------------------------------------------- 限流


async def test_rate_limit_allows_up_to_the_minute_quota(monkeypatch: pytest.MonkeyPatch):
    _caller(monkeypatch, "feishu-agent")
    limit = RateLimit(per_minute=3)

    for _ in range(3):
        assert await limit(FakeCtx(), _ok) is not None

    with pytest.raises(MCPError, match="调用过于频繁：feishu-agent 每分钟最多 3 次") as caught:
        await limit(FakeCtx(), _ok)
    # 调用方代码靠 data 做重试决策，不靠解析中文。
    assert caught.value.data == {
        "kind": "rate_limited",
        "scope": "caller",
        "limit_per_minute": 3,
    }


async def test_rate_limit_refills_over_time(monkeypatch: pytest.MonkeyPatch):
    _caller(monkeypatch, "feishu-agent")
    clock = Clock()
    monkeypatch.setattr("feishubot_mcp.middleware.time.monotonic", clock)
    limit = RateLimit(per_minute=60)  # 每秒补 1 个

    for _ in range(60):
        assert await limit(FakeCtx(), _ok) is not None
    with pytest.raises(MCPError):
        await limit(FakeCtx(), _ok)

    clock.now += 1.0  # 过 1 秒，补回 1 个
    assert await limit(FakeCtx(), _ok) is not None
    with pytest.raises(MCPError):
        await limit(FakeCtx(), _ok)


async def test_rate_limit_is_per_caller(monkeypatch: pytest.MonkeyPatch):
    limit = RateLimit(per_minute=1)

    _caller(monkeypatch, "feishu-agent")
    assert await limit(FakeCtx(), _ok) is not None
    with pytest.raises(MCPError):
        await limit(FakeCtx(), _ok)

    # 另一个调用方有自己的桶，不该被连坐
    _caller(monkeypatch, "ops-agent")
    assert await limit(FakeCtx(), _ok) is not None


async def test_rate_limit_ignores_other_methods(monkeypatch: pytest.MonkeyPatch):
    """列工具、握手这些不该吃工具调用的额度。"""
    _caller(monkeypatch, "feishu-agent")
    limit = RateLimit(per_minute=1)

    for _ in range(5):
        assert await limit(FakeCtx(method="tools/list", params=None), _ok) is not None
    assert await limit(FakeCtx(), _ok) is not None  # 第一次真正调用仍有权


# --------------------------------------------------------------- 限流 · 会话那一层


async def test_session_limit_is_per_session(monkeypatch: pytest.MonkeyPatch):
    """一个会话的额度不能被另一个会话吃掉——这层就是为"单用户别把容量打满"存在的。"""
    _caller(monkeypatch, "agent")
    _session(monkeypatch, "chat-a")
    _chat(monkeypatch, "p2p")
    limit = RateLimit(per_minute=0, session_per_minute=2)

    assert await limit(FakeCtx(), _ok) is not None
    assert await limit(FakeCtx(), _ok) is not None
    with pytest.raises(MCPError, match="这个会话调用过于频繁：chat-a 每分钟最多 2 次"):
        await limit(FakeCtx(), _ok)

    _session(monkeypatch, "chat-b")
    assert await limit(FakeCtx(), _ok) is not None


async def test_session_limit_refuses_calls_without_a_session(monkeypatch: pytest.MonkeyPatch):
    """配了单会话限额却没带 session_id —— **拒绝**，不是跳过。

    跳过等于留个洞：一个不带 session_id 的跑飞循环完全不受这一层管，而那正是单会话
    限额要防的。报 -32602（少给了参数），不是 -32000（调得太频）。
    """
    _caller(monkeypatch, "agent")
    _session(monkeypatch, None)
    _chat(monkeypatch, "p2p")
    limit = RateLimit(per_minute=0, session_per_minute=5)

    with pytest.raises(MCPError) as caught:
        await limit(FakeCtx(), _ok)

    assert caught.value.code == -32602
    assert "session_id" in caught.value.message


async def test_missing_session_is_fine_when_no_session_limit_is_configured(
    monkeypatch: pytest.MonkeyPatch,
):
    """没配额度的维度就不该管人家带不带。"""
    _caller(monkeypatch, "agent")
    _session(monkeypatch, None)
    limit = RateLimit(per_minute=5)

    assert await limit(FakeCtx(), _ok) is not None


async def test_group_chat_gets_its_own_quota(monkeypatch: pytest.MonkeyPatch):
    """群聊和单聊分两套额度：群里的额度是全群共享的，值得有单独的数。"""
    _caller(monkeypatch, "agent")
    _session(monkeypatch, "chat-a")
    _chat(monkeypatch, "group")
    limit = RateLimit(per_minute=0, session_per_minute=5, group_per_minute=2)

    assert await limit(FakeCtx(), _ok) is not None
    assert await limit(FakeCtx(), _ok) is not None
    with pytest.raises(MCPError, match="这个群聊调用过于频繁：chat-a 每分钟最多 2 次"):
        await limit(FakeCtx(), _ok)

    # 同一个会话换成单聊类型（现实中不会发生，但能证明确实是两套桶）
    _chat(monkeypatch, "p2p")
    assert await limit(FakeCtx(), _ok) is not None


async def test_group_falls_back_to_the_session_quota(monkeypatch: pytest.MonkeyPatch):
    """群聊额度没配就跟随单聊额度。

    否则"只写了每会话 20"会让群聊变成**不限**——少配一个变量就把口子开大，是最容易
    忽略的那种配置事故。
    """
    _caller(monkeypatch, "agent")
    _session(monkeypatch, "chat-a")
    _chat(monkeypatch, "group")
    limit = RateLimit(per_minute=0, session_per_minute=2)

    assert await limit(FakeCtx(), _ok) is not None
    assert await limit(FakeCtx(), _ok) is not None
    with pytest.raises(MCPError, match="这个群聊调用过于频繁：chat-a 每分钟最多 2 次"):
        await limit(FakeCtx(), _ok)


async def test_chat_type_is_required_when_a_session_quota_is_configured(
    monkeypatch: pytest.MonkeyPatch,
):
    """不知道是群还是单聊，就不知道该用哪个额度——拒绝，别猜。"""
    _caller(monkeypatch, "agent")
    _session(monkeypatch, "chat-a")
    _chat(monkeypatch, None)
    limit = RateLimit(per_minute=0, session_per_minute=5)

    with pytest.raises(MCPError) as caught:
        await limit(FakeCtx(), _ok)

    assert caught.value.code == -32602
    # 消息里要带合法取值：拼错的值和没带的值都走这条路，只写"没带"是查不出来的。
    assert "p2p、group" in caught.value.message
    assert caught.value.data == {
        "kind": "missing_meta",
        "field": "chat_type",
        "allowed": ["p2p", "group"],
    }


async def test_no_labels_needed_when_no_session_quota_is_configured(
    monkeypatch: pytest.MonkeyPatch,
):
    """只在调用方那层限流时，不该要求会话标签。"""
    _caller(monkeypatch, "agent")
    _session(monkeypatch, None)
    _chat(monkeypatch, None)
    limit = RateLimit(per_minute=5)

    assert await limit(FakeCtx(), _ok) is not None


async def test_session_rejection_does_not_eat_the_caller_quota(monkeypatch: pytest.MonkeyPatch):
    """会话超了不该连累别的会话——这次拒绝不能把调用方的额度也扣走。"""
    _caller(monkeypatch, "agent")
    _session(monkeypatch, "chat-a")
    _chat(monkeypatch, "p2p")
    limit = RateLimit(per_minute=2, session_per_minute=1)

    assert await limit(FakeCtx(), _ok) is not None  # chat-a 用掉 1；调用方剩 1

    with pytest.raises(MCPError, match="这个会话"):
        await limit(FakeCtx(), _ok)

    _session(monkeypatch, "chat-b")
    assert await limit(FakeCtx(), _ok) is not None  # 调用方那 1 个还在


# --------------------------------------------------------------------- 桶的回收


def test_full_buckets_are_evicted(monkeypatch: pytest.MonkeyPatch):
    """攒满的桶要删掉——否则桶表会随"出现过的所有会话"一直涨。"""
    clock = Clock()
    monkeypatch.setattr("feishubot_mcp.middleware.time.monotonic", clock)
    bucket = _TokenBucket(per_minute=10)

    for i in range(100):
        bucket.take(f"chat-{i}")
    assert bucket.size == 100

    clock.now += _TokenBucket.SWEEP_INTERVAL_SECONDS + 1
    bucket.take("someone")  # 触发扫描

    # 那 100 个已经空闲一分钟以上、都回满了 → 删掉；只剩刚用过的那一个
    assert bucket.size == 1


def test_non_full_buckets_are_kept(monkeypatch: pytest.MonkeyPatch):
    """**只删满桶。** 删没满的桶等于把剩余额度抹掉重来，那是限流绕过。"""
    clock = Clock()
    monkeypatch.setattr("feishubot_mcp.middleware.time.monotonic", clock)
    bucket = _TokenBucket(per_minute=10)

    bucket.take("trigger")

    clock.now = 30
    for _ in range(10):
        bucket.take("hot")  # 用光，最后一次在 t=30

    clock.now = 61
    bucket.take("other")  # 触发扫描

    # hot 到 t=61 只补了 31 秒（5 个多），没满 → 必须留着
    assert bucket.size == 2  # hot + other
    assert bucket.take("hot") is True


def test_sweep_is_not_run_on_every_call(monkeypatch: pytest.MonkeyPatch):
    """扫描是 O(n)，不能挂在每次调用上——隔 30 秒不该触发。"""
    clock = Clock()
    monkeypatch.setattr("feishubot_mcp.middleware.time.monotonic", clock)
    bucket = _TokenBucket(per_minute=10)

    for i in range(50):
        bucket.take(f"c{i}")

    clock.now = 30
    bucket.take("x")

    assert bucket.size == 51  # 没到扫描间隔，空闲的桶都还在


def test_eviction_does_not_change_the_allowance(monkeypatch: pytest.MonkeyPatch):
    """删桶不改变任何行为：被删的桶本来就已经回满了。"""
    clock = Clock()
    monkeypatch.setattr("feishubot_mcp.middleware.time.monotonic", clock)
    bucket = _TokenBucket(per_minute=3)

    assert bucket.take("a") is True

    clock.now += _TokenBucket.SWEEP_INTERVAL_SECONDS + 1
    bucket.take("b")  # 触发扫描，a 的桶被删

    # a 回来时拿到的额度和"桶一直留着"完全一样：满额度 3 次
    assert all(bucket.take("a") for _ in range(3))
    assert bucket.take("a") is False


# --------------------------------------------------------------------------- 超时


async def test_timeout_gives_up_with_a_readable_message():
    timeout = ToolTimeout(seconds=0.05)

    async def never_returns(_ctx):
        await asyncio.sleep(5)

    # 断言整句，不用 match= ：那句话里有小数点，当成正则会被 RUF043 揪住。
    with pytest.raises(MCPError) as caught:
        await timeout(FakeCtx(), never_returns)

    # 消息要告诉调用方"状态不确定"，否则它会直接重发一遍。
    assert caught.value.message == (
        "工具 工具 超过 0.05 秒没有返回，已放弃等待（它可能仍在执行，重试前请确认这个工具是否幂等）"
    )
    # 重试决策唯一的依据：这次到底可能做没做。
    assert caught.value.data == {
        "kind": "timeout",
        "tool": "工具",
        "timeout_seconds": 0.05,
        "may_still_be_running": True,
    }


async def test_timeout_lets_fast_tools_through():
    assert await ToolTimeout(seconds=1)(FakeCtx(), _ok) is not None


async def test_a_tools_own_timeout_error_is_not_mistaken_for_ours():
    """工具自己抛的 `TimeoutError`（它调的上游超时了）要原样传上去。

    两个都用 `TimeoutError`，只看异常类型分不出来——所以实现里查的是"超时窗口有没有
    真的到期"。查错了的话，工具自己写的"上游超时"会被顶替成"工具超时"，排查方向就歪了。
    """

    async def upstream_timed_out(_ctx):
        raise TimeoutError("连不上检索服务")

    with pytest.raises(TimeoutError, match="连不上检索服务"):
        await ToolTimeout(seconds=1)(FakeCtx(), upstream_timed_out)


# --------------------------------------------------------------------------- 审计


async def test_audit_logs_who_called_what_and_how_long(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
):
    _caller(monkeypatch, "feishu-agent")

    with caplog.at_level(logging.INFO):
        await AuditLog()(FakeCtx(params={"name": "search", "arguments": {"query": "x"}}), _ok)

    assert "feishu-agent" in caplog.text
    assert "search" in caplog.text
    assert "调用完成" in caplog.text


async def test_audit_logs_argument_names_but_never_their_values(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
):
    """参数值是调用方的数据——飞书那边就是用户发的原文。

    日志通常比数据库活得久、管得松，原样记等于把用户内容复制一份过去。
    """
    _caller(monkeypatch, "feishu-agent")

    with caplog.at_level(logging.INFO):
        await AuditLog()(
            FakeCtx(params={"name": "search", "arguments": {"query": "用户的隐私"}}), _ok
        )

    assert "query" in caplog.text
    assert "用户的隐私" not in caplog.text


async def test_audit_marks_tool_level_failures(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
):
    """工具用 `ToolError` 报的失败是**正常返回值**（`is_error=True`），不抛异常。

    所以只看有没有抛，会把失败记成成功。
    """

    async def returns_error(_ctx) -> FakeResult:
        return FakeResult(is_error=True)

    with caplog.at_level(logging.INFO):
        await AuditLog()(FakeCtx(), returns_error)

    assert "调用出错" in caplog.text


async def test_audit_logs_crashes_at_warning_and_reraises(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
):
    _caller(monkeypatch, "feishu-agent")

    async def crashes(_ctx):
        raise RuntimeError("上游挂了")

    with caplog.at_level(logging.INFO), pytest.raises(RuntimeError, match="上游挂了"):
        await AuditLog()(FakeCtx(), crashes)

    assert "调用失败" in caplog.text
    assert "feishu-agent" in caplog.text


async def test_audit_ignores_non_tool_calls(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
):
    """每次建会话都来的 `initialize` / `tools/list` 会把真正的调用淹掉。"""
    _caller(monkeypatch, "feishu-agent")

    with caplog.at_level(logging.INFO):
        await AuditLog()(FakeCtx(method="tools/list", params=None), _ok)

    assert caplog.text == ""


async def test_audit_shows_anonymous_when_there_is_no_caller(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
):
    _caller(monkeypatch, None)

    with caplog.at_level(logging.INFO):
        await AuditLog()(FakeCtx(), _ok)

    assert "未鉴权" in caplog.text


async def test_audit_shows_the_session(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
):
    """归因要能回答"哪个飞书会话触发的这一串调用"。"""
    _caller(monkeypatch, "agent")
    _session(monkeypatch, "chat-abc")

    with caplog.at_level(logging.INFO):
        await AuditLog()(FakeCtx(), _ok)

    assert "chat-abc" in caplog.text


async def test_audit_marks_calls_without_a_session(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
):
    """没带就显式写出来——空的会话列看起来像"没记"，写明才看得出是调用方没给。"""
    _caller(monkeypatch, "agent")
    _session(monkeypatch, None)

    with caplog.at_level(logging.INFO):
        await AuditLog()(FakeCtx(), _ok)

    assert "无会话" in caplog.text


# --------------------------------------------------------------------------- 装配


def test_build_middleware_skips_what_is_turned_off():
    """0 = 关。关掉的项不进链，而不是进链之后再判断——少一层也比多一层好。"""
    names = [type(m).__name__ for m in build_middleware(Settings())]
    assert names == ["SessionContext", "AuditLog", "ToolTimeout"]  # 两个限流都默认关

    names = [type(m).__name__ for m in build_middleware(Settings(rate_limit_per_minute=60))]
    assert names == ["SessionContext", "AuditLog", "RateLimit", "ToolTimeout"]

    names = [
        type(m).__name__
        for m in build_middleware(
            Settings(session_rate_limit_per_minute=20, tool_timeout_seconds=0)
        )
    ]
    assert names == ["SessionContext", "AuditLog", "RateLimit"]

    names = [
        type(m).__name__
        for m in build_middleware(Settings(rate_limit_per_minute=60, tool_timeout_seconds=0))
    ]
    assert names == ["SessionContext", "AuditLog", "RateLimit"]


def test_session_context_is_outermost():
    """限流和审计都要读它放进 contextvar 的会话标签，所以它必须在最外面。"""
    chain = build_middleware(Settings(session_rate_limit_per_minute=20))
    assert isinstance(chain[0], SessionContext)


def test_audit_log_is_outside_the_things_it_should_record():
    """被限流挡掉、被执行超时放弃的调用也要留下记录——否则日志里会出现一段"什么都没
    发生"的空白，而那正是最该看的时候。

    它外面只能有 `SessionContext`：那个只是把会话标签放进 contextvar，不拒绝任何调用。
    """
    chain = build_middleware(Settings(rate_limit_per_minute=60, session_rate_limit_per_minute=20))
    names = [type(m).__name__ for m in chain]

    assert names.index("AuditLog") < names.index("RateLimit")
    assert names.index("AuditLog") < names.index("ToolTimeout")
