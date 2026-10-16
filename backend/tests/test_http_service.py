"""外部服务转发的测试。

这个文件钉的是**接口约定**：请求发什么字段、响应怎么读、什么算失败。以后改协议的时候
这里会先红，而不是等某台服务对不上才发现。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from feishu_bot.adapters.http_service import HttpService, ServiceCallError
from feishu_bot.core.domain import IncomingMessage, MatchKind, ServiceRequest, Tone
from tests.conftest import SEARCH_ENDPOINT, SEARCH_KEYWORD, SEARCH_PATH

REQUEST_HEADERS = {"Authorization": "Bearer secret"}


def make_request(
    *,
    argument: str = "transformer",
    trigger: str = "搜索",
    matched: MatchKind = MatchKind.SLASH,
) -> ServiceRequest:
    return ServiceRequest(
        message=IncomingMessage(
            message_id="om_1",
            chat_id="oc_1",
            sender_id="ou_1",
            text=argument,
            chat_type="p2p",
        ),
        argument=argument,
        matched=matched,
        trigger=trigger,
    )


@pytest.fixture
async def make_service(fake_services) -> AsyncIterator[Callable[..., HttpService]]:
    """造一个指向假外部服务的 `HttpService`，结束时把客户端都关掉。"""
    clients: list[httpx.AsyncClient] = []

    def factory(**overrides: Any) -> HttpService:
        client = httpx.AsyncClient(transport=fake_services.transport())
        clients.append(client)
        params: dict[str, Any] = {
            "keyword": SEARCH_KEYWORD,
            "endpoint": SEARCH_ENDPOINT,
            "client": client,
        }
        params.update(overrides)
        return HttpService(**params)

    try:
        yield factory
    finally:
        for client in clients:
            await client.aclose()


class TestRequest:
    async def test_posts_the_documented_payload(self, make_service, fake_services) -> None:
        await make_service().handle(make_request())

        (call,) = fake_services.calls
        assert call["keyword"] == SEARCH_KEYWORD
        assert call["text"] == "transformer"
        assert call["messageId"] == "om_1"
        assert call["chatId"] == "oc_1"
        assert call["senderId"] == "ou_1"
        assert call["matched"] == "slash"
        assert call["trigger"] == "搜索"

    async def test_sends_the_configured_headers(self, make_service, fake_services) -> None:
        await make_service(headers=REQUEST_HEADERS).handle(make_request())

        assert fake_services.requests[0].headers["Authorization"] == "Bearer secret"

    async def test_sends_no_authorization_when_none_configured(
        self, make_service, fake_services
    ) -> None:
        await make_service().handle(make_request())

        assert "Authorization" not in fake_services.requests[0].headers

    async def test_posts_to_the_configured_endpoint(self, make_service, fake_services) -> None:
        await make_service().handle(make_request())

        assert fake_services.paths == [SEARCH_PATH]

    async def test_mention_hits_forward_the_whole_text(self, make_service, fake_services) -> None:
        await make_service().handle(
            make_request(
                argument="帮我查一下 transformer", trigger="查一下", matched=MatchKind.MENTION
            )
        )

        (call,) = fake_services.calls
        assert call["text"] == "帮我查一下 transformer"
        assert call["matched"] == "mention"
        assert call["trigger"] == "查一下"


class TestResponse:
    async def test_reads_title_text_and_tone(self, make_service, fake_services) -> None:
        fake_services.respond_json(
            SEARCH_PATH, {"title": "检索结果", "text": "找到 3 篇", "tone": "success"}
        )

        reply = await make_service().handle(make_request())

        assert reply.title == "检索结果"
        assert reply.body == "找到 3 篇"
        assert reply.tone is Tone.SUCCESS

    async def test_title_defaults_to_the_keyword(self, make_service, fake_services) -> None:
        fake_services.respond_json(SEARCH_PATH, {"text": "只有正文"})

        assert (await make_service().handle(make_request())).title == SEARCH_KEYWORD

    async def test_unknown_tone_falls_back_to_info(self, make_service, fake_services) -> None:
        """对方拼错一个 tone 不该把整条回复搞挂。"""
        fake_services.respond_json(SEARCH_PATH, {"text": "正文", "tone": "urgent"})

        assert (await make_service().handle(make_request())).tone is Tone.INFO

    async def test_tone_is_case_insensitive(self, make_service, fake_services) -> None:
        fake_services.respond_json(SEARCH_PATH, {"text": "正文", "tone": "WARNING"})

        assert (await make_service().handle(make_request())).tone is Tone.WARNING


class TestFailures:
    """全部抛 `ServiceCallError`，由用例兜底成"服务暂时不可用"的卡片。"""

    async def test_http_error_status(self, make_service, fake_services) -> None:
        fake_services.respond_raw(SEARCH_PATH, status=503, text="上游挂了")

        with pytest.raises(ServiceCallError, match="503"):
            await make_service().handle(make_request())

    async def test_response_is_not_json(self, make_service, fake_services) -> None:
        fake_services.respond_raw(SEARCH_PATH, text="<html>不是 JSON</html>")

        with pytest.raises(ServiceCallError, match="不是 JSON"):
            await make_service().handle(make_request())

    async def test_response_is_not_an_object(self, make_service, fake_services) -> None:
        fake_services.respond_json(SEARCH_PATH, ["不是对象"])

        with pytest.raises(ServiceCallError, match="不是对象"):
            await make_service().handle(make_request())

    @pytest.mark.parametrize("payload", [{}, {"text": ""}, {"text": "   "}, {"text": 42}])
    async def test_missing_text_is_a_failure(
        self, make_service, fake_services, payload: dict[str, Any]
    ) -> None:
        """正文是必填的——没有它就没什么可展示的，静默发一张空卡片更糟。"""
        fake_services.respond_json(SEARCH_PATH, payload)

        with pytest.raises(ServiceCallError, match="text"):
            await make_service().handle(make_request())

    async def test_connection_failure_is_wrapped(self) -> None:
        def explode(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("连不上", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(explode)) as client:
            service = HttpService(keyword=SEARCH_KEYWORD, endpoint=SEARCH_ENDPOINT, client=client)

            with pytest.raises(ServiceCallError, match="请求发不出去"):
                await service.handle(make_request())

    async def test_error_message_names_the_endpoint(self, make_service, fake_services) -> None:
        """服务一多，不说是哪台就得靠猜。"""
        fake_services.respond_raw(SEARCH_PATH, status=500, text="boom")

        with pytest.raises(ServiceCallError, match=SEARCH_ENDPOINT):
            await make_service().handle(make_request())
