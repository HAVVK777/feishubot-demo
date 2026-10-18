"""会话标签：从 `_meta` 取出来、放进 contextvar、用完清掉。

它和 `caller_name()` 的差别是**谁说的**：调用方名字来自已验证的 token，会话标签是
调用方自己在 `_meta` 里写的字符串。所以这里的重点是"不是非空字符串就当没带"。
"""

from __future__ import annotations

from typing import Any

import pytest

from feishubot_mcp.session import (
    CHAT_TYPE_META_KEY,
    SESSION_META_KEY,
    SessionContext,
    chat_type,
    extract,
    extract_chat_type,
    session_id,
)


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        ({"session_id": "chat-abc"}, "chat-abc"),
        ({"session_id": "  chat-abc  "}, "chat-abc"),  # 从 HTTP/JSON 里抄过来常带空格
        ({"session_id": "chat-abc", "其他键": 1}, "chat-abc"),  # _meta 是开放 map
        ({"session_id": ""}, None),
        ({"session_id": "   "}, None),
        ({"session_id": 123}, None),  # 类型不对，不能直接拿来做桶的键
        ({"session_id": None}, None),
        ({"别的键": "x"}, None),
        ({}, None),
        (None, None),
    ],
)
def test_extract_only_accepts_a_non_empty_string(meta: dict[str, Any] | None, expected: str | None):
    assert extract(meta, SESSION_META_KEY) == expected


def test_no_labels_by_default():
    assert session_id() is None
    assert chat_type() is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("group", "group"),
        ("p2p", "p2p"),
        ("  group  ", "group"),
        ("GROUP", None),  # 大小写不一样也算认不出来
        ("群聊", None),
        ("single", None),
        ("", None),
        (123, None),
    ],
)
def test_chat_type_only_accepts_feishu_vocabulary(value: Any, expected: str | None):
    """认不出来一律当没带——宽松地"猜一个"会让拼错变成静默的行为差异。"""
    assert extract_chat_type({CHAT_TYPE_META_KEY: value}) == expected


async def test_session_context_sets_the_label_for_the_call():
    seen: list[str | None] = []

    async def call_next(_ctx: Any) -> str:
        seen.append(session_id())
        return "ok"

    middleware = SessionContext()
    await middleware(_FakeCtx(meta={"session_id": "chat-abc"}), call_next)

    assert seen == ["chat-abc"]


async def test_session_context_clears_it_afterwards():
    """用完要清掉：contextvar 是任务级的，残留会让下一次调用顶着上一个会话的身份。"""

    async def call_next(_ctx: Any) -> str:
        return "ok"

    middleware = SessionContext()
    await middleware(_FakeCtx(meta={"session_id": "chat-abc"}), call_next)

    assert session_id() is None


class _FakeCtx:
    def __init__(self, meta: dict[str, Any] | None) -> None:
        self.meta = meta
