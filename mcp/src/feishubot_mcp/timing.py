"""工具执行时间的记录口径。

耗时放在 `CallToolResult._meta` 里，而不是正文里：

* `_meta` 是协议层的开放字段，**不会进入模型看到的 content**；
* 调用方（agent）可以读它做日志/指标，模型不会把它当成工具结果的一部分；
* 正文保持干净，不需要为了一个时间戳去改工具函数的返回值。

键名是固定的 `execution_time_ms`，调用方按这个键读取，不要解析中文日志。
"""

from __future__ import annotations

from typing import Any

EXECUTION_TIME_META_KEY = "execution_time_ms"


def execution_time_meta(elapsed_seconds: float) -> dict[str, Any]:
    """把一次工具调用的耗时（秒）做成 MCP 的 `_meta` 字段。

    只返回 `_meta` 的内容，调用方负责合并进 `result.meta`。保留 3 位小数（微秒级），
    避免浮点误差把同一毫秒的记录写成两个值。
    """
    return {EXECUTION_TIME_META_KEY: round(elapsed_seconds * 1000, 3)}
