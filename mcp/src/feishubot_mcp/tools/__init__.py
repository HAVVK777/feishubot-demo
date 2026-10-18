"""工具注册表：`MODULES` 里的模块各自用 `TOOLS` 元组声明工具，
`register_all()` 把它们装到 server 上。

工具函数为什么不写成 `@server.tool()` 的导入期装饰器：那需要一个模块级的 server
单例（导入即注册）。测试、同进程开两个实例、以及"只想 import 一个函数来看看"都会
被它牵连——和 `backend` 不建模块级 `app` 是同一条理由。

工具怎么写见 README 的[「加一个工具」](../../../README.md#加一个工具)。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from types import ModuleType

from mcp.server.mcpserver import MCPServer

# 重新导出，方便别处 `from feishubot_mcp.tools import hints`。**工具模块自己要从
# `feishubot_mcp.tools.hints` 导入**：包根会 import 各个工具模块，从这里导入就成环了。
from feishubot_mcp.tools import example, leave
from feishubot_mcp.tools.hints import annotations_of, describe, hints

__all__ = ["MODULES", "hints", "register_all"]

logger = logging.getLogger(__name__)

#: 要注册的工具模块。加一个模块就在这里加一行。
#: **故意不做目录自动扫描**：自动发现的话，"文件放错目录"和"文件写好了"表现一样，
#: 而显式清单漏了一行会在下面这个循环里立刻报出来。
MODULES: tuple[ModuleType, ...] = (example, leave)


def register_all(server: MCPServer, modules: Sequence[ModuleType] = MODULES) -> None:
    """把 `modules` 里的工具装到 server 上。`modules` 可传入是为了测试。

    重名**直接抛异常、拒绝启动**。SDK 自己的行为是打一条 warning 然后让后注册的盖掉
    先注册的——被盖掉的那个工具名调起来会打到另一个实现上，排查时看到的是"这个工具
    返回值不对"，离真正的原因很远。
    """
    owners: dict[str, str] = {}
    registered: list[tuple[str, str]] = []
    for module in modules:
        for fn in module.TOOLS:  # type: ignore[attr-defined]
            name = fn.__name__
            if (owner := owners.get(name)) is not None:
                raise RuntimeError(
                    f"工具重名：{name}（{module.__name__} 和 {owner}）。"
                    "函数名就是工具名，重名时客户端只能看到其中一个。"
                )
            owners[name] = module.__name__
            server.add_tool(fn, name=name, annotations=annotations_of(fn))
            registered.append((name, describe(annotations_of(fn))))

    if not registered:
        logger.warning("一个工具都没注册，客户端连上来只会看到一张空列表")
        return

    # 带上性质：哪个工具没声明（调用方无从判断能不能重试）一眼就能看出来。
    logger.info(
        "已注册 %d 个工具：%s",
        len(registered),
        "、".join(f"{name}（{hint}）" for name, hint in sorted(registered)),
    )
