"""一个用户回合的模型 ↔ 工具循环。

流程：

1. `budget.take_step()`；调用 `model.complete()`；
2. 把 `AssistantMessage` 追加进本回合消息；
3. 没有 `tool_calls` → 返回文本；
4. 有工具调用 → 并行执行，把**所有**结果放进一条 `ToolResultsMessage` 回传；
5. 回到 1，直到模型不再调用工具或预算耗尽。

两个容易写错的地方：

* 并行工具调用的结果必须放在**同一条** user 消息里。拆成多条会让模型以后不再并行
  调用工具。
* 工具失败也要回传 `is_error=True` 的 `ToolResult`，不要静默丢弃。模型需要知道
  发生了什么，才能决定换参数、换工具还是告诉用户。
* 自动重试管不住模型在下一步再发一次同名调用；`ToolExecutionBudget` 负责在
  “模型层”再设一道每回合的硬上限。

这个模块不 import 任何模型 SDK；它只依赖 `ModelClient` 和 `ToolExecutor`。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace

from feishubot_agent.harness.context import (
    DEFAULT_MAX_TOOL_RESULT_CHARS,
    truncate_tool_result,
)
from feishubot_agent.harness.errors import ToolCallError
from feishubot_agent.harness.model import (
    AssistantMessage,
    Message,
    ModelClient,
    StopReason,
    ToolCall,
    ToolResult,
    ToolResultsMessage,
    ToolSpec,
    Usage,
)
from feishubot_agent.harness.policy import (
    DEFAULT_MAX_EXECUTIONS_PER_FINGERPRINT,
    DEFAULT_MAX_TOOL_EXECUTIONS,
    ToolExecutionBudget,
    TurnBudget,
    backoff_delay,
    should_retry,
)
from feishubot_agent.harness.tools import ToolExecutor

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOOL_ATTEMPTS = 2
DEFAULT_RETRY_BASE_DELAY = 0.5
DEFAULT_RETRY_CAP_SECONDS = 4.0


@dataclass(frozen=True, slots=True)
class TurnResult:
    """一个回合的结果。

    `new_messages` 只包含本回合新增的消息，调用方把它追加到会话历史即可。
    """

    text: str
    new_messages: tuple[Message, ...]
    steps: int
    stop_reason: StopReason = "end_turn"


async def run_turn(
    *,
    model: ModelClient,
    system: str,
    history: Sequence[Message],
    tools: Sequence[ToolSpec],
    executor: ToolExecutor,
    budget: TurnBudget,
    max_tool_attempts: int = DEFAULT_MAX_TOOL_ATTEMPTS,
    retry_base_delay: float = DEFAULT_RETRY_BASE_DELAY,
    retry_cap_seconds: float = DEFAULT_RETRY_CAP_SECONDS,
    max_tool_result_chars: int = DEFAULT_MAX_TOOL_RESULT_CHARS,
    max_executions_per_fingerprint: int = DEFAULT_MAX_EXECUTIONS_PER_FINGERPRINT,
    max_tool_executions: int = DEFAULT_MAX_TOOL_EXECUTIONS,
) -> TurnResult:
    """执行一个回合。`history` 必须已经包含本条用户消息。"""
    _validate(
        max_tool_attempts=max_tool_attempts,
        retry_base_delay=retry_base_delay,
        retry_cap_seconds=retry_cap_seconds,
        max_tool_result_chars=max_tool_result_chars,
        max_executions_per_fingerprint=max_executions_per_fingerprint,
        max_tool_executions=max_tool_executions,
    )

    tool_budget = ToolExecutionBudget(
        max_per_fingerprint=max_executions_per_fingerprint,
        max_total=max_tool_executions,
    )
    messages = list(history)
    first_new = len(messages)
    usage = Usage()
    steps = 0
    final: AssistantMessage | None = None

    while True:
        budget.take_step()
        steps = budget.steps

        assistant = await model.complete(system=system, messages=messages, tools=tools)
        messages.append(assistant)
        usage = _add_usage(usage, assistant.usage)

        if not assistant.tool_calls:
            if assistant.stop_reason == "tool_use":
                logger.warning("模型返回 stop_reason=tool_use，但没有 tool_use block，按结束处理")
            final = assistant
            break

        logger.info(
            "第 %d 步：模型请求 %d 个工具（%s）",
            steps,
            len(assistant.tool_calls),
            "、".join(call.name for call in assistant.tool_calls),
        )

        results = await asyncio.gather(
            *(
                _execute_tool(
                    executor,
                    call,
                    budget=budget,
                    tool_budget=tool_budget,
                    max_attempts=max_tool_attempts,
                    retry_base_delay=retry_base_delay,
                    retry_cap_seconds=retry_cap_seconds,
                    max_result_chars=max_tool_result_chars,
                )
                for call in assistant.tool_calls
            )
        )
        messages.append(ToolResultsMessage(results=tuple(results)))

    text = final.text if final is not None else ""
    stop_reason = final.stop_reason if final is not None else "other"
    logger.info(
        "回合结束：%d 步，stop_reason=%s，输入 %d tokens，输出 %d tokens，文本 %d 字符",
        steps,
        stop_reason,
        usage.input_tokens,
        usage.output_tokens,
        len(text),
    )
    return TurnResult(
        text=text,
        new_messages=tuple(messages[first_new:]),
        steps=steps,
        stop_reason=stop_reason,
    )


async def _execute_tool(
    executor: ToolExecutor,
    call: ToolCall,
    *,
    budget: TurnBudget,
    tool_budget: ToolExecutionBudget,
    max_attempts: int,
    retry_base_delay: float,
    retry_cap_seconds: float,
    max_result_chars: int,
) -> ToolResult:
    """执行一次工具调用，按错误性质决定要不要重试。

    **超时和未知失败不重试**：`ToolCallError.retryable` 已经在那两种情况下是 False。
    重试的等待时间也不能超过回合剩余预算，否则只是把超时往后拖。

    每次真正执行前都要向 `tool_budget` 申请额度。自动重试也占额度，所以
    “重试两次后再被模型重新调用”会被硬性拦住，而不是只靠提示词。
    """
    attempts = 0
    last_error: str | None = None

    while True:
        denial = tool_budget.reserve(call)
        if denial is not None:
            if last_error:
                denial = f"{denial}（最后一次错误：{last_error}）"
            logger.warning("工具 %s 被工具执行预算拦截：%s", call.name, denial)
            return ToolResult(
                call_id=call.id,
                name=call.name,
                content=denial,
                is_error=True,
            )

        attempts += 1
        try:
            result = await executor.call(call)
        except ToolCallError as exc:
            last_error = str(exc) or "工具调用失败。"
            if should_retry(exc, attempts=attempts, max_attempts=max_attempts):
                delay = backoff_delay(attempts, base=retry_base_delay, cap=retry_cap_seconds)
                if exc.retry_after_seconds is not None:
                    delay = min(retry_cap_seconds, max(delay, exc.retry_after_seconds))
                if delay < budget.remaining_seconds():
                    logger.warning(
                        "工具 %s 第 %d 次失败（kind=%s），%.2f 秒后重试",
                        call.name,
                        attempts,
                        exc.kind,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue

            logger.warning(
                "工具 %s 失败（kind=%s，retryable=%s）：%s",
                call.name,
                exc.kind,
                exc.retryable,
                exc,
            )
            content = last_error
            if attempts > 1:
                # 让模型明确知道自动重试已经用掉，不要再发起同名调用。
                content += (
                    f"\n（这个工具已经尝试 {attempts} 次仍未成功；"
                    "请不要再调用它，把情况告诉用户。）"
                )
            return ToolResult(
                call_id=call.id,
                name=call.name,
                content=content,
                is_error=True,
            )
        except Exception:
            # 未预料的崩溃：细节只进日志，回给模型一句不含内部信息的话。
            logger.exception("工具 %s 崩溃", call.name)
            return ToolResult(
                call_id=call.id,
                name=call.name,
                content="工具执行失败（内部错误），详情已记录在服务端日志。",
                is_error=True,
            )
        else:
            return _truncate(result, max_result_chars)


def _truncate(result: ToolResult, max_chars: int) -> ToolResult:
    content = truncate_tool_result(result.content, max_chars=max_chars)
    if content == result.content:
        return result
    return replace(result, content=content)


def _add_usage(total: Usage, step: Usage | None) -> Usage:
    if step is None:
        return total
    return Usage(
        input_tokens=total.input_tokens + step.input_tokens,
        output_tokens=total.output_tokens + step.output_tokens,
    )


def _validate(
    *,
    max_tool_attempts: int,
    retry_base_delay: float,
    retry_cap_seconds: float,
    max_tool_result_chars: int,
    max_executions_per_fingerprint: int,
    max_tool_executions: int,
) -> None:
    if max_tool_attempts < 1:
        raise ValueError("max_tool_attempts 必须至少为 1")
    if retry_base_delay < 0:
        raise ValueError("retry_base_delay 不能为负数")
    if retry_cap_seconds < 0:
        raise ValueError("retry_cap_seconds 不能为负数")
    if max_tool_result_chars <= 0:
        raise ValueError("max_tool_result_chars 必须大于 0")
    if max_executions_per_fingerprint < 1:
        raise ValueError("max_executions_per_fingerprint 必须至少为 1")
    if max_tool_executions < 1:
        raise ValueError("max_tool_executions 必须至少为 1")
