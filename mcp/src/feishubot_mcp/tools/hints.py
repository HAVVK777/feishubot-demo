"""工具的性质声明。

单独一个模块是**为了断开循环导入**：`tools/__init__.py` 要 import 各个工具模块（为了
拿到 `TOOLS`），所以工具模块不能再回头 import 包根本身。工具模块从
`feishubot_mcp.tools.hints` 拿这个装饰器，而不是从 `feishubot_mcp.tools`。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from mcp.types import ToolAnnotations

F = TypeVar("F", bound=Callable[..., Any])

#: 声明挂在函数属性上，不改变函数本身——工具还是个能直接调用、直接单测的普通函数。
HINTS_ATTR = "__mcp_hints__"


def hints(
    *,
    read_only: bool | None = None,
    idempotent: bool | None = None,
    destructive: bool | None = None,
    open_world: bool | None = None,
) -> Callable[[F], F]:
    """声明工具的性质。**这是调用方判断"能不能重试"的唯一依据**，别省这一步。

    可以叠着用：

        @hints(read_only=True)               # 只读：查东西、算东西，不改任何状态
        @hints(idempotent=True)              # 有副作用，但重复调用不会多产生一遍
        @hints(destructive=True)             # 破坏性：删除、覆盖、不可逆

    语义按 MCP 的 `ToolAnnotations`：`read_only` 为真时另外几个没有意义；
    `idempotent` / `destructive` 只在有副作用时才有意义。

    **不声明 = 什么都不说**，调用方只能按"可能有副作用"对待（安全的那一侧）。反过来，
    把有副作用的工具标成 `read_only` 是这里唯一会出事的错法：调用方会放心重试。

    标了之后会出现在 `tools/list` 里——**模型自己也看得到**，它据此决定要不要重试、
    要不要先跟用户确认。
    """

    def decorate(fn: F) -> F:
        declared: dict[str, Any] = {
            "read_only_hint": read_only,
            "idempotent_hint": idempotent,
            "destructive_hint": destructive,
            "open_world_hint": open_world,
        }
        current: ToolAnnotations = getattr(fn, HINTS_ATTR, ToolAnnotations())
        # 叠着用的时候按字段合并，后写的覆盖先写的。
        setattr(
            fn,
            HINTS_ATTR,
            current.model_copy(update={k: v for k, v in declared.items() if v is not None}),
        )
        return fn

    return decorate


def annotations_of(fn: Callable[..., Any]) -> ToolAnnotations | None:
    """取一个工具声明过的性质，没声明返回 None。"""
    return getattr(fn, HINTS_ATTR, None)


def describe(annotations: ToolAnnotations | None) -> str:
    """一句话概括，只用于启动日志。

    "未声明"要显式打出来——它意味着调用方无从判断能不能重试，值得在启动时看一眼。
    """
    if annotations is None:
        return "未声明"
    if annotations.read_only_hint:
        return "只读"
    parts = ["有副作用", "可重复调用" if annotations.idempotent_hint else "不可重复调用"]
    if annotations.destructive_hint:
        parts.append("破坏性")
    return "、".join(parts)
