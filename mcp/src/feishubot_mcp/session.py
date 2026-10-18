"""会话标签：这次调用是替哪个飞书会话、哪种会话发的。

## 它和 `auth.caller_name()` 是两回事

| | 来源 | 能不能拿来做判断 |
|---|---|---|
| `caller_name()` | **已验证的 token** 反查出来的 | 能——身份 |
| `session_id()` / `chat_type()` | 调用方在 `_meta` 里**自称**的 | **不能**——只是标签 |

MCP 没有任何办法验证这些值。它们适合做归因和公平性（限流、日志），**不能**做权限判断
（"这个会话能不能调这个工具"）。这条界线要在写工具时守住。

另外它们**不是** MCP 传输层的 `Mcp-Session-Id`：那个是连接级的、由传输层自己管，和
"一次对话"没有关系。

## 调用方怎么带

在每个 `tools/call` 的 params 里：

    {"name": "search", "arguments": {},
     "_meta": {"session_id": "oc_xxx", "chat_type": "group"}}

`_meta` 是协议里的**开放**字段（保留键都以 `io.modelcontextprotocol/` 开头），塞自定义
键是规范允许的——实测自定义键会原样出现在服务端的 `ctx.meta` 里。

`chat_type` 的取值跟飞书一致（`p2p` 单聊 / `group` 群聊），和机器人那边
`IncomingMessage.chat_type` 是同一套词汇。**为什么要传**：一个群和一个单聊都只是一个
会话，行为却不一样——群聊里额度是全群共享的，所以两边的额度要分开定。

**稳定性由调用方保证**：同一次对话的多次调用要用同一个 id，换了 id 就等于换了个额度桶。
每次请求现生成一个的话，限流等于没限。
"""

from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any

from mcp.server.context import HandlerResult, ServerMiddleware

#: `_meta` 里的键名。都是调用方自称的。
SESSION_META_KEY = "session_id"
CHAT_TYPE_META_KEY = "chat_type"

#: `chat_type` 的合法取值，和飞书一致。
P2P_CHAT = "p2p"
GROUP_CHAT = "group"
CHAT_TYPES = (P2P_CHAT, GROUP_CHAT)

#: 日志里代替"没带"的说法。显式写出来，免得看起来像某个真的叫这名字的值。
NO_SESSION = "无会话"
NO_CHAT_TYPE = "无类型"

_session: ContextVar[str | None] = ContextVar("session_id", default=None)
_chat_type: ContextVar[str | None] = ContextVar("chat_type", default=None)


def session_id() -> str | None:
    """当前这次调用带的会话标签；没带返回 None。

    工具和中间件里都能用。**工具必须处理 None**——没带的调用是合法的，只是没有归因。
    """
    return _session.get()


def chat_type() -> str | None:
    """当前这次调用的会话类型：`"p2p"`、`"group"`，或 None（没带/认不出来）。

    **群聊和单聊共用同一个 `session_id` 命名空间**，区别只在额度——所以这个值是限流
    分流的依据，不是身份。
    """
    return _chat_type.get()


class SessionContext(ServerMiddleware):
    """把 `_meta` 里的会话标签放进 contextvar。

    必须排在中间件链的**最外层**：限流按它分桶、审计按它归因，都在里面。
    """

    async def __call__(self, ctx, call_next) -> HandlerResult:
        session_token = _session.set(extract(ctx.meta, SESSION_META_KEY))
        type_token = _chat_type.set(extract_chat_type(ctx.meta))
        try:
            return await call_next(ctx)
        finally:
            _chat_type.reset(type_token)
            _session.reset(session_token)


def extract(meta: Mapping[str, Any] | None, key: str) -> str | None:
    """从 `_meta` 里取一个字符串标签。

    中间件跑在协议层，`_meta` 是调用方直接给的原始数据（开放 map），所以类型要自己判：
    不是非空字符串一律当没带。
    """
    if not meta:
        return None
    value = meta.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def extract_chat_type(meta: Mapping[str, Any] | None) -> str | None:
    """取会话类型，**只认飞书那两个取值**。

    认不出来（拼错、大小写不一样、用了别的词）一律当没带——然后限流那层会拒绝并说清
    合法取值。宽松地"猜一个"会让 E 类错误变成静默的行为差异，那是这里最不该有的东西。
    """
    value = extract(meta, CHAT_TYPE_META_KEY)
    return value if value in CHAT_TYPES else None
