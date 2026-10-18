"""工具报错的约定：预期内的失败抛 `ToolError`。

两种失败要分开，混在一起会让模型（或者看日志的人）拿到错误的结论：

* **你预料到的失败**——参数不对、上游返回"查不到"、对方接口限流。抛
  `ToolError("人话")`：这句话会**原样送到模型手里**，模型能据此改参数重试或者
  告诉用户。日志里只记 INFO，没有堆栈。
* **没预料到的 bug**——空指针、类型错、连不上数据库。别接，让它抛。SDK 会把它
  包成 `UnexpectedToolError`，模型只看到一句 `Error executing tool <名字>`，
  堆栈只进服务端日志。**异常文本不会漏给调用方**——消息里混着 token、SQL、
  内部主机名是常态。

所以工具里那句 `raise ToolError(...)` 的话是写给模型看的：说清楚"哪里不对、
可以怎么办"，不要贴堆栈。

这里重新导出而不是让工具直接 import mcp 的内部路径：2.3 已经改过一次名字
（`FastMCP` → `MCPServer`），工具代码不该跟着协议库的目录结构动。
"""

from __future__ import annotations

from mcp.server.mcpserver.exceptions import ToolError

__all__ = ["ToolError"]
