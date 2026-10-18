"""回合循环：工具结果合并、错误回传、重试与预算。"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from feishubot_agent.harness.errors import BudgetExceeded, ToolCallError
from feishubot_agent.harness.loop import run_turn
from feishubot_agent.harness.model import (
    AssistantMessage,
    Message,
    ToolCall,
    ToolResult,
    ToolResultsMessage,
    UserMessage,
)
from feishubot_agent.harness.policy import TurnBudget


class FakeModel:
    def __init__(self, responses: list[AssistantMessage | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[list[Message]] = []

    async def complete(self, *, system, messages, tools) -> AssistantMessage:
        self.calls.append(list(messages))
        if not self._responses:
            raise AssertionError("没有更多脚本化回复")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeExecutor:
    def __init__(self, handler: Callable[[ToolCall], object]) -> None:
        self._handler = handler
        self.calls: list[ToolCall] = []

    async def call(self, call: ToolCall) -> ToolResult:
        self.calls.append(call)
        result = self._handler(call)
        if isinstance(result, Exception):
            raise result
        assert isinstance(result, ToolResult)
        return result


def _budget(*, max_steps: int = 5, timeout: float = 30.0) -> TurnBudget:
    return TurnBudget.start(max_steps=max_steps, timeout_seconds=timeout)


async def _run(
    model: FakeModel,
    executor: FakeExecutor,
    *,
    budget: TurnBudget | None = None,
    **kwargs,
):
    return await run_turn(
        model=model,
        system="sys",
        history=[UserMessage("hi")],
        tools=(),
        executor=executor,
        budget=budget or _budget(),
        retry_base_delay=0,
        **kwargs,
    )


async def test_returns_text_without_tool_calls() -> None:
    model = FakeModel([AssistantMessage(text="你好")])
    executor = FakeExecutor(lambda call: ToolResult(call.id, call.name, "unused"))

    result = await _run(model, executor)

    assert result.text == "你好"
    assert result.steps == 1
    assert result.stop_reason == "end_turn"
    assert executor.calls == []
    assert len(result.new_messages) == 1


async def test_parallel_tool_results_are_one_message() -> None:
    calls = (ToolCall("t1", "search", {"q": "a"}), ToolCall("t2", "fetch", {"id": "b"}))
    model = FakeModel(
        [
            AssistantMessage(text="", tool_calls=calls, stop_reason="tool_use"),
            AssistantMessage(text="done"),
        ]
    )
    executor = FakeExecutor(lambda call: ToolResult(call.id, call.name, f"{call.name}-ok"))

    result = await _run(model, executor)

    results_message = result.new_messages[1]
    assert isinstance(results_message, ToolResultsMessage)
    assert [item.call_id for item in results_message.results] == ["t1", "t2"]
    assert [call.name for call in executor.calls] == ["search", "fetch"]
    # 第二次模型调用必须看到一条包含两个结果的 user 消息。
    assert model.calls[1][-1] is results_message


async def test_tool_error_is_returned_as_is_error() -> None:
    model = FakeModel(
        [
            AssistantMessage(
                text="",
                tool_calls=(ToolCall("t1", "search", {}),),
                stop_reason="tool_use",
            ),
            AssistantMessage(text="failed gracefully"),
        ]
    )

    def handler(call: ToolCall) -> Exception:
        return ToolCallError("查不到数据", kind="unknown", retryable=False)

    result = await _run(model, FakeExecutor(handler))

    results_message = result.new_messages[1]
    assert isinstance(results_message, ToolResultsMessage)
    assert results_message.results[0].is_error is True
    assert "查不到数据" in results_message.results[0].content


async def test_unexpected_tool_exception_does_not_leak_details() -> None:
    model = FakeModel(
        [
            AssistantMessage(
                text="",
                tool_calls=(ToolCall("t1", "search", {}),),
                stop_reason="tool_use",
            ),
            AssistantMessage(text="ok"),
        ]
    )

    def handler(call: ToolCall) -> Exception:
        return RuntimeError("secret internal path")

    result = await _run(model, FakeExecutor(handler))

    results_message = result.new_messages[1]
    assert isinstance(results_message, ToolResultsMessage)
    assert "secret internal path" not in results_message.results[0].content


async def test_retryable_error_is_retried() -> None:
    model = FakeModel(
        [
            AssistantMessage(
                text="",
                tool_calls=(ToolCall("t1", "search", {}),),
                stop_reason="tool_use",
            ),
            AssistantMessage(text="ok"),
        ]
    )
    attempts = {"count": 0}

    def handler(call: ToolCall) -> ToolResult | Exception:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return ToolCallError("限流", kind="rate_limited", retryable=True)
        return ToolResult(call.id, call.name, "ok")

    executor = FakeExecutor(handler)
    result = await _run(model, executor)

    assert attempts["count"] == 2
    results_message = result.new_messages[1]
    assert isinstance(results_message, ToolResultsMessage)
    assert results_message.results[0].is_error is False


async def test_timeout_is_not_retried() -> None:
    model = FakeModel(
        [
            AssistantMessage(
                text="",
                tool_calls=(ToolCall("t1", "search", {}),),
                stop_reason="tool_use",
            ),
            AssistantMessage(text="ok"),
        ]
    )
    executor = FakeExecutor(lambda call: ToolCallError("超时", kind="timeout", retryable=False))

    await _run(model, executor)

    assert len(executor.calls) == 1


async def test_budget_exceeded_stops_the_loop() -> None:
    model = FakeModel(
        [
            AssistantMessage(
                text="",
                tool_calls=(ToolCall(f"t{i}", "search", {}),),
                stop_reason="tool_use",
            )
            for i in range(3)
        ]
    )
    executor = FakeExecutor(lambda call: ToolResult(call.id, call.name, "ok"))

    with pytest.raises(BudgetExceeded):
        await _run(model, executor, budget=_budget(max_steps=2))

    assert len(model.calls) == 2


async def test_identical_tool_call_is_blocked_after_the_budget() -> None:
    model = FakeModel(
        [
            AssistantMessage(
                text="",
                tool_calls=(ToolCall("t1", "search", {}),),
                stop_reason="tool_use",
            ),
            AssistantMessage(
                text="",
                tool_calls=(ToolCall("t2", "search", {}),),
                stop_reason="tool_use",
            ),
            AssistantMessage(
                text="",
                tool_calls=(ToolCall("t3", "search", {}),),
                stop_reason="tool_use",
            ),
            AssistantMessage(text="done"),
        ]
    )
    executor = FakeExecutor(lambda call: ToolResult(call.id, call.name, "ok"))

    result = await _run(model, executor)

    assert len(executor.calls) == 2
    third_results = result.new_messages[5]
    assert isinstance(third_results, ToolResultsMessage)
    assert third_results.results[0].is_error is True
    assert "达到上限" in third_results.results[0].content
    assert result.text == "done"


async def test_automatic_retries_consume_the_budget() -> None:
    model = FakeModel(
        [
            AssistantMessage(
                text="",
                tool_calls=(ToolCall("t1", "search", {}),),
                stop_reason="tool_use",
            ),
            AssistantMessage(
                text="",
                tool_calls=(ToolCall("t2", "search", {}),),
                stop_reason="tool_use",
            ),
            AssistantMessage(text="done"),
        ]
    )
    executor = FakeExecutor(
        lambda call: ToolCallError("limited", kind="rate_limited", retryable=True)
    )

    result = await _run(model, executor)

    # 第 1 次模型调用内部自动重试一次，已经用掉 2 次额度。
    assert len(executor.calls) == 2
    first_results = result.new_messages[1]
    assert isinstance(first_results, ToolResultsMessage)
    assert "尝试 2 次" in first_results.results[0].content

    # 第 2 次模型调用再试同名同参数：直接拒绝，不再执行。
    second_results = result.new_messages[3]
    assert isinstance(second_results, ToolResultsMessage)
    assert second_results.results[0].is_error is True
    assert "达到上限" in second_results.results[0].content
    assert result.text == "done"


async def test_different_arguments_have_separate_budgets() -> None:
    model = FakeModel(
        [
            AssistantMessage(
                text="",
                tool_calls=(ToolCall("t1", "search", {"q": "a"}),),
                stop_reason="tool_use",
            ),
            AssistantMessage(
                text="",
                tool_calls=(ToolCall("t2", "search", {"q": "a"}),),
                stop_reason="tool_use",
            ),
            AssistantMessage(
                text="",
                tool_calls=(ToolCall("t3", "search", {"q": "b"}),),
                stop_reason="tool_use",
            ),
            AssistantMessage(text="done"),
        ]
    )
    executor = FakeExecutor(lambda call: ToolResult(call.id, call.name, "ok"))

    result = await _run(model, executor)

    assert len(executor.calls) == 3
    assert result.text == "done"


async def test_global_tool_execution_budget_is_enforced() -> None:
    model = FakeModel(
        [
            AssistantMessage(
                text="",
                tool_calls=(ToolCall("t1", "search", {"q": "a"}),),
                stop_reason="tool_use",
            ),
            AssistantMessage(
                text="",
                tool_calls=(ToolCall("t2", "search", {"q": "b"}),),
                stop_reason="tool_use",
            ),
            AssistantMessage(
                text="",
                tool_calls=(ToolCall("t3", "search", {"q": "c"}),),
                stop_reason="tool_use",
            ),
            AssistantMessage(text="done"),
        ]
    )
    executor = FakeExecutor(lambda call: ToolResult(call.id, call.name, "ok"))

    result = await _run(model, executor, max_tool_executions=2)

    assert len(executor.calls) == 2
    third_results = result.new_messages[5]
    assert isinstance(third_results, ToolResultsMessage)
    assert "工具执行次数已经达到上限" in third_results.results[0].content


async def test_long_tool_results_are_truncated() -> None:
    model = FakeModel(
        [
            AssistantMessage(
                text="",
                tool_calls=(ToolCall("t1", "search", {}),),
                stop_reason="tool_use",
            ),
            AssistantMessage(text="ok"),
        ]
    )
    executor = FakeExecutor(lambda call: ToolResult(call.id, call.name, "x" * 100))

    result = await run_turn(
        model=model,
        system="sys",
        history=[UserMessage("hi")],
        tools=(),
        executor=executor,
        budget=_budget(),
        max_tool_result_chars=10,
        retry_base_delay=0,
    )

    results_message = result.new_messages[1]
    assert isinstance(results_message, ToolResultsMessage)
    assert "已截断" in results_message.results[0].content
