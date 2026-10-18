"""请求级的横切关注点：审计日志、限流、超时。

三个都用 SDK 的 `ServerMiddleware`——它包住**每一个**入站请求，所以在这一处就能拿到
"谁、调了什么、多久、成没成"，任何一个工具都不用改。

顺序（`MCPServer(middleware=[...])` 是**外层在前**）：

    AuditLog → RateLimit → ToolTimeout → 工具

* `AuditLog` 放最外层：被限流挡掉的、超时放弃的调用都要留下记录，否则日志里会有一段
  "什么都没发生"的空白——而那恰恰是最该看的时候。
* `RateLimit` 在 `ToolTimeout` 外面：被挡掉的调用不该占用超时窗口。

它们和具体的传输无关（HTTP 和 stdio 都过这一层），所以挂在 server 上而不是 HTTP 应用上。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any

from mcp.server.context import HandlerResult, ServerMiddleware
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult

from feishubot_mcp.auth import caller_name
from feishubot_mcp.session import (
    CHAT_TYPE_META_KEY,
    CHAT_TYPES,
    GROUP_CHAT,
    NO_CHAT_TYPE,
    NO_SESSION,
    SESSION_META_KEY,
    SessionContext,
    chat_type,
    session_id,
)
from feishubot_mcp.settings import Settings
from feishubot_mcp.timing import execution_time_meta

logger = logging.getLogger(__name__)

#: 自定义错误的码。JSON-RPC 把 -32000..-32099 留给实现，MCP 没有"限流""超时"这类标准码，
#: 用这一档；客户端认不出码就只看 message。
RATE_LIMITED = -32000
TOOL_TIMED_OUT = -32001

#: 配了这一层的限额，调用却没带对应的 `_meta` 标签。这是**标准的 invalid params**——
#: 调用少给了东西，不是调用太频繁，所以不该混在 RATE_LIMITED 里。
LABEL_REQUIRED = -32602

#: 没有通过 token 表的调用方（没配 MCP_AUTH_TOKENS，或者走 stdio）。
#: 日志里显式写出来，免得看起来像"某个叫这个的调用方"。
ANONYMOUS = "未鉴权"


class AuditLog(ServerMiddleware):
    """每次工具调用留一行：谁、调了什么、花了多久、成没成。

    两个刻意的取舍：

    * **只记参数名，不记参数值**。值是调用方的数据——飞书那边就是用户发的原文。
      原样进日志等于把用户内容复制进日志系统（日志通常比数据库活得久、管得松）。
      要排查具体内容时由工具自己按需记。
    * **只记 `tools/call`**。`initialize`、`tools/list` 每次建会话都会来一遍，全记会把
      真正的调用淹掉。
    """

    async def __call__(self, ctx, call_next) -> HandlerResult:
        if ctx.method != "tools/call":
            return await call_next(ctx)

        tool = _tool_of(ctx.params)
        # 一行里把三层身份标全：调用方[会话/用户]。
        who = (
            f"{caller_name() or ANONYMOUS}"
            f"[{session_id() or NO_SESSION}/{chat_type() or NO_CHAT_TYPE}]"
        )
        arguments = _argument_names(ctx.params)
        started = time.perf_counter()

        try:
            result = await call_next(ctx)
        except Exception as exc:
            logger.warning(
                "调用失败：%s → %s（%.2fs，参数：%s）：%s",
                who,
                tool,
                time.perf_counter() - started,
                arguments,
                exc,
            )
            raise

        elapsed = time.perf_counter() - started
        # 耗时随响应返回，放在 `_meta` 里：调用方可以记录分析，模型看不到。
        # 合并而不是覆盖，保留结果里可能已经有的其他 `_meta`（例如 serverInfo）。
        #
        # 这里拿到的是 SDK 已经序列化过的结果：`HandlerResult` 可能是 dict，也可能
        # 是 `CallToolResult`，两种都要处理。
        if isinstance(result, dict):
            meta = result.get("_meta")
            result["_meta"] = {
                **(meta if isinstance(meta, dict) else {}),
                **execution_time_meta(elapsed),
            }
        elif isinstance(result, CallToolResult):
            result.meta = {**(result.meta or {}), **execution_time_meta(elapsed)}

        # 工具用 `ToolError` 报的失败**不会抛出来**，它是个 `is_error=True` 的正常返回值，
        # 所以这里得看返回值才知道这次调用到底成没成。dict 形状用 `isError` 键。
        is_error = getattr(result, "is_error", False)
        if isinstance(result, dict):
            is_error = bool(result.get("isError", is_error))
        outcome = "调用出错" if is_error else "调用完成"
        logger.info(
            "%s：%s → %s（%.2fs，参数：%s）",
            outcome,
            who,
            tool,
            elapsed,
            arguments,
        )
        return result


class RateLimit(ServerMiddleware):
    """两级限流：每个**会话**一个额度，每个**调用方**一个总额。

    **会话那层按类型分两套额度**：群聊和单聊都只是一个会话，但群聊的额度是**全群共享**的
    （一个人把额度用光，全群都得等），所以群聊该有自己的数。依据是调用方自称的
    `_meta.chat_type`：

    * `group` —— 用群聊额度
    * `p2p` —— 用单聊额度
    * **群聊额度没配就跟随单聊额度**：只写"每会话 20"时，两种会话都是 20——不会因为
      少配一个变量就把群聊放成不限。

    **调用方**那层（按已验证的 token 反查出的名字）是整条链路上**唯一不可伪造**的一层：
    会话标签是调用方自称的，换个值就是新桶。它也是整个服务的总闸——MCP 通常只看到
    一个调用方（agent），而这是唯一不可伪造的一档。

    计数按名字和会话标签分，不按 IP。桶表只会随 token 表和会话数增长。

    **少了标签就拒绝**（前提是那一层配了限额）。跳过等于留洞：一个不带 `session_id` 的
    跑飞循环就完全不受这一层管，而那正是它要防的。
    """

    def __init__(
        self,
        *,
        per_minute: int,
        session_per_minute: int = 0,
        group_per_minute: int = 0,
    ) -> None:
        self._caller = _TokenBucket(per_minute) if per_minute > 0 else None
        self._session = _TokenBucket(session_per_minute) if session_per_minute > 0 else None
        self._group = _TokenBucket(group_per_minute) if group_per_minute > 0 else None

    async def __call__(self, ctx, call_next) -> HandlerResult:
        if ctx.method != "tools/call":
            return await call_next(ctx)

        caller = caller_name() or ANONYMOUS
        session = session_id()

        # 先查细的（会话），再查粗的（调用方）：会话超了就不该再扣调用方的令牌。
        # 两段检查之间**没有 await**，所以单线程事件循环里不会交错。
        bucket, scope = self._quota_for(ctx)
        if bucket is not None and not bucket.take(session or ""):
            label = "群聊" if scope == "group" else "会话"
            raise MCPError(
                code=RATE_LIMITED,
                message=f"这个{label}调用过于频繁：{session} 每分钟最多 {bucket.per_minute} 次",
                # data 是给**调用方代码**的：它据此算该等多久、退避重试。
                # message 是给**人和模型**的：读得懂就够。
                data={
                    "kind": "rate_limited",
                    "scope": scope,
                    "limit_per_minute": bucket.per_minute,
                },
            )
        if self._caller is not None and not self._caller.take(caller):
            raise MCPError(
                code=RATE_LIMITED,
                message=f"调用过于频繁：{caller} 每分钟最多 {self._caller.per_minute} 次",
                data={
                    "kind": "rate_limited",
                    "scope": "caller",
                    "limit_per_minute": self._caller.per_minute,
                },
            )
        return await call_next(ctx)

    def _quota_for(self, ctx) -> tuple[_TokenBucket | None, str]:
        """这次调用该用哪个桶。返回 (桶, 机器可读的档位)；桶是 None 表示这层不限。

        档位用 `session` / `group` 而不是中文：它会进错误的 `data`，给 agent 代码判断用；
        中文留给 `message`，给人和模型看。

        没配任何会话额度就直接放行，连标签都不要求——**没配的东西不该管人家带不带**。
        """
        if self._session is None and self._group is None:
            return None, "session"

        session = session_id()
        chat = chat_type()
        _require(session, SESSION_META_KEY, "按会话")
        _require(chat, CHAT_TYPE_META_KEY, "按会话类型", "、".join(CHAT_TYPES))

        if chat == GROUP_CHAT:
            return (self._group or self._session), "group"
        return self._session, "session"


def _require(value: str | None, key: str, how: str, allowed: str = "") -> None:
    """配了这一层的限额，却少给了对应的标签 —— **拒绝**，不跳过。

    跳过等于留个洞：不带标签的调用完全不受这一层管，而那正是它要防的东西。

    `allowed` 非空时把合法取值也写进消息：拼错的值和没带的值都会走到这里，而"拼错了"
    只看一句"没带"是查不出来的。
    """
    if value is None:
        suffix = f"，合法取值：{allowed}" if allowed else ""
        details: dict[str, Any] = {"kind": "missing_meta", "field": key}
        if allowed:
            # 只有有取值域的那一层（chat_type）才带，session_id 没有。
            details["allowed"] = allowed.split("、")
        raise MCPError(
            code=LABEL_REQUIRED,
            message=(
                f"这次调用没有带可用的 _meta.{key}，无法{how}计量{suffix}。"
                "每个 tools/call 都要带上它（见 README「按会话限流」）。"
            ),
            data=details,
        )


class _TokenBucket:
    """一个令牌桶：容量是一分钟的量，按秒连续补。

    所以"攒满一分钟的额度一次打出去"是允许的，之后只能按补充速度来——正常的突发不
    误伤，跑飞的循环会被压到限速上。

    **桶表只保留活跃的会话**：攒满的桶会被定期删掉。满桶和不存在是等价的（下次调用都
    从满开始），所以删它不改变任何行为——而删没满的桶等于把剩余额度抹掉重来，那是限流
    绕过。没有这一步的话，这个 dict 会随出现过的所有会话一直涨。
    """

    #: 多久扫一次。**恰好等于一个空桶回满所需的时间**（容量 / 补充速率恒为 60 秒），
    #: 所以一次扫描就足以让"空闲了一分钟以上"的桶全部消失。
    SWEEP_INTERVAL_SECONDS = 60.0

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._capacity = float(per_minute)
        self._refill_per_second = per_minute / 60.0
        #: 键 → (剩余令牌, 上次补充的时刻)
        self._buckets: dict[str, tuple[float, float]] = {}
        self._last_sweep = time.monotonic()

    @property
    def size(self) -> int:
        """当前保留了多少个桶。用来验证回收真的在发生（也会是将来指标的一项）。"""
        return len(self._buckets)

    def take(self, key: str) -> bool:
        """取走一个令牌；没有了就返回 False。

        **整个函数没有 `await`**：单线程事件循环里它不会被中途切走，"读-改-写"不会
        交错，所以不需要锁。哪天给它加了任何 await，这里就变成竞态。
        """
        now = time.monotonic()
        self._sweep_if_due(now)

        tokens, last = self._buckets.get(key, (self._capacity, now))
        tokens = min(self._capacity, tokens + (now - last) * self._refill_per_second)
        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - 1.0, now)
        return True

    def _sweep_if_due(self, now: float) -> None:
        """删掉已经回满的桶。

        必须**先补再判断**：存储的 `tokens` 是上次更新时的值。一个一小时没用过的桶，存
        储值可能是 0，但它现在早就满了——按存储值判断的话，空闲的桶永远不会被删，内存
        照样涨，而且是在最常见的情况下涨。
        """
        if now - self._last_sweep < self.SWEEP_INTERVAL_SECONDS:
            return
        self._last_sweep = now

        # 夹到容量再比较：既精确（不会因为浮点误差漏删），也说明"满"就是满。
        full = [
            key
            for key, (tokens, last) in self._buckets.items()
            if min(self._capacity, tokens + (now - last) * self._refill_per_second)
            >= self._capacity
        ]
        for key in full:
            del self._buckets[key]


class ToolTimeout(ServerMiddleware):
    """单次工具调用的上限，防止一个卡住的上游把请求一直挂着。

    **超时只是"不再等它"，不是"中止它"**：同步工具跑在工作线程里，Python 杀不掉线程，
    它会继续跑完，副作用照常发生。所以这个值要按"最慢的正常调用"来定，别当成中断手段。
    """

    def __init__(self, *, seconds: float) -> None:
        self._seconds = seconds

    async def __call__(self, ctx, call_next) -> HandlerResult:
        if ctx.method != "tools/call":
            return await call_next(ctx)

        timeout = asyncio.timeout(self._seconds)
        try:
            async with timeout:
                return await call_next(ctx)
        except TimeoutError as exc:
            if not timeout.expired():
                # 是工具自己抛的 TimeoutError（比如它调的上游超时了），不是我们掐的，
                # 不该顶替成"工具超时"。
                raise
            tool = _tool_of(ctx.params)
            raise MCPError(
                code=TOOL_TIMED_OUT,
                # "可能仍在执行"这句是**给调用方看的决策依据**，不是场面话：超时意味着
                # "不知道对方做没做"，而这正是最不该盲目重试的一类失败。调用方拿到这句
                # 才知道该去看工具声明（`@hints`）而不是直接再发一次。
                message=(
                    f"工具 {tool} 超过 {self._seconds:g} 秒没有返回，已放弃等待"
                    "（它可能仍在执行，重试前请确认这个工具是否幂等）"
                ),
                # `may_still_be_running` 是这次失败**唯一重要的事实**：它决定重试是否
                # 安全。调用方代码不该从中文散文里猜这一点。
                data={
                    "kind": "timeout",
                    "tool": tool,
                    "timeout_seconds": self._seconds,
                    "may_still_be_running": True,
                },
            ) from exc


def build_middleware(settings: Settings) -> list[ServerMiddleware[Any]]:
    """按配置装好中间件链，外层在前。"""
    chain: list[ServerMiddleware[Any]] = [
        # 最外层：审计和限流都要读它放进去的会话标签。
        SessionContext(),
        AuditLog(),
    ]
    if any(
        (
            settings.rate_limit_per_minute,
            settings.session_rate_limit_per_minute,
            settings.group_rate_limit_per_minute,
        )
    ):
        chain.append(
            RateLimit(
                per_minute=settings.rate_limit_per_minute,
                session_per_minute=settings.session_rate_limit_per_minute,
                group_per_minute=settings.group_rate_limit_per_minute,
            )
        )
    if settings.tool_timeout_seconds > 0:
        chain.append(ToolTimeout(seconds=settings.tool_timeout_seconds))
    return chain


def _tool_of(params: Mapping[str, Any] | None) -> str:
    """从**原始**入参里取工具名。

    中间件跑在参数校验之前，所以 `params` 什么形状都可能有——不能假设它一定是
    `{"name": str, "arguments": {...}}`。
    """
    name = (params or {}).get("name")
    return name if isinstance(name, str) else "?"


def _argument_names(params: Mapping[str, Any] | None) -> str:
    """只取参数名。理由见 `AuditLog` 的类文档。"""
    arguments = (params or {}).get("arguments")
    if not isinstance(arguments, Mapping) or not arguments:
        return "无"
    return "、".join(sorted(str(key) for key in arguments))
