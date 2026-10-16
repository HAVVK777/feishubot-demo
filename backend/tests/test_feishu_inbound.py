"""入站适配器的测试：飞书的请求打进来，卡片发出去。

走的是真实的 ASGI 应用和真实的 HTTP 调用，被替换掉的只有"远端那台服务器"。
端到端用的是真装配——真读 routes.json、真建 HttpService——这正是它证明力的来源。
"""

from __future__ import annotations

import dataclasses
import json

import httpx

from feishu_bot.bootstrap import create_app
from feishu_bot.core.routing import HELP_TITLE
from tests.conftest import (
    SEARCH_KEYWORD,
    SEARCH_PATH,
    SEND_PATH,
    SUMMARY_KEYWORD,
    SUMMARY_PATH,
    VERIFICATION_TOKEN,
    card_body,
    card_title,
    challenge_body,
    message_event,
    service_title,
)


class TestUrlVerification:
    async def test_challenge_is_returned_verbatim(self, client: httpx.AsyncClient) -> None:
        response = await client.post("/feishu/events", json=challenge_body(challenge="abc123"))

        assert response.status_code == 200
        assert response.json() == {"challenge": "abc123"}

    async def test_challenge_with_wrong_token_is_rejected(self, client: httpx.AsyncClient) -> None:
        response = await client.post("/feishu/events", json=challenge_body(token="not-the-token"))

        assert response.status_code == 403

    async def test_challenge_is_accepted_when_no_token_is_configured(
        self, settings, fake_feishu, fake_services
    ) -> None:
        """Verification Token 没配的时候不校验——本地演示的默认状态。

        这种配置下任何人都能伪造回调，所以 `bootstrap.create_app` 会打一条
        warning。这里验的是"能跑起来"，不是"这样安全"。
        """
        lax = dataclasses.replace(settings, verification_token="")
        app = create_app(
            lax,
            feishu_transport=fake_feishu.transport(),
            service_transport=fake_services.transport(),
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://bot.test"
        ) as http:
            response = await http.post("/feishu/events", json=challenge_body(token=""))

        assert response.status_code == 200
        assert response.json()["challenge"]

    async def test_challenge_cannot_be_missing(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            "/feishu/events", json={"type": "url_verification", "token": VERIFICATION_TOKEN}
        )

        assert response.status_code == 400


class TestMessageEvent:
    async def test_event_is_acked_and_a_card_is_sent(
        self, client: httpx.AsyncClient, fake_feishu
    ) -> None:
        response = await client.post("/feishu/events", json=message_event(text="/搜索 论文"))

        assert response.status_code == 200
        assert len(fake_feishu.sent_cards) == 1

        card = fake_feishu.sent_cards[0]
        assert card_title(card) == service_title(SEARCH_KEYWORD)
        assert "论文" in card_body(card)

    async def test_card_goes_to_the_events_chat(
        self, client: httpx.AsyncClient, fake_feishu
    ) -> None:
        await client.post("/feishu/events", json=message_event(chat_id="oc_target"))

        assert fake_feishu.sent_messages[0]["receive_id"] == "oc_target"

    async def test_duplicate_delivery_replies_only_once(
        self, client: httpx.AsyncClient, fake_feishu, fake_services
    ) -> None:
        """飞书是"至少发送一次"，同一条消息投两次是完全正常的。"""
        event = message_event(message_id="om_dup", text="/搜索 只该回一次")

        first = await client.post("/feishu/events", json=event)
        second = await client.post("/feishu/events", json=event)

        # 两次都要 ACK——不 ACK 的话飞书会一直重发。
        assert first.status_code == second.status_code == 200
        assert len(fake_feishu.sent_cards) == 1
        # 外部服务也只该被调一次：重复投递不能变成重复外呼。
        assert len(fake_services.calls) == 1

    async def test_group_mention_placeholder_is_stripped(
        self, client: httpx.AsyncClient, fake_feishu, fake_services
    ) -> None:
        event = message_event(
            text="@_user_1 /搜索 帮我看下这篇论文",
            chat_type="group",
            mentions=[
                {
                    "key": "@_user_1",
                    "id": {"open_id": "ou_bot"},
                    "name": "机器人",
                    "tenant_key": "test-tenant",
                }
            ],
        )

        await client.post("/feishu/events", json=event)

        # 命令被正常识别——剥离发生在路由之前。
        assert card_title(fake_feishu.sent_cards[0]) == service_title(SEARCH_KEYWORD)
        # 内容里也不该混进占位符——剥离发生在拆分内容之前。
        assert fake_services.calls[0]["text"] == "帮我看下这篇论文"

    async def test_event_with_wrong_token_is_rejected(
        self, client: httpx.AsyncClient, fake_feishu, fake_services
    ) -> None:
        response = await client.post("/feishu/events", json=message_event(token="forged"))

        assert response.status_code == 403
        assert fake_feishu.sent_cards == []
        assert fake_services.calls == []

    async def test_send_failure_still_returns_200(
        self, client: httpx.AsyncClient, fake_feishu
    ) -> None:
        """飞书那边必须看到 200，否则会按 15s→5min→1h→6h 重发。

        发送失败是**我们**的问题，不能再让它变成飞书的重发风暴。
        """
        fake_feishu.fail_with[SEND_PATH] = 230001

        response = await client.post("/feishu/events", json=message_event(text="/搜索 x"))

        assert response.status_code == 200


