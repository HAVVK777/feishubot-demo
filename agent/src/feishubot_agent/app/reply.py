"""最终文本 → backend 的卡片字段。

飞书的标题、颜色、卡片 JSON 都在 backend；这里只决定「说什么」。
"""

from __future__ import annotations

from feishubot_agent.app.contracts import TurnReply


def to_reply(text: str, *, title: str = "助手") -> TurnReply:
    """空回复不能原样返回：backend 会把空 `text` 当失败。"""
    cleaned = text.strip()
    if not cleaned:
        return TurnReply(
            title=title,
            text="这次没有得到可回复的内容，请稍后再试。",
            tone="warning",
        )
    return TurnReply(title=title, text=cleaned)
