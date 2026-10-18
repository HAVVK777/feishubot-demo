"""三个示例工具。

`ping` 和 `add` 是占位，写真实工具时删掉；`whoami` 建议留着——验证 token 配置对不对
的时候要用它。

工具怎么写（docstring 的写法、注解要多具体、`@hints` 怎么用）见 README 的
[「加一个工具」](../../../README.md#加一个工具)。
"""

from __future__ import annotations

from feishubot_mcp.auth import caller_name
from feishubot_mcp.tools.hints import hints


@hints(read_only=True)
async def ping(text: str) -> str:
    """连通性自检：把传进来的内容原样返回。用来确认服务活着、参数能传到工具里。"""
    return text


@hints(read_only=True)
async def add(a: int, b: int) -> int:
    """算两个整数的和。用来演示带类型的入参和结构化返回值。"""
    return a + b


@hints(read_only=True)
async def whoami() -> str:
    """报告这次调用是哪个调用方发起的。验证 token 配置对不对时用它。

    调用方来自**已验证**的 token，不是请求头里客户端自称的东西——见 `auth.caller_name`。
    """
    return caller_name() or "没有调用方信息（stdio 或进程内调用）"


#: 这个模块对外提供的工具。函数名即工具名，重名会在启动时直接报错。
TOOLS = (ping, add, whoami)
