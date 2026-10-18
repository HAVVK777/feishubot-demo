"""工具目录。

MCP 的 `tools/list` 在 `mcp_client.py` 里变成中立的 `ToolSpec`；这个模块负责两件
**模型看得到**的事：

1. 把 MCP 的 `annotations`（`@hints`）渲染进工具描述。模型据此判断超时后能不能重试；
2. 按白名单过滤工具。白名单写错（名字拼错、工具被删了）**在启动时**就报错，
   不要等到模型调到一个不存在的名字。

`loop.py` 只认 `ToolExecutor` 协议，不认识 MCP。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from typing import Protocol

from feishubot_agent.harness.model import ToolCall, ToolResult, ToolSpec


class ToolExecutor(Protocol):
    """执行一次工具调用。实现方通常是 MCP 客户端。"""

    async def call(self, call: ToolCall) -> ToolResult: ...


def with_hints(
    spec: ToolSpec,
    *,
    read_only: bool | None = None,
    idempotent: bool | None = None,
    destructive: bool | None = None,
) -> ToolSpec:
    """把工具性质追加到描述末尾。

    语义沿用 MCP 的 `ToolAnnotations`：`read_only` 为真时其余几个没有意义；
    没声明就不加任何文字。模型看不到 hints 时，只会把超时当成普通失败。
    """

    labels: list[str] = []
    if read_only:
        labels.append("只读")
    elif read_only is False or idempotent is not None or destructive:
        labels.append("有副作用")
        if idempotent is not None:
            labels.append("可重复调用" if idempotent else "不可重复调用")
        if destructive:
            labels.append("破坏性")

    if not labels:
        return spec

    description = spec.description.rstrip()
    # 中文描述后面直接接全角括号；英文描述需要一个空格才不会连在一起。
    if description and description[-1].isascii() and not description[-1].isspace():
        description += " "
    description += "（" + "，".join(labels) + "）"
    return replace(spec, description=description)


class ToolCatalog:
    """启动时定下来的工具清单。

    `allowed=None` 表示全部放行；传空元组表示一个都不放行。重名和白名单里不存在
    的名字都直接抛异常——两者都只会在配置/工具改动后出现，越早发现越好。
    """

    def __init__(
        self,
        specs: Iterable[ToolSpec],
        *,
        allowed: Iterable[str] | None = None,
    ) -> None:
        by_name: dict[str, ToolSpec] = {}
        for spec in specs:
            if spec.name in by_name:
                raise ValueError(f"工具重名：{spec.name}")
            by_name[spec.name] = spec

        names = tuple(by_name) if allowed is None else tuple(allowed)
        missing = [name for name in names if name not in by_name]
        if missing:
            raise ValueError("工具白名单里有不存在的名字：" + "、".join(missing))

        self._specs = tuple(by_name[name] for name in names)

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        return self._specs
