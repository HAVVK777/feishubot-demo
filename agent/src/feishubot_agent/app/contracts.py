"""boundary 上的请求/响应模型。

字段别名是 camelCase，因为 backend 的 `http_service.py` 已经那样发了；改成
snake_case 会让 backend 多一次翻译，没有必要。

单独一个模块是为了断开 `service.py` 和 `reply.py` 之间的循环导入：两者都依赖
契约，但不需要互相依赖。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TurnRequest(BaseModel):
    """backend 的外呼请求。`text` 是真正要处理的内容。"""

    model_config = ConfigDict(populate_by_name=True)

    keyword: str
    text: str
    message_id: str = Field(alias="messageId")
    chat_id: str = Field(alias="chatId")
    sender_id: str = Field(default="", alias="senderId")
    #: 飞书的原始取值 p2p / group。backend 现在的外呼 payload 还没有这个字段；
    #: 接入 MCP 的会话限流标签时需要在 `http_service.py` 里补一行。
    #: 空字符串表示未提供，调用 MCP 时不带这个标签。
    chat_type: str = Field(default="", alias="chatType")
    matched: str = ""
    trigger: str = ""


class TurnReply(BaseModel):
    """backend 会把它翻译成飞书卡片。agent 不构造卡片 JSON。"""

    title: str
    text: str
    tone: Literal["info", "success", "warning"] = "info"
