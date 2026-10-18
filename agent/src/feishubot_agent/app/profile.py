"""这个 agent 是谁：模型、工具白名单、预算、提示词。

所有「业务常量」都收在这里。`server.py` 和 `harness/` 都不应该出现模型 id、
工具名列表或回复标题。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from feishubot_agent.settings import Settings

PROMPT_PATH = Path(__file__).parent / "prompts" / "system.md"


@dataclass(frozen=True, slots=True)
class AgentProfile:
    model: str
    max_steps: int
    system_prompt: str
    #: None = 放行全部工具；空元组 = 一个都不放行。
    allowed_tools: tuple[str, ...] | None = None
    reply_title: str = "助手"

    @classmethod
    def load(cls, settings: Settings) -> AgentProfile:
        try:
            prompt = PROMPT_PATH.read_text(encoding="utf-8").strip()
        except FileNotFoundError as exc:
            raise RuntimeError(f"系统提示词不存在：{PROMPT_PATH}") from exc
        if not prompt:
            raise RuntimeError(f"系统提示词是空的：{PROMPT_PATH}")

        return cls(
            model=settings.model,
            max_steps=settings.max_steps,
            system_prompt=prompt,
        )
