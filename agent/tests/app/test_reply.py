"""空回复必须变成明确的兜底文案，不能原样交给 backend。"""

from __future__ import annotations

from feishubot_agent.app.reply import to_reply


def test_empty_text_becomes_a_warning_reply() -> None:
    reply = to_reply("   ")

    assert reply.tone == "warning"
    assert reply.text


def test_normal_text_is_passed_through() -> None:
    reply = to_reply("  还剩 5 天。 ")

    assert reply.text == "还剩 5 天。"
    assert reply.tone == "info"
