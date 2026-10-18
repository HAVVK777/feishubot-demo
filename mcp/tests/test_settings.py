"""配置解析。重点在"配错了要报出来"，而不是"配对了能读出来"。"""

from __future__ import annotations

import pytest

from feishubot_mcp.settings import DEFAULT_PORT, Settings


def test_defaults_are_safe():
    settings = Settings.from_environment({})

    # 默认只听本机、默认没有 token，两个都是"先能跑起来"的选择；
    # 要暴露出去必须显式改，理由写在 README 的部署一节。
    assert settings.host == "127.0.0.1"
    assert settings.auth_tokens == ()
    assert settings.port == DEFAULT_PORT
    assert settings.path == "/mcp"


@pytest.mark.parametrize("raw", ["mcp", "/mcp", "/mcp/", "  /mcp  "])
def test_path_is_normalised(raw: str):
    assert Settings.from_environment({"MCP_PATH": raw}).path == "/mcp"


def test_bad_port_fails_loudly():
    with pytest.raises(RuntimeError, match="MCP_PORT 必须是整数"):
        Settings.from_environment({"MCP_PORT": "八千"})


def test_lists_are_split_and_trimmed():
    settings = Settings.from_environment(
        {"MCP_ALLOWED_HOSTS": " mcp.example.com , mcp.example.com:* ,"}
    )

    assert settings.allowed_hosts == ("mcp.example.com", "mcp.example.com:*")
    # 没配的那项是空元组，不是 None——下游直接 list() 就行。
    assert settings.allowed_origins == ()


def test_named_tokens_are_parsed():
    settings = Settings.from_environment({"MCP_AUTH_TOKENS": " agent-a:aaa , agent-b:bbb ,"})

    assert settings.auth_tokens == (("agent-a", "aaa"), ("agent-b", "bbb"))


def test_single_token_still_works_and_gets_a_default_name():
    """旧式配置不能一升级就失效——它只是没有名字。"""
    settings = Settings.from_environment({"MCP_AUTH_TOKEN": "solo"})

    assert settings.auth_tokens == (("default", "solo"),)


def test_token_may_contain_colons():
    """只按第一个冒号切，token 里的冒号是它自己的一部分。"""
    settings = Settings.from_environment({"MCP_AUTH_TOKENS": "agent:aa:bb"})

    assert settings.auth_tokens == (("agent", "aa:bb"),)


def test_both_env_vars_at_once_is_refused():
    """两个都设 = 那个旧的会悄悄继续放行，属于必须当场可见的配置错误。"""
    with pytest.raises(RuntimeError, match="同时设了"):
        Settings.from_environment({"MCP_AUTH_TOKEN": "old", "MCP_AUTH_TOKENS": "a:new"})


def test_token_entry_without_a_name_is_refused():
    with pytest.raises(RuntimeError, match="名字:token"):
        Settings.from_environment({"MCP_AUTH_TOKENS": "just-a-token"})


def test_limits_default_to_on_for_timeout_and_off_for_rate():
    """超时有默认值（卡住的工具默认就该被放弃）；限流默认关（默认值不该把谁挡在门外）。"""
    settings = Settings.from_environment({})

    assert settings.tool_timeout_seconds == 30.0
    assert settings.rate_limit_per_minute == 0


def test_limits_are_read_from_the_environment():
    settings = Settings.from_environment(
        {"MCP_TOOL_TIMEOUT_SECONDS": "5.5", "MCP_RATE_LIMIT_PER_MINUTE": "60"}
    )

    assert settings.tool_timeout_seconds == 5.5
    assert settings.rate_limit_per_minute == 60


@pytest.mark.parametrize("value", ["六十", "1m", "一小时"])
def test_bad_limit_fails_loudly(value: str):
    with pytest.raises(RuntimeError, match="不是数字"):
        Settings.from_environment({"MCP_RATE_LIMIT_PER_MINUTE": value})


def test_negative_limit_is_refused():
    """负数没有意义，而且负的超时会让判断反过来（永远立即超时）。"""
    with pytest.raises(RuntimeError, match="是负数"):
        Settings.from_environment({"MCP_TOOL_TIMEOUT_SECONDS": "-1"})
