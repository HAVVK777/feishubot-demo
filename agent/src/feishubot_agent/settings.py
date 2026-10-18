"""启动配置。

只读环境变量、只做「能不能启动」的判断，不做业务校验。模型调用凭据
（`DEEPSEEK_API_KEY`）在 `bootstrap` 装配真实服务时检查；只跑 HTTP 层的测试
可以不带凭据。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

HOST_ENV = "AGENT_HOST"
PORT_ENV = "AGENT_PORT"
MCP_URL_ENV = "AGENT_MCP_URL"
MCP_TOKEN_ENV = "AGENT_MCP_TOKEN"
MCP_TIMEOUT_ENV = "AGENT_MCP_TIMEOUT_SECONDS"
DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"
DEEPSEEK_BASE_URL_ENV = "DEEPSEEK_BASE_URL"
MODEL_ENV = "AGENT_MODEL"
REASONING_EFFORT_ENV = "AGENT_REASONING_EFFORT"
MAX_STEPS_ENV = "AGENT_MAX_STEPS"
TURN_TIMEOUT_ENV = "AGENT_TURN_TIMEOUT_SECONDS"
SESSION_TTL_ENV = "AGENT_SESSION_IDLE_TTL_SECONDS"
SESSION_LOCK_WAIT_ENV = "AGENT_SESSION_LOCK_WAIT_SECONDS"
HISTORY_TURNS_ENV = "AGENT_HISTORY_MAX_TURNS"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9003
DEFAULT_MCP_URL = "http://127.0.0.1:8003/mcp"
DEFAULT_MCP_TIMEOUT_SECONDS = 35.0
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-pro"
DEFAULT_REASONING_EFFORT = "none"
REASONING_EFFORTS = frozenset({"none", "low", "high", "max"})
DEFAULT_MAX_STEPS = 8
DEFAULT_TURN_TIMEOUT_SECONDS = 100.0
DEFAULT_SESSION_IDLE_TTL_SECONDS = 1800.0
DEFAULT_SESSION_LOCK_WAIT_SECONDS = 10.0
DEFAULT_HISTORY_MAX_TURNS = 20


@dataclass(frozen=True, slots=True)
class Settings:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    mcp_url: str = DEFAULT_MCP_URL
    mcp_token: str = ""
    mcp_timeout_seconds: float = DEFAULT_MCP_TIMEOUT_SECONDS
    deepseek_api_key: str = ""
    deepseek_base_url: str = DEFAULT_DEEPSEEK_BASE_URL
    model: str = DEFAULT_MODEL
    reasoning_effort: str = DEFAULT_REASONING_EFFORT
    max_steps: int = DEFAULT_MAX_STEPS
    turn_timeout_seconds: float = DEFAULT_TURN_TIMEOUT_SECONDS
    session_idle_ttl_seconds: float = DEFAULT_SESSION_IDLE_TTL_SECONDS
    session_lock_wait_seconds: float = DEFAULT_SESSION_LOCK_WAIT_SECONDS
    history_max_turns: int = DEFAULT_HISTORY_MAX_TURNS

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> Settings:
        values = os.environ if environ is None else environ
        return cls(
            host=values.get(HOST_ENV) or DEFAULT_HOST,
            port=_positive_int(values.get(PORT_ENV), PORT_ENV, DEFAULT_PORT),
            mcp_url=values.get(MCP_URL_ENV) or DEFAULT_MCP_URL,
            mcp_token=values.get(MCP_TOKEN_ENV, ""),
            mcp_timeout_seconds=_positive_float(
                values.get(MCP_TIMEOUT_ENV), MCP_TIMEOUT_ENV, DEFAULT_MCP_TIMEOUT_SECONDS
            ),
            deepseek_api_key=values.get(DEEPSEEK_API_KEY_ENV, ""),
            deepseek_base_url=values.get(DEEPSEEK_BASE_URL_ENV) or DEFAULT_DEEPSEEK_BASE_URL,
            model=values.get(MODEL_ENV) or DEFAULT_MODEL,
            reasoning_effort=_reasoning_effort(values.get(REASONING_EFFORT_ENV)),
            max_steps=_positive_int(values.get(MAX_STEPS_ENV), MAX_STEPS_ENV, DEFAULT_MAX_STEPS),
            turn_timeout_seconds=_positive_float(
                values.get(TURN_TIMEOUT_ENV), TURN_TIMEOUT_ENV, DEFAULT_TURN_TIMEOUT_SECONDS
            ),
            session_idle_ttl_seconds=_positive_float(
                values.get(SESSION_TTL_ENV), SESSION_TTL_ENV, DEFAULT_SESSION_IDLE_TTL_SECONDS
            ),
            session_lock_wait_seconds=_positive_float(
                values.get(SESSION_LOCK_WAIT_ENV),
                SESSION_LOCK_WAIT_ENV,
                DEFAULT_SESSION_LOCK_WAIT_SECONDS,
            ),
            history_max_turns=_positive_int(
                values.get(HISTORY_TURNS_ENV), HISTORY_TURNS_ENV, DEFAULT_HISTORY_MAX_TURNS
            ),
        )


def _positive_int(raw: str | None, name: str, default: int) -> int:
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是整数，得到 {raw!r}") from exc
    if value <= 0:
        raise RuntimeError(f"{name} 必须大于 0，得到 {value}")
    return value


def _reasoning_effort(raw: str | None) -> str:
    value = (raw or DEFAULT_REASONING_EFFORT).strip().lower()
    if value not in REASONING_EFFORTS:
        raise RuntimeError(
            f"{REASONING_EFFORT_ENV} 必须是 "
            + " / ".join(sorted(REASONING_EFFORTS))
            + f"，得到 {raw!r}"
        )
    return value


def _positive_float(raw: str | None, name: str, default: float) -> float:
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是数字，得到 {raw!r}") from exc
    if value <= 0:
        raise RuntimeError(f"{name} 必须大于 0，得到 {value}")
    return value
