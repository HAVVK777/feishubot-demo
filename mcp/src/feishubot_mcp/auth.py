"""鉴权：一张 `名字 → token` 的表，每个调用方一个。

自己写而不是用 SDK 的 `auth=AuthSettings(...)`：那套是 **OAuth 资源服务器**的形状，
必填 `issuer_url` 和 `resource_server_url`，为几个共享密钥编假 URL 反而更难懂，还得连着
实现授权端点。调用方是固定的几个（自己的 agent 程序），一张表够用；真要做 OAuth 时
换掉的也只是这个文件。

**为什么是一张表而不是一个密钥**：单个共享密钥的维护成本全压在"轮换"上——换一次
要同时改服务端和所有客户端，中间必然有调用失败；而且没法只吊销其中一个。变成表之后：

* 轮换可以分步走：先加新的（旧的同时有效）→ 各个 agent 陆续切 → 再删旧的。
* 某一个泄露了只删它那一行。
* 每个请求的调用方能落到日志和工具里（`caller_name()`），不再是"有人调了"。

名字是**标签不是身份**：不参与校验，重名也允许（轮换时同一个 agent 的新旧 token 会
短暂并存）。
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Iterable, Mapping

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

_BEARER_PREFIX = "bearer "


class BearerTokenMiddleware:
    """没带对 token 的请求一律 401。

    两个实现上的选择，都是踩过的坑：

    * **纯 ASGI 中间件**，不用 Starlette 的 `BaseHTTPMiddleware`。后者把响应包一层
      再转发，对 SSE 这类长连接有已知的缓冲/截断问题——而 streamable HTTP 的响应
      正是长连接（服务端要往客户端推通知）。
    * **`secrets.compare_digest` 比较**，不用 `==`。字符串比较在第一个不同的字符处
      就返回，耗时差异可以被用来逐字节试出 token。
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        tokens: Mapping[str, str] | Iterable[tuple[str, str]],
        exempt_paths: Iterable[str] = (),
    ) -> None:
        self.app = app
        # 提前编码成 bytes：compare_digest 对 str 只接受 ASCII，而 token 里出现非 ASCII
        # 字符是可能的（有人会把中文备注写进去），那时会在**每个请求**上抛 TypeError。
        pairs = tokens.items() if isinstance(tokens, Mapping) else tokens
        self._tokens = tuple((token.encode(), name) for name, token in pairs)
        self._exempt_paths = frozenset(exempt_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # lifespan 走的是同一套 ASGI 接口，但它不是 HTTP 请求——拦掉它 uvicorn 起不来。
        # 探活路径也放行：探活的调用方（容器编排、反代）通常拿不到 token，
        # 需要鉴权的探活等于没有探活。
        if scope["type"] != "http" or scope["path"] in self._exempt_paths:
            await self.app(scope, receive, send)
            return

        caller = self._identify(scope["headers"])
        if caller is None:
            logger.warning(
                "拒绝了一个没带对 token 的请求：%s %s", scope.get("method"), scope["path"]
            )
            await _send_unauthorized(send)
            return

        # 把调用方挂到 scope 上，交给内层的 `AuthContextMiddleware` 放进 contextvar，
        # 工具里就能用 `caller_name()` 读到。装配顺序见 `server.build_http_app`——
        # 反过来的话内层读 scope 时这里还没写。
        scope["user"] = _authenticated(caller)
        await self.app(scope, receive, send)

    def _identify(self, headers: Iterable[tuple[bytes, bytes]]) -> str | None:
        """认出是哪个调用方，认不出返回 None。

        每个候选都比一遍 `compare_digest`，不做字典查找：字典查找是"算出 hash 直接
        命中"，命中和不命中的耗时差异同样能被用来试探；候选只有几个，线性比不慢。
        """
        provided = _bearer_value(headers)
        if provided is None:
            return None
        for token, name in self._tokens:
            if secrets.compare_digest(provided, token):
                return name
        return None


def caller_name() -> str | None:
    """当前这次调用的调用方名字（token 表里的那个）。

    工具里可以直接用：记日志、按调用方区分行为、或者做粗粒度的权限判断。

    HTTP 模式下由 `BearerTokenMiddleware` 从**已验证**的 token 反查出来；stdio 和
    进程内直连没有这层中间件，返回 `None`。所以工具必须处理 None——不能把它当成
    "没有权限"，也不能当成"有权限"。
    """
    access_token = get_access_token()
    return access_token.client_id if access_token else None


def _authenticated(name: str) -> AuthenticatedUser:
    """按 SDK 期望的形状造一个已认证用户。

    `token` 字段**故意留空**：那是给 OAuth 场景放原始 bearer token 的，塞进去等于
    让每个工具都能通过 `get_access_token()` 读到明文 token。名字在 `client_id` 里，
    这里只用来归因。
    """
    return AuthenticatedUser(AccessToken(token="", client_id=name, scopes=[]))


def _bearer_value(headers: Iterable[tuple[bytes, bytes]]) -> bytes | None:
    """从请求头里取出 Bearer 后面那段，没有就是 None。"""
    for key, value in headers:
        if key.lower() != b"authorization":
            continue
        raw = value.decode("latin-1").strip()
        if raw.lower().startswith(_BEARER_PREFIX):
            return raw[len(_BEARER_PREFIX) :].strip().encode()
    return None


async def _send_unauthorized(send: Send) -> None:
    """401 的响应体写成 **JSON-RPC 错误对象**，而不是 RFC 6750 的
    `{"error": "invalid_token", ...}`。

    形状是照着**客户端怎么读它**倒推的：SDK 的客户端拿到 4xx 时，先看 content-type，
    是 JSON 就试着按 JSON-RPC 错误解析——解析成功就把 `message` 原样交给调用方，
    失败则退化成一句 "Server returned an error response"。SDK 自己的鉴权中间件用的
    正是 RFC 6750 那套，于是落进后一种，调用方只看到一句没信息量的话（实测客户端
    甚至只抛出一个空消息的 `ReadError`）。这里故意和它不一样：报错要能到人手里。

    `id` 给 null 是允许的——客户端会把请求真实的 id 填回去（"request rejected before
    its id was parsed" 那条分支）。
    """
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": None,
            "error": {
                "code": -32600,
                "message": "未授权：缺少或错误的 Bearer token"
                "（请求头要带 Authorization: Bearer <token>，token 见 MCP_AUTH_TOKENS）",
                # 和另外几类失败用同一套 `kind`：调用方靠它做分类（这个不该重试，
                # 该去修配置），而不是解析中文消息。
                "data": {"kind": "unauthorized"},
            },
        },
        ensure_ascii=False,
    ).encode()
    # WWW-Authenticate 是规范要求的：客户端据此知道该用哪种认证方式，
    # 没有它客户端只会报一个笼统的连接失败。
    #
    # 这里**不要**再加 `Connection: close`。连接什么时候关是 ASGI 服务器（uvicorn）
    # 的事，应用自己在响应头里声明关闭会让它在读完响应之前就把 socket 关掉，客户端
    # 间歇性地拿到 `ReadError: Connection reset by peer` 而不是那个 401——实测
    # 十来次里挂一次。401 不该污染连接，下一个请求带着对 token 复用同一条连接是正常的。
    headers = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"www-authenticate", b'Bearer realm="mcp"'),
        (b"content-length", str(len(body)).encode()),
    ]
    await send({"type": "http.response.start", "status": 401, "headers": headers})
    await send({"type": "http.response.body", "body": body})
