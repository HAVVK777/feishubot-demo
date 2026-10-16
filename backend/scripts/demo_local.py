"""本地演示：一条命令跑通整条链路。

不需要飞书凭据，不需要网络，也不需要真的把外部服务起起来——用
`httpx.MockTransport` 把飞书和你的外部服务都假装掉。打进来的请求、路由判定、外呼、
发出去的卡片全是真的在跑。

    uv run python scripts/demo_local.py

**读的是仓库里真实的 `routes.json`**，所以演示的就是你那份配置。加一条路由再跑一次，
第 ④ 节就会多一个服务。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

import httpx

from feishu_bot.bootstrap import create_app
from feishu_bot.settings import Settings

#: 相对脚本定位，这样从哪个目录跑都找得到。
ROUTES_FILE = Path(__file__).resolve().parents[1] / "routes.json"

SETTINGS = Settings(
    app_id="cli_demo",
    app_secret="demo-secret",
    verification_token="demo-verification-token",
    base_url="https://open.feishu.test",
    routes_file=ROUTES_FILE,
)

TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
SEND_PATH = "/open-apis/im/v1/messages"

WIDTH = 68


def title(text: str) -> None:
    print(f"\n{'═' * WIDTH}\n{text}\n{'═' * WIDTH}")


def inbound(description: str, body: Any) -> None:
    print(f"飞书 → 机器人   POST /feishu/events   {description}")
    print(f"                {_compact(body)}")


def outbound(status: int, body: Any) -> None:
    print(f"机器人 → 飞书   {status} {_compact(body)}")


def _compact(value: Any, limit: int = 150) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= limit else text[: limit - 3] + "..."


class FakeFeishu:
    """假装成飞书开放平台，并把它收到的每一样东西打出来。"""

    def __init__(self, *, fail_send: bool = False) -> None:
        self.fail_send = fail_send
        self.token_calls = 0
        self.cards: list[dict[str, Any]] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            self.token_calls += 1
            print("  ↳ 机器人来换 tenant_access_token（有效期 2 小时，之后会被缓存复用）")
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "t-demo", "expire": 7200}
            )

        if request.url.path == SEND_PATH:
            return self._handle_send(request)

        return httpx.Response(404, json={"code": 404, "msg": "假飞书没有这条路由"})

    def _handle_send(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        card = json.loads(body["content"])
        self.cards.append(card)

        print(f"  ↳ 收到发送请求  receive_id={body['receive_id']}  msg_type={body['msg_type']}")
        print(
            f"     卡片标题：{card['header']['title']['content']}"
            f"   配色：{card['header']['template']}"
        )
        print(f"     卡片正文：{card['elements'][0]['text']['content']}")

        if self.fail_send:
            return httpx.Response(200, json={"code": 230001, "msg": "无发送权限"})
        return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {}})


class FakeExternalServices:
    """假装成你在 `routes.json` 里注册的那些服务。

    它**不认路径**——任何 endpoint 都接，这样你改了配置演示照样能跑，而且跑的就是你
    那份真实配置，而不是演示专用的一份。
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.calls.append(payload)

        print(f"  ↳ 外呼  POST {request.url}")
        if authorization := request.headers.get("Authorization"):
            print(f"     携带了你配的 header → Authorization: {authorization}")
        print(f"     请求体：{_compact(payload, 200)}")

        if self.fail:
            return httpx.Response(503, text="上游挂了")

        return httpx.Response(
            200,
            json={
                "title": f"{payload['keyword']}（演示）",
                "text": f"收到了：{payload['text']}",
            },
        )


def message_event(
    *,
    text: str,
    message_id: str,
    chat_id: str = "oc_demo_chat",
    chat_type: str = "p2p",
    mentions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": message_id,
        "chat_id": chat_id,
        "chat_type": chat_type,
        "message_type": "text",
        # 注意这里：content 是**字符串化的 JSON**，不是对象。
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }
    if mentions is not None:
        message["mentions"] = mentions

    return {
        "schema": "2.0",
        "header": {
            "event_id": f"evt_{message_id}",
            "event_type": "im.message.receive_v1",
            "create_time": "1608725989000",
            "token": SETTINGS.verification_token,
            "app_id": SETTINGS.app_id,
            "tenant_key": "demo-tenant",
        },
        "event": {
            "sender": {"sender_id": {"open_id": "ou_demo_user"}, "sender_type": "user"},
            "message": message,
        },
    }


def describe_routes() -> list[str]:
    """把配置里的关键字读出来，用于小节标题。"""
    document = json.loads(ROUTES_FILE.read_text(encoding="utf-8"))
    return [route["keyword"] for route in document["routes"]]


def endpoints() -> dict[str, str]:
    document = json.loads(ROUTES_FILE.read_text(encoding="utf-8"))
    return {route["keyword"]: route["endpoint"] for route in document["routes"]}


