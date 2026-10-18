"""历史裁剪。

v1 只做裁剪，不做摘要。摘要需要额外一次模型调用，而且会悄悄改变对话内容；等真的
撞上上下文上限再引入，并且要留下明确的日志。
"""

from __future__ import annotations

from collections.abc import Sequence

from feishubot_agent.harness.model import Message, UserMessage

DEFAULT_MAX_TOOL_RESULT_CHARS = 8_000


def trim_history(messages: Sequence[Message], *, max_turns: int) -> list[Message]:
    """保留最近 `max_turns` 个用户回合。

    从第 N 个用户消息开始切：它后面的工具调用、工具结果、最终回复都跟着保留，不会
    留下「有 tool_result 却没有对应 tool_use」的残片。
    """

    if max_turns <= 0:
        raise ValueError("max_turns 必须大于 0")

    starts = [index for index, message in enumerate(messages) if isinstance(message, UserMessage)]
    if len(starts) <= max_turns:
        return list(messages)
    return list(messages[starts[-max_turns] :])


def truncate_tool_result(
    text: str,
    *,
    max_chars: int = DEFAULT_MAX_TOOL_RESULT_CHARS,
) -> str:
    """截断过长的工具结果，并留下明确的标记。

    工具结果通常比用户输入大一个数量级，原样塞回模型很容易把上下文吃满。截断是
    有损的，所以必须告诉模型「这里被截了」，而不是让它以为结果就这么多。
    """

    if max_chars <= 0:
        raise ValueError("max_chars 必须大于 0")
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n…（工具结果已截断，原文共 {len(text)} 字符）"
