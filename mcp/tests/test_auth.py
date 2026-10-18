"""鉴权中间件：放行还是拦住，以及"拦下来之后调用方是谁"。

用假的 ASGI 应用当下游，不走真正的 MCP 会话——协议往返在 test_server.py 里测。
分开是因为这里要覆盖的各种**没带对 token** 的请求根本到不了 MCP 那一层。
"""

from __future__ import annotations

import httpx2
import pytest
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from starlette.types import Receive, Scope, Send

from feishubot_mcp.auth import BearerTokenMiddleware, caller_name

TOKEN = "s3cret-token"
OTHER_TOKEN = "another-token"
EXEMPT = "/healthz"
TOKENS = {"alice": TOKEN, "bob": OTHER_TOKEN}


async def _downstream(scope: Scope, receive: Receive, send: Send) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"reached the mcp app"})


async def _client(app) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def client():
    app = BearerTokenMiddleware(_downstream, tokens=TOKENS, exempt_paths=[EXEMPT])
    async with await _client(app) as http:
        yield http


async def test_no_token_is_rejected(client: httpx2.AsyncClient):
    response = await client.post("/mcp", json={})

    assert response.status_code == 401
    # body 必须是 **JSON-RPC 错误对象**：SDK 客户端按这个形状读 4xx 的响应体，
    # 读不出来就只给调用方一句 "Server returned an error response"。
    # 形状的来由见 auth._send_unauthorized。
    body = response.json()
    assert body["jsonrpc"] == "2.0"
    assert "Bearer token" in body["error"]["message"]


async def test_wrong_token_is_rejected(client: httpx2.AsyncClient):
    response = await client.post("/mcp", json={}, headers={"Authorization": "Bearer nope"})

    assert response.status_code == 401


@pytest.mark.parametrize("token", [TOKEN, OTHER_TOKEN])
async def test_every_token_in_the_table_is_accepted(client: httpx2.AsyncClient, token: str):
    """表里每个 token 都能进来——轮换时新旧并存的依据。"""
    response = await client.post("/mcp", json={}, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.text == "reached the mcp app"


async def test_scheme_is_case_insensitive(client: httpx2.AsyncClient):
    """`bearer` 和 `Bearer` 都是合法写法，客户端不会统一成一种。"""
    response = await client.post("/mcp", json={}, headers={"Authorization": f"bearer {TOKEN}"})

    assert response.status_code == 200


async def test_unauthorized_response_tells_the_client_the_scheme(client: httpx2.AsyncClient):
    """MCP 客户端靠这个头判断"该用哪种认证"，少了它只会报一个笼统的连接失败。"""
    response = await client.post("/mcp", json={})

    assert response.headers["www-authenticate"].lower().startswith("bearer")


async def test_health_check_needs_no_token(client: httpx2.AsyncClient):
    """探活的调用方（容器编排、反代）通常拿不到 token。"""
    response = await client.get(EXEMPT)

    assert response.status_code == 200


async def test_lifespan_scope_passes_through():
    """lifespan 不是 HTTP 请求。拦掉它 uvicorn 直接起不来——不是 401，是启动失败。"""
    seen: list[Scope] = []

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope)

    middleware = BearerTokenMiddleware(downstream, tokens=TOKENS)

    async def receive() -> dict:
        return {"type": "lifespan.startup"}

    async def send(_message: dict) -> None:
        return None

    await middleware({"type": "lifespan"}, receive, send)

    assert [scope["type"] for scope in seen] == ["lifespan"]


async def test_the_caller_is_identified_by_name():
    """通过校验之后，调用方的名字要挂到 scope 上。

    下游这里读的就是 SDK 认的那个键——上层的 `AuthContextMiddleware` 正是从这里取的。
    """
    seen: list[Scope] = []

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope)
        await _downstream(scope, receive, send)

    async with await _client(BearerTokenMiddleware(downstream, tokens=TOKENS)) as http:
        await http.post("/mcp", json={}, headers={"Authorization": f"Bearer {OTHER_TOKEN}"})

    user = seen[0].get("user")
    assert isinstance(user, AuthenticatedUser)
    assert user.access_token.client_id == "bob"
    # 故意不放明文 token 进去：否则每个工具都能通过 get_access_token() 读到它。
    assert user.access_token.token == ""


async def test_caller_name_is_visible_inside_the_app():
    """装配顺序对不对，就看这个——`BearerTokenMiddleware` 必须在外层。

    反过来的话工具里拿到的永远是 None，而且不会有任何报错。
    """
    names: list[str | None] = []

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        names.append(caller_name())
        await _downstream(scope, receive, send)

    app = BearerTokenMiddleware(AuthContextMiddleware(downstream), tokens=TOKENS)
    async with await _client(app) as http:
        await http.post("/mcp", json={}, headers={"Authorization": f"Bearer {TOKEN}"})

    assert names == ["alice"]


async def test_caller_name_is_none_without_the_bearer_layer():
    """stdio 和进程内调用没有这层中间件。

    工具必须处理 None，而不是把它当成"没权限"或者"有权限"。
    """
    names: list[str | None] = []

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        names.append(caller_name())
        await _downstream(scope, receive, send)

    async with await _client(AuthContextMiddleware(downstream)) as http:
        await http.post("/mcp", json={})

    assert names == [None]
