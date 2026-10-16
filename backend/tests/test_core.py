"""核心逻辑的测试。

这个文件里没有任何飞书字样——这正是分层要买到的东西：核心逻辑可以脱离租户、
脱离凭据、脱离网络来测。

"分发确实发生了"由 `TestDispatch` 钉死：两个被记录的服务，断言收到了几次、收到了什么。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import pytest

from feishu_bot.adapters import memory as memory_module
from feishu_bot.adapters.memory import DEFAULT_TTL_SECONDS, MemorySeenStore
from feishu_bot.core.domain import (
    IncomingMessage,
    MatchKind,
    Reply,
    ServiceRequest,
    Tone,
)
from feishu_bot.core.handler import (
    FAILED_TITLE,
    UNREADABLE_TITLE,
    MessageHandler,
    Outcome,
)
from feishu_bot.core.ports import Service
from feishu_bot.core.routing import HELP_KEYWORD, Router, Rule

SEARCH = "搜索"
SUMMARY = "摘要"


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now


class RecordingMessenger:
    """记下发出去的东西，不做任何 IO。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Reply]] = []

    async def send(self, chat_id: str, reply: Reply) -> None:
        self.sent.append((chat_id, reply))

    @property
    def last(self) -> Reply:
        return self.sent[-1][1]


class BrokenMessenger:
    async def send(self, chat_id: str, reply: Reply) -> None:
        raise RuntimeError("发送失败了")


class RecordingService:
    """记下收到的 `ServiceRequest`，返回一张带标记的卡片。

    "分发到了哪个服务"靠它断言——只看回复卡片是不够的，那证明不了**另一个**服务
    没有被调用。
    """

    def __init__(self, title: str) -> None:
        self.title = title
        self.requests: list[ServiceRequest] = []

    async def handle(self, request: ServiceRequest) -> Reply:
        self.requests.append(request)
        return Reply(title=self.title, body=request.argument)


class BrokenService:
    async def handle(self, request: ServiceRequest) -> Reply:
        raise RuntimeError("服务炸了")


@dataclass
class Rig:
    handler: MessageHandler
    messenger: RecordingMessenger
    services: dict[str, Service]

    def recording(self, keyword: str) -> RecordingService:
        """取出记录型服务。顺手断言类型，避免测试在错误的对象上空断言。"""
        service = self.services[keyword]
        assert isinstance(service, RecordingService)
        return service


def build_rules() -> tuple[Rule, ...]:
    return (
        Rule(keyword=SEARCH, aliases=("搜索", "查一下"), description="在当前知识库里检索"),
        Rule(keyword=SUMMARY, description="把一段内容压缩成要点"),
        Rule(keyword=HELP_KEYWORD, requires_argument=False, description="显示这份帮助"),
    )


def build_rig(
    *,
    rules: tuple[Rule, ...] | None = None,
    overrides: Mapping[str, Service] | None = None,
) -> Rig:
    """装配一个不带飞书的核心。默认三个关键字都有记录型服务。"""
    resolved_rules = rules if rules is not None else build_rules()

    services: dict[str, Service] = {
        rule.keyword: RecordingService(rule.keyword) for rule in resolved_rules
    }
    if overrides:
        services.update(overrides)

    messenger = RecordingMessenger()
    handler = MessageHandler(
        messenger=messenger,
        seen=MemorySeenStore(),
        router=Router(resolved_rules),
        services=services,
    )
    return Rig(handler=handler, messenger=messenger, services=services)


def make_message(
    *,
    message_id: str = "om_1",
    chat_id: str = "oc_1",
    text: str = "你好",
    chat_type: str = "p2p",
) -> IncomingMessage:
    return IncomingMessage(
        message_id=message_id,
        chat_id=chat_id,
        sender_id="ou_user",
        text=text,
        chat_type=chat_type,
    )


