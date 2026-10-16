"""出站适配器的测试：中立回复 → 飞书卡片 → 真实请求体。"""

from __future__ import annotations

import json

import httpx
import pytest

from feishu_bot.adapters.feishu.outbound import (
    SEND_MESSAGE_PATH,
    TENANT_ACCESS_TOKEN_PATH,
    FeishuAPIError,
    FeishuMessenger,
    build_card,
)
from feishu_bot.core.domain import Reply, Tone


@pytest.fixture
async def messenger(settings, fake_feishu):
    async with httpx.AsyncClient(
        base_url=settings.base_url, transport=fake_feishu.transport()
    ) as client:
        yield FeishuMessenger(settings, client)


class TestBuildCard:
    @pytest.mark.parametrize(
        ("tone", "template"),
        [(Tone.INFO, "blue"), (Tone.SUCCESS, "green"), (Tone.WARNING, "orange")],
    )
    def test_tone_maps_to_a_feishu_template(self, tone: Tone, template: str) -> None:
        card = build_card(Reply(title="标题", body="正文", tone=tone))

        assert card["header"]["template"] == template

    def test_title_and_body_land_in_the_card(self) -> None:
        card = build_card(Reply(title="我的标题", body="我的正文"))

        assert card["header"]["title"]["content"] == "我的标题"
        assert card["elements"][0]["text"]["content"] == "我的正文"

    def test_body_uses_lark_md_so_markdown_renders(self) -> None:
        card = build_card(Reply(title="t", body="**粗体**"))

        assert card["elements"][0]["text"]["tag"] == "lark_md"

    def test_markdown_survives_the_tag_neutralisation(self) -> None:
        """中和只针对 `<`，不能顺手把 markdown 也弄坏。"""
        card = build_card(Reply(title="t", body="**粗体** 和 `代码` 和 [链接](https://x.test)"))

        assert (
            card["elements"][0]["text"]["content"] == "**粗体** 和 `代码` 和 [链接](https://x.test)"
        )

    def test_at_tag_in_the_body_is_neutralised(self) -> None:
        """`lark_md` 认 `<at id=...>`：正文里可能混着用户原文，不处理就等于让人
        借机器人的嘴 @ 别人。"""
        card = build_card(Reply(title="t", body="你好 <at id=ou_someone></at>"))

        body = card["elements"][0]["text"]["content"]
        assert "<at" not in body
        assert "＜at id=ou_someone>" in body

    def test_placeholder_brackets_survive(self) -> None:
        """帮助卡片里的 `/搜索 <内容>` 不能被误伤——它后面跟的不是 ASCII 字母。"""
        card = build_card(Reply(title="t", body="用法：/搜索 <内容>"))

        assert card["elements"][0]["text"]["content"] == "用法：/搜索 <内容>"

    def test_a_bare_less_than_survives(self) -> None:
        """`a < b` 不是标签，不该变形。"""
        card = build_card(Reply(title="t", body="a < b"))

        assert card["elements"][0]["text"]["content"] == "a < b"

    def test_title_is_not_neutralised(self) -> None:
        """标题用的是 plain_text，本来就不解析标签，不需要动它。"""
        card = build_card(Reply(title="a < b", body="正文"))

        assert card["header"]["title"]["content"] == "a < b"


class TestSend:
    async def test_content_is_a_json_string_not_an_object(
        self, messenger: FeishuMessenger, fake_feishu
    ) -> None:
        """这是发送失败最常见的原因，所以专门钉一条。"""
        await messenger.send("oc_1", Reply(title="标题", body="正文"))

        body = json.loads(fake_feishu.requests[-1].content)
        assert isinstance(body["content"], str)
        assert json.loads(body["content"])["header"]["title"]["content"] == "标题"

    async def test_message_is_sent_as_an_interactive_card(
        self, messenger: FeishuMessenger, fake_feishu
    ) -> None:
        await messenger.send("oc_1", Reply(title="t", body="b"))

        body = json.loads(fake_feishu.requests[-1].content)
        assert body["msg_type"] == "interactive"
        assert body["receive_id"] == "oc_1"

    async def test_receive_id_type_is_chat_id(
        self, messenger: FeishuMessenger, fake_feishu
    ) -> None:
        await messenger.send("oc_1", Reply(title="t", body="b"))

        assert fake_feishu.requests[-1].url.params["receive_id_type"] == "chat_id"

    async def test_request_carries_the_bearer_token(
        self, messenger: FeishuMessenger, fake_feishu
    ) -> None:
        await messenger.send("oc_1", Reply(title="t", body="b"))

        assert fake_feishu.requests[-1].headers["Authorization"] == "Bearer t-fake"


class TestAccessToken:
    async def test_token_is_fetched_with_the_app_credentials(
        self, messenger: FeishuMessenger, fake_feishu
    ) -> None:
        await messenger.send("oc_1", Reply(title="t", body="b"))

        request = next(r for r in fake_feishu.requests if r.url.path == TENANT_ACCESS_TOKEN_PATH)
        assert json.loads(request.content) == {
            "app_id": "cli_test_app",
            "app_secret": "test-app-secret",
        }

    async def test_token_is_cached_across_sends(
        self, messenger: FeishuMessenger, fake_feishu
    ) -> None:
        """有效期 2 小时，每次发送都去换会被限频。"""
        for _ in range(3):
            await messenger.send("oc_1", Reply(title="t", body="b"))

        assert fake_feishu.token_calls == 1

    async def test_concurrent_sends_do_not_stampede_the_token_endpoint(
        self, messenger: FeishuMessenger, fake_feishu
    ) -> None:
        import asyncio

        await asyncio.gather(
            *(messenger.send("oc_1", Reply(title="t", body="b")) for _ in range(5))
        )

        assert fake_feishu.token_calls == 1


class TestErrors:
    async def test_business_error_code_raises_even_on_http_200(
        self, messenger: FeishuMessenger, fake_feishu
    ) -> None:
        """飞书的业务错误也走 HTTP 200，只看状态码会静默丢消息。"""
        fake_feishu.fail_with[SEND_MESSAGE_PATH] = 230001

        with pytest.raises(FeishuAPIError) as excinfo:
            await messenger.send("oc_1", Reply(title="t", body="b"))

        assert excinfo.value.code == 230001
        assert excinfo.value.action == "发送消息"

    async def test_token_failure_is_attributed_to_the_token_endpoint(
        self, messenger: FeishuMessenger, fake_feishu
    ) -> None:
        fake_feishu.fail_with[TENANT_ACCESS_TOKEN_PATH] = 99991663

        with pytest.raises(FeishuAPIError) as excinfo:
            await messenger.send("oc_1", Reply(title="t", body="b"))

        assert excinfo.value.action == "获取 tenant_access_token"
