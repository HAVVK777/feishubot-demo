"""配置解析：默认值、覆盖、以及配错时拒绝启动。"""

from __future__ import annotations

import pytest

from feishubot_agent.settings import Settings


def test_defaults() -> None:
    settings = Settings.from_environment({})

    assert settings.host == "127.0.0.1"
    assert settings.port == 9003
    assert settings.model == "deepseek-v4-pro"
    assert settings.reasoning_effort == "none"
    assert settings.deepseek_base_url == "https://api.deepseek.com"
    assert settings.deepseek_api_key == ""
    assert settings.max_steps == 8
    assert settings.turn_timeout_seconds == 100.0
    assert settings.mcp_timeout_seconds == 35.0
    assert settings.session_lock_wait_seconds == 10.0
    assert settings.history_max_turns == 20


def test_environment_overrides() -> None:
    settings = Settings.from_environment(
        {
            "AGENT_HOST": "0.0.0.0",
            "AGENT_PORT": "9100",
            "AGENT_MODEL": "deepseek-v4-pro",
            "AGENT_REASONING_EFFORT": "none",
            "DEEPSEEK_API_KEY": "sk-test",
            "DEEPSEEK_BASE_URL": "https://example.invalid/v1",
            "AGENT_MAX_STEPS": "3",
            "AGENT_TURN_TIMEOUT_SECONDS": "12.5",
        }
    )

    assert settings.host == "0.0.0.0"
    assert settings.port == 9100
    assert settings.model == "deepseek-v4-pro"
    assert settings.reasoning_effort == "none"
    assert settings.deepseek_api_key == "sk-test"
    assert settings.deepseek_base_url == "https://example.invalid/v1"
    assert settings.max_steps == 3
    assert settings.turn_timeout_seconds == 12.5


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AGENT_PORT", "not-a-number"),
        ("AGENT_MAX_STEPS", "0"),
        ("AGENT_TURN_TIMEOUT_SECONDS", "-1"),
        ("AGENT_MCP_TIMEOUT_SECONDS", "0"),
        ("AGENT_SESSION_LOCK_WAIT_SECONDS", "0"),
        ("AGENT_HISTORY_MAX_TURNS", "0"),
        ("AGENT_REASONING_EFFORT", "turbo"),
    ],
)
def test_invalid_values_fail_at_startup(name: str, value: str) -> None:
    with pytest.raises(RuntimeError, match=name):
        Settings.from_environment({name: value})
