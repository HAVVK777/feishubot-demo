"""历史裁剪不留下半截回合。"""

from __future__ import annotations

from feishubot_agent.harness.context import trim_history, truncate_tool_result
from feishubot_agent.harness.model import AssistantMessage, ToolCall, UserMessage


def _history() -> list:
    return [
        UserMessage("a"),
        AssistantMessage("a1"),
        UserMessage("b"),
        AssistantMessage("b1", tool_calls=(ToolCall("call_1", "search", {}),)),
        UserMessage("c"),
        AssistantMessage("c1"),
    ]


def test_trim_keeps_the_last_n_user_turns() -> None:
    trimmed = trim_history(_history(), max_turns=2)

    assert trimmed == _history()[2:]


def test_trim_is_a_noop_when_history_is_short() -> None:
    history = _history()
    assert trim_history(history, max_turns=10) == history


def test_truncate_marks_the_loss() -> None:
    result = truncate_tool_result("x" * 20, max_chars=5)

    assert result.startswith("xxxxx")
    assert "已截断" in result
    assert "20" in result