class TestSeenStore:
    def test_first_claim_wins_and_repeats_lose(self) -> None:
        store = MemorySeenStore()

        assert store.claim("om_1") is True
        assert store.claim("om_1") is False
        assert store.claim("om_1") is False

    def test_distinct_keys_do_not_interfere(self) -> None:
        store = MemorySeenStore()

        assert store.claim("om_1") is True
        assert store.claim("om_2") is True

    def test_non_positive_ttl_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            MemorySeenStore(ttl_seconds=0)

    def test_key_can_be_claimed_again_after_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = FakeClock()
        monkeypatch.setattr(memory_module, "time", clock)
        store = MemorySeenStore(ttl_seconds=10)

        assert store.claim("om_1") is True
        clock.now += 9
        assert store.claim("om_1") is False
        clock.now += 2  # 累计 11 秒，已过期
        assert store.claim("om_1") is True

    def test_expiry_is_checked_even_below_the_sweep_threshold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """清扫是攒够量才做的，单条过期不能依赖它。"""
        clock = FakeClock()
        monkeypatch.setattr(memory_module, "time", clock)
        store = MemorySeenStore(ttl_seconds=10)

        store.claim("om_1")
        clock.now += DEFAULT_TTL_SECONDS * 100

        assert len(store) == 1  # 没触发清扫，过期条目还留在表里
        assert store.claim("om_1") is True


class TestDispatch:
    async def test_slash_command_goes_to_its_own_service_only(self) -> None:
        rig = build_rig()

        outcome = await rig.handler.handle(make_message(text="/搜索 transformer"))

        assert outcome is Outcome.HANDLED
        assert len(rig.recording(SEARCH).requests) == 1
        # 关键断言：另一个服务一次都没被碰过。
        assert rig.recording(SUMMARY).requests == []

    async def test_alias_goes_to_the_same_service(self) -> None:
        rig = build_rig()

        outcome = await rig.handler.handle(make_message(text="帮我查一下 transformer"))

        assert outcome is Outcome.HANDLED
        assert len(rig.recording(SEARCH).requests) == 1
        assert rig.recording(SUMMARY).requests == []

    async def test_each_keyword_maps_to_its_own_service(self) -> None:
        rig = build_rig()

        await rig.handler.handle(make_message(text="/摘要 这段", message_id="om_a"))
        await rig.handler.handle(make_message(text="/搜索 那段", message_id="om_b"))

        assert len(rig.recording(SEARCH).requests) == 1
        assert len(rig.recording(SUMMARY).requests) == 1

    async def test_service_receives_the_parsed_argument_not_the_raw_text(self) -> None:
        rig = build_rig()

        await rig.handler.handle(make_message(text="/搜索 transformer"))

        assert rig.recording(SEARCH).requests[0].argument == "transformer"

    async def test_alias_hit_gets_the_whole_text_as_argument(self) -> None:
        rig = build_rig()

        await rig.handler.handle(make_message(text="帮我查一下这段"))

        request = rig.recording(SEARCH).requests[0]
        assert request.argument == "帮我查一下这段"
        assert request.matched is MatchKind.MENTION
        assert request.trigger == "查一下"

    async def test_service_request_carries_the_whole_message(self) -> None:
        """服务拿得到 chat_id / sender_id / 群聊标记，而不只是一截文本。"""
        rig = build_rig()
        message = make_message(text="/搜索 x", chat_id="oc_9", chat_type="group")

        await rig.handler.handle(message)

        request = rig.recording(SEARCH).requests[0]
        assert request.message is message
        assert request.message.chat_id == "oc_9"
        assert request.message.is_group is True

    async def test_slash_hit_reports_the_keyword_as_trigger(self) -> None:
        rig = build_rig()

        await rig.handler.handle(make_message(text="/搜索 x"))

        assert rig.recording(SEARCH).requests[0].trigger == SEARCH

    async def test_help_command_goes_through_the_registry(self) -> None:
        """/帮助 没有特例分支——它就是注册表里的一个服务。"""
        rig = build_rig()

        outcome = await rig.handler.handle(make_message(text="/帮助"))

        assert outcome is Outcome.HANDLED
        assert len(rig.recording(HELP_KEYWORD).requests) == 1

    async def test_reply_goes_to_the_messages_chat(self) -> None:
        rig = build_rig()

        await rig.handler.handle(make_message(text="/搜索 x", chat_id="oc_42"))

        assert rig.messenger.sent[0][0] == "oc_42"

    async def test_custom_rules_are_honoured(self) -> None:
        """规则表是注入的——换一张表，路由立刻跟着变。"""
        rig = build_rig(rules=(Rule(keyword="纪要"),))

        await rig.handler.handle(make_message(text="/纪要 今天的会"))

        assert rig.recording("纪要").requests[0].argument == "今天的会"


