"""回合服务：历史只在成功后写回，并发按会话串行。"""

from __future__ import annotations

import asyncio
import calendar
from datetime import date

from feishubot_agent.app.contracts import TurnRequest
from feishubot_agent.app.profile import AgentProfile
from feishubot_agent.app.service import AgentTurnService
from feishubot_agent.harness.errors import ModelError
from feishubot_agent.harness.model import (
    AssistantMessage,
    Message,
    ToolCall,
    ToolResult,
    UserMessage,
)
from feishubot_agent.harness.session import SessionRegistry


class FakeModel:
    def __init__(self, responses: list[AssistantMessage | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[list[Message]] = []
        self.systems: list[str] = []

    async def complete(self, *, system, messages, tools) -> AssistantMessage:
        self.systems.append(system)
        self.calls.append(list(messages))
        if not self._responses:
            raise AssertionError("没有更多脚本化回复")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeExecutor:
    async def call(self, call: ToolCall) -> ToolResult:
        return ToolResult(call.id, call.name, "ok")


def _profile(*, max_steps: int = 3) -> AgentProfile:
    return AgentProfile(
        model="test-model",
        max_steps=max_steps,
        system_prompt="你是助手。",
        reply_title="助手",
    )


def _service(
    model,
    *,
    max_steps: int = 3,
    history_max_turns: int = 10,
    lock_wait: float = 1.0,
    turn_timeout: float = 5.0,
    executor_factory=None,
):
    registry = SessionRegistry(idle_ttl_seconds=60)
    service = AgentTurnService(
        profile=_profile(max_steps=max_steps),
        registry=registry,
        model=model,
        tools=(),
        executor_factory=executor_factory or (lambda session_id, chat_type: FakeExecutor()),
        turn_timeout_seconds=turn_timeout,
        history_max_turns=history_max_turns,
        session_lock_wait_seconds=lock_wait,
    )
    return service, registry


def _request(
    *,
    message_id: str = "m1",
    chat_id: str = "c1",
    text: str = "问",
    sender_id: str = "",
) -> TurnRequest:
    return TurnRequest(
        keyword="助手",
        text=text,
        message_id=message_id,
        chat_id=chat_id,
        sender_id=sender_id,
        chat_type="group",
    )


async def test_normal_turn_updates_history() -> None:
    model = FakeModel([AssistantMessage(text="答1"), AssistantMessage(text="答2")])
    service, registry = _service(model)

    first = await service.handle(_request(message_id="m1"))
    second = await service.handle(_request(message_id="m2"))

    assert first.text == "答1"
    assert second.text == "答2"
    assert len(model.calls[1]) == 3
    assert isinstance(model.calls[1][0], UserMessage)
    assert model.calls[1][0].text == "问"
    assert isinstance(model.calls[1][1], AssistantMessage)
    assert isinstance(model.calls[1][2], UserMessage)

    session = registry.get("c1")
    assert len(session.messages) == 4
    assert session.messages[0] == UserMessage("问")
    assert session.messages[1] == AssistantMessage(text="答1")


async def test_empty_reply_becomes_a_warning_and_is_not_committed() -> None:
    service, registry = _service(FakeModel([AssistantMessage(text="   ")]))

    reply = await service.handle(_request())

    assert reply.tone == "warning"
    assert reply.text
    assert registry.get("c1").messages == []


async def test_empty_request_is_rejected_without_calling_the_model() -> None:
    model = FakeModel([])
    service, _ = _service(model)

    reply = await service.handle(_request(text="   "))

    assert reply.tone == "warning"
    assert model.calls == []


async def test_model_error_leaves_history_untouched() -> None:
    service, registry = _service(FakeModel([ModelError("upstream down")]))

    reply = await service.handle(_request())

    assert reply.tone == "warning"
    assert registry.get("c1").messages == []


async def test_budget_exceeded_leaves_history_untouched() -> None:
    model = FakeModel(
        [
            AssistantMessage(
                text="",
                tool_calls=(ToolCall("t1", "search", {}),),
                stop_reason="tool_use",
            )
        ]
    )
    service, registry = _service(model, max_steps=1)

    reply = await service.handle(_request())

    assert reply.tone == "warning"
    assert registry.get("c1").messages == []


async def test_history_is_trimmed_by_user_turns() -> None:
    model = FakeModel([AssistantMessage(text="答1"), AssistantMessage(text="答2")])
    service, registry = _service(model, history_max_turns=1)

    await service.handle(_request(message_id="m1"))
    await service.handle(_request(message_id="m2"))

    session = registry.get("c1")
    assert session.messages == [UserMessage("问"), AssistantMessage(text="答2")]
    # 第二次请求只看到最近一个用户回合。
    assert model.calls[1] == [UserMessage("问")]


async def test_sender_open_id_is_added_to_the_system_prompt() -> None:
    model = FakeModel([AssistantMessage(text="ok")])
    service, _ = _service(model)

    await service.handle(_request(sender_id="ou_123"))

    assert "ou_123" in model.systems[0]
    today = date.today()
    month_start = today.replace(day=1)
    month_end = today.replace(day=calendar.monthrange(today.year, today.month)[1])
    assert month_start.isoformat() in model.systems[0]
    assert month_end.isoformat() in model.systems[0]


async def test_executor_factory_receives_chat_tags() -> None:
    seen: list[tuple[str, str]] = []

    def factory(session_id: str, chat_type: str):
        seen.append((session_id, chat_type))
        return FakeExecutor()

    service, _ = _service(FakeModel([AssistantMessage(text="ok")]), executor_factory=factory)

    await service.handle(_request(chat_id="oc_9"))

    assert seen == [("oc_9", "group")]


async def test_second_message_while_busy_returns_warning() -> None:
    class BlockingModel:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def complete(self, *, system, messages, tools) -> AssistantMessage:
            self.started.set()
            await self.release.wait()
            return AssistantMessage(text="ok")

    model = BlockingModel()
    service, _ = _service(model, lock_wait=0.01)

    first = asyncio.create_task(service.handle(_request(message_id="m1")))
    await model.started.wait()
    busy = await service.handle(_request(message_id="m2"))
    model.release.set()
    completed = await first

    assert completed.text == "ok"
    assert busy.tone == "warning"
    assert "上一条" in busy.text


async def test_turn_timeout_returns_warning() -> None:
    class SlowModel:
        async def complete(self, *, system, messages, tools) -> AssistantMessage:
            await asyncio.sleep(1)
            return AssistantMessage(text="too late")

    service, _ = _service(SlowModel(), turn_timeout=0.01)

    reply = await service.handle(_request())

    assert reply.tone == "warning"
    assert "超时" in reply.text