class TestIgnoredInputs:
    """这些都不该被回复，但都必须回 200，否则会招来重发。"""

    async def test_other_event_types_are_ignored(
        self, client: httpx.AsyncClient, fake_feishu
    ) -> None:
        response = await client.post(
            "/feishu/events", json=message_event(event_type="im.chat.updated_v1")
        )

        assert response.status_code == 200
        assert fake_feishu.sent_cards == []

    async def test_non_text_message_is_ignored(
        self, client: httpx.AsyncClient, fake_feishu
    ) -> None:
        response = await client.post(
            "/feishu/events", json=message_event(message_type="image", content='{"image_key":"k"}')
        )

        assert response.status_code == 200
        assert fake_feishu.sent_cards == []

    async def test_missing_message_id_is_ignored(
        self, client: httpx.AsyncClient, fake_feishu
    ) -> None:
        """没有 message_id 就无法去重，宁可漏回一条也不发两条。"""
        event = message_event()
        del event["event"]["message"]["message_id"]

        response = await client.post("/feishu/events", json=event)

        assert response.status_code == 200
        assert fake_feishu.sent_cards == []

    async def test_broken_content_json_gets_a_warning_card(
        self, client: httpx.AsyncClient, fake_feishu
    ) -> None:
        """解析不了的正文当空文本处理，回一张"没有收到文字"。

        比静默丢弃好：用户在界面上看得到有东西来过，日志里有原始 content，
        排查有起点。
        """
        response = await client.post("/feishu/events", json=message_event(content="{不是 JSON"))

        assert response.status_code == 200
        assert fake_feishu.sent_cards[0]["header"]["template"] == "orange"

    async def test_legacy_schema_is_ignored(self, client: httpx.AsyncClient, fake_feishu) -> None:
        event = message_event()
        event["schema"] = "1.0"

        response = await client.post("/feishu/events", json=event)

        assert response.status_code == 200
        assert fake_feishu.sent_cards == []


