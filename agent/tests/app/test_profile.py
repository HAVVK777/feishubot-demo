"""提示词文件要能从包内读出来。"""

from __future__ import annotations

from feishubot_agent.app.profile import AgentProfile
from feishubot_agent.settings import Settings


def test_profile_loads_the_prompt_from_the_package() -> None:
    profile = AgentProfile.load(Settings())

    assert profile.model == "deepseek-v4-pro"
    assert profile.max_steps == 8
    assert "飞书" in profile.system_prompt
