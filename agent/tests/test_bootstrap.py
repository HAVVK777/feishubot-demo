"""真实装配的启动条件：没有模型凭据就明确失败。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from feishubot_agent.bootstrap import create_app
from feishubot_agent.settings import Settings


def test_startup_fails_without_a_deepseek_key() -> None:
    app = create_app(Settings(deepseek_api_key=""), build_service=True)

    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"), TestClient(app):
        pass