async def run() -> None:
    keywords = describe_routes()
    targets = endpoints()
    first, second = keywords[0], keywords[-1]

    fake_feishu = FakeFeishu()
    external = FakeExternalServices()
    app = create_app(
        SETTINGS,
        feishu_transport=fake_feishu.transport(),
        service_transport=external.transport(),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bot.test"
    ) as client:

        async def send(description: str, event: dict[str, Any], *, note: str = "") -> None:
            inbound(description, event)
            response = await client.post("/feishu/events", json=event)
            outbound(response.status_code, {"状态": "已 ACK"})
            if note:
                print(f"\n{note}")

        title(f"配置里注册了 {len(keywords)} 个关键字：{'、'.join(keywords)}")

        title("① URL 校验 —— 在飞书后台点「保存」请求地址时触发")
        body = {
            "challenge": "ajls384kdj1234",
            "token": SETTINGS.verification_token,
            "type": "url_verification",
        }
        inbound("校验请求", body)
        response = await client.post("/feishu/events", json=body)
        outbound(response.status_code, response.json())
        print("\n  原样返回 challenge 即通过。这条路必须在 1 秒内响应，")
        print("  而且飞书**不会**带 X-Lark-Signature 头，所以不能在这里验签。")

        title(f"② 命令路由：/{first} → {targets[first]}")
        await send(
            "消息事件",
            message_event(text=f"/{first} transformer", message_id="om_cmd"),
            note=(
                "  关键字既是命令词，也是 routes.json 里查端点的键。\n"
                "  内容取斜杠之后的整段。ACK 在 3 秒内返回，外呼和发卡片都在这之后。"
            ),
        )

        title(f"③ 别名触发：正文里出现“{first}”也走同一个服务")
        await send(
            "消息事件",
            message_event(text=f"帮我{first}一下 transformer", message_id="om_alias"),
            note=(
                "  注意 text 是**整条消息**——正文触发时用户没划出内容边界，我们也不猜。\n"
                "  要去掉这个行为，把 routes.json 里的 aliases 删掉即可。"
            ),
        )

        title(f"④ 另一个关键字：/{second} → {targets[second]}")
        await send(
            "消息事件",
            message_event(text=f"/{second} 这段文字", message_id="om_second"),
            note="  不同的关键字打到不同的端点、拿到不同的卡片标题——这就是分发。",
        )

        title("⑤ 没命中任何关键字 → 帮助卡片")
        await send(
            "消息事件",
            message_event(text="今天天气不错", message_id="om_none"),
            note="  帮助内容是从路由表现算的：加一条路由，这份帮助自动跟着变。",
        )

        title(f"⑥ 群里 @ 机器人 + /{first}")
        await send(
            "群聊消息事件",
            message_event(
                text=f"@_user_1 /{first} 这篇文章的方法部分",
                message_id="om_group",
                chat_id="oc_demo_group",
                chat_type="group",
                mentions=[
                    {
                        "key": "@_user_1",
                        "id": {"open_id": "ou_demo_bot"},
                        "name": "机器人",
                        "tenant_key": "demo-tenant",
                    }
                ],
            ),
            note="  @_user_1 占位符在拆分内容**之前**就被剥掉了，不会混进外呼的 text。",
        )

        calls_so_far = len(external.calls)
        title("⑦ 同一条命令再推一次 —— 飞书是「至少发送一次」")
        await send(
            "重复投递（message_id 和 ② 相同）",
            message_event(text=f"/{first} transformer", message_id="om_cmd"),
            note=(
                f"  外呼次数停在 {calls_so_far}，第 ⑦ 步没有产生新的调用。\n"
                "  去重排在路由**之前**——一次外呼就是一个真实的副作用，绝不能重复。"
            ),
        )

        title("⑧ 外部服务挂了")
        failing = FakeExternalServices(fail=True)
        failing_app = create_app(
            SETTINGS,
            feishu_transport=fake_feishu.transport(),
            service_transport=failing.transport(),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=failing_app), base_url="http://bot.test"
        ) as failing_client:
            event = message_event(text=f"/{first} 这条会失败", message_id="om_fail")
            inbound("消息事件", {"message_id": "om_fail"})
            response = await failing_client.post("/feishu/events", json=event)
        outbound(response.status_code, {"状态": "仍然 ACK"})
        print("\n  一张橙色的兜底卡片。飞书那边照样是 200——否则会招来重发风暴。")

        title("⑨ 连飞书都发不出去")
        broken = FakeFeishu(fail_send=True)
        broken_app = create_app(
            SETTINGS,
            feishu_transport=broken.transport(),
            service_transport=external.transport(),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=broken_app), base_url="http://bot.test"
        ) as broken_client:
            event = message_event(text=f"/{first} 这条也发不出去", message_id="om_sendfail")
            inbound("消息事件", {"message_id": "om_sendfail"})
            response = await broken_client.post("/feishu/events", json=event)
        outbound(response.status_code, {"状态": "仍然 ACK"})
        print("\n  发送失败是我们的问题，不能再让它变成飞书的重发风暴。")

    title("完成")
    print(
        f"外呼了 {len(external.calls) + len(failing.calls)} 次"
        f"（其中 {len(failing.calls)} 次故意失败），"
        f"发出去 {len(fake_feishu.cards) + len(broken.cards)} 张卡片。"
    )
    print(f"路由配置来自 {ROUTES_FILE}")
    print("\n想看真实 HTTP，用 uvicorn 起服务后自己发请求：")
    print("  uv run python -m feishu_bot")
    print("  curl -s localhost:8000/feishu/events -H 'Content-Type: application/json' \\")
    print('       -d \'{"challenge":"abc","type":"url_verification"}\'')
    print()


def main() -> None:
    # stdout 被重定向时是块缓冲的，日志走 stderr 是无缓冲的——不改成行缓冲的话，
    # 管道里看到的所有日志都会挤在脚本输出前面。
    sys.stdout.reconfigure(line_buffering=True)

    logging.basicConfig(level=logging.INFO, format="      [%(levelname)s] %(message)s")
    # httpx 会把每一个请求都打成 INFO，对演示来说是纯噪声。
    logging.getLogger("httpx").setLevel(logging.WARNING)

    asyncio.run(run())


if __name__ == "__main__":
    main()
