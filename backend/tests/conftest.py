"""测试夹具：一个假的飞书开放平台、一组假的外部服务，以及一个接上它们的 app。

假飞书和假外部服务都挂在 `httpx.MockTransport` 上，所以整条链路——入站解析、去重、
路由、外呼、构造卡片、发请求——都是真的在跑，被替换掉的只有"远端那台服务器"。
不需要凭据，不需要网络。

外部服务是**按配置文件注册**的，所以这里先写一份 routes.json 出来，再让 Settings
指向它。这样测的就是真实的装配路径。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from feishu_bot.bootstrap import create_app
from feishu_bot.settings import Settings

VERIFICATION_TOKEN = "test-verification-token"
APP_ID = "cli_test_app"
APP_SECRET = "test-app-secret"

TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
SEND_PATH = "/open-apis/im/v1/messages"

#: 测试用的两个外部服务。注意 `摘要` 故意**没有** aliases——这样两条路径
#: （只有斜杠 / 还有正文触发）都有覆盖。
SEARCH_KEYWORD = "搜索"
SUMMARY_KEYWORD = "摘要"
SEARCH_PATH = "/search"
SUMMARY_PATH = "/summarize"
SEARCH_ENDPOINT = f"https://service.test{SEARCH_PATH}"
SUMMARY_ENDPOINT = f"https://service.test{SUMMARY_PATH}"

DEFAULT_ROUTES: dict[str, Any] = {
    "routes": [
        {
            "keyword": SEARCH_KEYWORD,
            "endpoint": SEARCH_ENDPOINT,
            "description": "在当前知识库里检索",
            "aliases": ["搜索", "查一下", "搜一下"],
        },
        {
            "keyword": SUMMARY_KEYWORD,
            "endpoint": SUMMARY_ENDPOINT,
            "description": "把一段内容压缩成要点",
        },
    ]
}


class FakeFeishu:
    """假装成飞书开放平台，并记录下被请求了什么。"""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.token_calls = 0
        #: 失败注入：路径 → 要返回的业务错误码。
        self.fail_with: dict[str, int] = {}

    @property
    def sent_messages(self) -> list[dict[str, Any]]:
        return [
            json.loads(request.content)
            for request in self.requests
            if request.url.path == SEND_PATH
        ]

    @property
    def sent_cards(self) -> list[dict[str, Any]]:
        """发出去的卡片本体（`content` 再解析一层）。"""
        return [json.loads(message["content"]) for message in self.sent_messages]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path

        if path in self.fail_with:
            return httpx.Response(200, json={"code": self.fail_with[path], "msg": "注入的失败"})

        if path == TOKEN_PATH:
            self.token_calls += 1
            return httpx.Response(
                200,
                json={"code": 0, "msg": "ok", "tenant_access_token": "t-fake", "expire": 7200},
            )

        if path == SEND_PATH:
            return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {}})

        return httpx.Response(404, json={"code": 404, "msg": f"假飞书没有这条路由：{path}"})


def service_title(keyword: str) -> str:
    """`FakeServices` 默认响应里的标题。

    单独抽出来是给测试断言用的——否则"卡片标题怎么拼的"就有两处，改一处就会红。
    """
    return f"{keyword}结果"


class FakeServices:
    """假装成一堆外部服务。

    默认响应**把收到的 text 回显出来**：这是"内容确实被转发过去了、没被截错"
    最直接的证据，端到端测试靠它断言。
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._overrides: dict[str, Callable[[httpx.Request], httpx.Response]] = {}

    @property
    def calls(self) -> list[dict[str, Any]]:
        """收到的请求体，按顺序。"""
        return [json.loads(request.content) for request in self.requests]

    @property
    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def respond_json(self, path: str, payload: Any, *, status: int = 200) -> None:
        self._overrides[path] = lambda _: httpx.Response(status, json=payload)

    def respond_raw(self, path: str, *, status: int = 200, text: str = "") -> None:
        self._overrides[path] = lambda _: httpx.Response(status, text=text)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)

        override = self._overrides.get(request.url.path)
        if override is not None:
            return override(request)

        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                # 标题带上关键字，端到端就能靠标题判断是哪个服务回的话。
                "title": service_title(body["keyword"]),
                "text": f"处理了：{body['text']}",
            },
        )


@pytest.fixture
def routes_file(tmp_path: Path) -> Path:
    path = tmp_path / "routes.json"
    path.write_text(json.dumps(DEFAULT_ROUTES, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def settings(routes_file: Path) -> Settings:
    return Settings(
        app_id=APP_ID,
        app_secret=APP_SECRET,
        verification_token=VERIFICATION_TOKEN,
        base_url="https://open.feishu.test",
        routes_file=routes_file,
    )


@pytest.fixture
def fake_feishu() -> FakeFeishu:
    return FakeFeishu()


@pytest.fixture
def fake_services() -> FakeServices:
    return FakeServices()


@pytest.fixture
async def client(settings: Settings, fake_feishu: FakeFeishu, fake_services: FakeServices):
    """打向本地 app 的客户端。出站请求分别落到假飞书和假外部服务上。"""
    app = create_app(
        settings,
        feishu_transport=fake_feishu.transport(),
        service_transport=fake_services.transport(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://bot.test",
    ) as http:
        yield http


def card_title(card: dict[str, Any]) -> str:
    return card["header"]["title"]["content"]


def card_body(card: dict[str, Any]) -> str:
    return card["elements"][0]["text"]["content"]


def message_event(
    *,
    text: str = "你好",
    message_id: str = "om_test_1",
    chat_id: str = "oc_test_1",
    chat_type: str = "p2p",
    message_type: str = "text",
    content: str | None = None,
    mentions: list[dict[str, Any]] | None = None,
    token: str = VERIFICATION_TOKEN,
    event_type: str = "im.message.receive_v1",
) -> dict[str, Any]:
    """按飞书 schema 2.0 的真实形状造一个事件体。"""
    message: dict[str, Any] = {
        "message_id": message_id,
        "chat_id": chat_id,
        "chat_type": chat_type,
        "message_type": message_type,
        "content": content
        if content is not None
        else json.dumps({"text": text}, ensure_ascii=False),
    }
    if mentions is not None:
        message["mentions"] = mentions

    return {
        "schema": "2.0",
        "header": {
            "event_id": f"evt_{message_id}",
            "event_type": event_type,
            "create_time": "1608725989000",
            "token": token,
            "app_id": APP_ID,
            "tenant_key": "test-tenant",
        },
        "event": {
            "sender": {
                "sender_id": {"open_id": "ou_test_user", "union_id": "on_test"},
                "sender_type": "user",
                "tenant_key": "test-tenant",
            },
            "message": message,
        },
    }


def challenge_body(*, challenge: str = "ajls384kdj1234", token: str = VERIFICATION_TOKEN) -> dict:
    """URL 校验请求体。飞书就是这三行，没有别的。"""
    return {"challenge": challenge, "token": token, "type": "url_verification"}
