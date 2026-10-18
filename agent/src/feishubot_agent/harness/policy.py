"""回合预算与重试判定。

重试的分工和 MCP 设计保持一致：

| 失败 | 谁决定 | 动作 |
|---|---|---|
| `rate_limited` | 这里 | 按退避有界重试 |
| `connection` | 这里 | 有界重试 |
| `timeout` | 模型 | 不自动重试，把 hints 交给模型判断 |
| `unauthorized` / `missing_meta` | 调用方 | 不重试，修配置 |

**自动重试只适用于「确定这次没有成功」的失败。** 超时明确表示「不知道」，所以
`ToolCallError.retryable` 在超时上是 False。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from feishubot_agent.harness.errors import BudgetExceeded, ToolCallError
from feishubot_agent.harness.model import ToolCall

#: 同一个工具 + 同一组参数，一个回合内最多真正执行几次（包括自动重试）。
DEFAULT_MAX_EXECUTIONS_PER_FINGERPRINT = 2
#: 一个回合内最多真正执行多少次工具（包括自动重试）。
DEFAULT_MAX_TOOL_EXECUTIONS = 16


@dataclass(slots=True)
class TurnBudget:
    """一个用户回合的步数与时间预算。"""

    max_steps: int
    deadline: float
    steps: int = 0

    @classmethod
    def start(
        cls,
        *,
        max_steps: int,
        timeout_seconds: float,
        now: float | None = None,
    ) -> TurnBudget:
        if max_steps <= 0:
            raise ValueError("max_steps 必须大于 0")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        current = time.monotonic() if now is None else now
        return cls(max_steps=max_steps, deadline=current + timeout_seconds)

    def take_step(self, *, now: float | None = None) -> None:
        """在每次模型调用前调用；超预算直接结束回合。"""
        current = time.monotonic() if now is None else now
        if self.steps >= self.max_steps:
            raise BudgetExceeded(f"本回合已达到 {self.max_steps} 步上限")
        if current >= self.deadline:
            raise BudgetExceeded("本回合已超过时间预算")
        self.steps += 1

    def remaining_seconds(self, *, now: float | None = None) -> float:
        current = time.monotonic() if now is None else now
        return max(0.0, self.deadline - current)


@dataclass(slots=True)
class ToolExecutionBudget:
    """一个回合内的工具执行预算。

    自动重试只能管住**一次**工具调用内部的循环；模型看到错误后，仍然可以在下一步
    再发起一次同名同参数的调用。这个预算把那条路也堵上：

    * 同一个 `(工具名, 参数)` 在一个回合里最多执行 `max_per_fingerprint` 次
      （默认 2，包含自动重试）；
    * 整个回合最多执行 `max_total` 次工具（默认 16，包含自动重试）。

    `reserve()` 没有 `await`，并行工具调用共享同一个预算时不会交错。
    """

    max_per_fingerprint: int = DEFAULT_MAX_EXECUTIONS_PER_FINGERPRINT
    max_total: int = DEFAULT_MAX_TOOL_EXECUTIONS
    _per_fingerprint: dict[str, int] = field(default_factory=dict)
    _total: int = 0

    def reserve(self, call: ToolCall) -> str | None:
        """占用一次执行额度。返回 None 表示放行；否则返回给模型看的拒绝原因。"""
        fingerprint = _fingerprint(call)
        used = self._per_fingerprint.get(fingerprint, 0)
        if used >= self.max_per_fingerprint:
            return (
                f"本次回合里工具 {call.name}（相同参数）已经执行过 {used} 次，"
                "达到上限；不要再调用它，请根据已有结果回答，或告诉用户稍后再试。"
            )
        if self._total >= self.max_total:
            return (
                f"本次回合的工具执行次数已经达到上限（{self.max_total} 次）；"
                "不要再调用工具，请根据已有结果回答，或告诉用户稍后再试。"
            )
        self._per_fingerprint[fingerprint] = used + 1
        self._total += 1
        return None


def _fingerprint(call: ToolCall) -> str:
    """同一个工具 + 同一组参数才算“重复”。参数排序后再序列化，避免键顺序不同漏判。"""
    try:
        arguments = json.dumps(
            dict(call.arguments),
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
    except (TypeError, ValueError):
        arguments = repr(call.arguments)
    return f"{call.name}:{arguments}"


def should_retry(error: ToolCallError, *, attempts: int, max_attempts: int) -> bool:
    """`attempts` 是已经失败的次数（1 表示第一次失败）。"""
    return error.retryable and attempts < max_attempts


def backoff_delay(attempt: int, *, base: float = 0.5, cap: float = 4.0) -> float:
    """第 n 次失败后的等待时间，指数退避并封顶。"""
    if attempt < 1:
        raise ValueError("attempt 从 1 开始")
    return min(cap, base * (2 ** (attempt - 1)))