class TestBadRequests:
    async def test_encrypted_body_gets_a_clear_error(self, client: httpx.AsyncClient) -> None:
        """后台顺手开了 Encrypt Key 是最常见的翻车方式，报错要说清怎么办。"""
        response = await client.post("/feishu/events", json={"encrypt": "ds3da3sj32421lkkld4"})

        assert response.status_code == 501
        assert "Encrypt Key" in response.json()["detail"]

    async def test_non_json_body_is_rejected(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            "/feishu/events",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 400


class TestContentParsing:
    """`content` 是字符串化的 JSON——"字段明明是空的"多半栽在这里。"""

    async def test_text_is_read_from_the_double_encoded_content(
        self, client: httpx.AsyncClient, fake_services
    ) -> None:
        raw = json.dumps({"text": "/搜索 双重编码的内容"}, ensure_ascii=False)

        await client.post("/feishu/events", json=message_event(content=raw))

        assert fake_services.calls[0]["text"] == "双重编码的内容"


class TestDispatch:
    """端到端的证据：**同一把假飞书收到的是不同的卡片，外部服务收到的是不同的内容**。"""

    async def test_slash_command_reaches_its_service(
        self, client: httpx.AsyncClient, fake_feishu, fake_services
    ) -> None:
        await client.post("/feishu/events", json=message_event(text="/搜索 transformer"))

        assert fake_services.paths == [SEARCH_PATH]
        # 服务收到的是解析后的内容，不是整条命令。
        assert fake_services.calls[0]["text"] == "transformer"
        assert card_title(fake_feishu.sent_cards[0]) == service_title(SEARCH_KEYWORD)

    async def test_alias_reaches_the_same_service(
        self, client: httpx.AsyncClient, fake_services
    ) -> None:
        await client.post("/feishu/events", json=message_event(text="帮我查一下 transformer"))

        assert fake_services.paths == [SEARCH_PATH]
        assert fake_services.calls[0]["text"] == "帮我查一下 transformer"

    async def test_different_keywords_go_to_different_services(
        self, client: httpx.AsyncClient, fake_feishu, fake_services
    ) -> None:
        """把"分发"本身钉死：两个不同的端点，两张标题不同的卡片。"""
        await client.post("/feishu/events", json=message_event(text="/搜索 a", message_id="om_a"))
        await client.post("/feishu/events", json=message_event(text="/摘要 b", message_id="om_b"))

        assert fake_services.paths == [SEARCH_PATH, SUMMARY_PATH]
        assert [card_title(card) for card in fake_feishu.sent_cards] == [
            service_title(SEARCH_KEYWORD),
            service_title(SUMMARY_KEYWORD),
        ]

    async def test_keyword_without_aliases_does_not_fire_on_prose(
        self, client: httpx.AsyncClient, fake_feishu, fake_services
    ) -> None:
        """`摘要` 在配置里没配 aliases，所以正文里出现它只该回帮助。"""
        await client.post("/feishu/events", json=message_event(text="帮我摘要一下这段"))

        assert fake_services.calls == []
        assert card_title(fake_feishu.sent_cards[0]) == HELP_TITLE

    async def test_external_service_receives_the_source_ids(
        self, client: httpx.AsyncClient, fake_services
    ) -> None:
        await client.post(
            "/feishu/events",
            json=message_event(text="/搜索 x", message_id="om_9", chat_id="oc_9"),
        )

        (call,) = fake_services.calls
        assert call["messageId"] == "om_9"
        assert call["chatId"] == "oc_9"
        assert call["senderId"] == "ou_test_user"

    async def test_unmatched_text_gets_the_help_card(
        self, client: httpx.AsyncClient, fake_feishu, fake_services
    ) -> None:
        await client.post("/feishu/events", json=message_event(text="今天天气不错"))

        assert fake_services.calls == []
        card = fake_feishu.sent_cards[0]
        assert card_title(card) == HELP_TITLE
        assert "/搜索" in card_body(card)

    async def test_unknown_command_gets_the_help_card(
        self, client: httpx.AsyncClient, fake_feishu, fake_services
    ) -> None:
        await client.post("/feishu/events", json=message_event(text="/sousuo x"))

        assert fake_services.calls == []
        assert "sousuo" in card_body(fake_feishu.sent_cards[0])

    async def test_command_without_argument_gets_usage_not_a_service_card(
        self, client: httpx.AsyncClient, fake_feishu, fake_services
    ) -> None:
        await client.post("/feishu/events", json=message_event(text="/搜索"))

        assert fake_services.calls == []
        assert "/搜索 <内容>" in card_body(fake_feishu.sent_cards[0])

    async def test_service_failure_gets_a_fallback_card(
        self, client: httpx.AsyncClient, fake_feishu, fake_services
    ) -> None:
        """外部服务挂了，用户看到的是一张兜底卡片，而不是石沉大海。"""
        fake_services.respond_raw(SEARCH_PATH, status=503, text="上游挂了")

        response = await client.post("/feishu/events", json=message_event(text="/搜索 x"))

        # 飞书那边照样是 200——否则会招来重发风暴。
        assert response.status_code == 200
        assert fake_feishu.sent_cards[0]["header"]["template"] == "orange"

    async def test_help_command_lists_the_configured_keywords(
        self, client: httpx.AsyncClient, fake_feishu
    ) -> None:
        await client.post("/feishu/events", json=message_event(text="/帮助"))

        body = card_body(fake_feishu.sent_cards[0])
        assert "/搜索 <内容>" in body
        assert "/摘要 <内容>" in body