class TestHelpAndUsage:
    async def test_unmatched_message_gets_the_help_card(self) -> None:
        rig = build_rig()

        outcome = await rig.handler.handle(make_message(text="今天天气不错"))

        assert outcome is Outcome.HELP
        assert len(rig.messenger.sent) == 1
        assert "/搜索" in rig.messenger.last.body

    async def test_unknown_command_gets_help_mentioning_the_token(self) -> None:
        rig = build_rig()

        outcome = await rig.handler.handle(make_message(text="/sousuo x"))

        assert outcome is Outcome.HELP
        assert "sousuo" in rig.messenger.last.body

    async def test_missing_argument_never_reaches_the_service(self) -> None:
        rig = build_rig()

        outcome = await rig.handler.handle(make_message(text="/搜索"))

        assert outcome is Outcome.USAGE
        assert rig.recording(SEARCH).requests == []
        assert "/搜索" in rig.messenger.last.body

    async def test_command_with_only_whitespace_after_it_needs_an_argument(self) -> None:
        rig = build_rig()

        assert await rig.handler.handle(make_message(text="/搜索    ")) is Outcome.USAGE
        assert rig.recording(SEARCH).requests == []

    async def test_keyword_that_takes_no_argument_is_dispatched(self) -> None:
        rig = build_rig()

        assert await rig.handler.handle(make_message(text="/帮助")) is Outcome.HANDLED


class TestFailures:
    async def test_service_failure_sends_a_warning_card(self) -> None:
        rig = build_rig(overrides={SEARCH: BrokenService()})

        outcome = await rig.handler.handle(make_message(text="/搜索 x"))

        assert outcome is Outcome.FAILED
        assert rig.messenger.last.title == FAILED_TITLE
        assert rig.messenger.last.tone is Tone.WARNING

    async def test_service_failure_does_not_escape(self) -> None:
        """兜底的意义就是不让异常跑到响应之后的那个后台任务里。"""
        rig = build_rig(overrides={SEARCH: BrokenService()})

        await rig.handler.handle(make_message(text="/搜索 x"))  # 不抛

    async def test_keyword_without_a_service_fails_gracefully(self) -> None:
        """bootstrap 会拦住这种配置，但用例层不该因此崩掉。"""
        rig = build_rig()
        del rig.services[SEARCH]

        outcome = await rig.handler.handle(make_message(text="/搜索 x"))

        assert outcome is Outcome.FAILED

    async def test_send_failure_is_not_swallowed_by_the_handler(self) -> None:
        """发送失败要抛出去。是否吞掉由调用方决定——入站适配器吞，测试不吞。"""
        handler = MessageHandler(
            messenger=BrokenMessenger(),
            seen=MemorySeenStore(),
            router=Router((Rule(keyword=SEARCH),)),
            services={SEARCH: RecordingService(SEARCH)},
        )

        with pytest.raises(RuntimeError):
            await handler.handle(make_message(text="/搜索 x"))


class TestUnreadable:
    async def test_empty_text_is_unreadable_and_never_routes(self) -> None:
        rig = build_rig()

        outcome = await rig.handler.handle(make_message(text="   "))

        assert outcome is Outcome.UNREADABLE
        assert all(rig.recording(keyword).requests == [] for keyword in rig.services)
        assert rig.messenger.last.title == UNREADABLE_TITLE


class TestDedup:
    async def test_duplicate_is_rejected_before_routing(self) -> None:
        """去重排在最前：路由是纯 CPU 的，但一次外呼就是一个真实的副作用。"""
        rig = build_rig()
        message = make_message(text="/搜索 x")

        assert await rig.handler.handle(message) is Outcome.HANDLED
        assert await rig.handler.handle(message) is Outcome.DUPLICATE

        assert len(rig.recording(SEARCH).requests) == 1
        assert len(rig.messenger.sent) == 1
