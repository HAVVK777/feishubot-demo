"""启动入口：`python -m feishubot_agent`。

真正的装配在 `bootstrap.py`；这里只负责把它跑起来。
"""

from __future__ import annotations

import logging

import uvicorn

from feishubot_agent.bootstrap import create_app
from feishubot_agent.settings import Settings


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )

    settings = Settings.from_environment()
    app = create_app(settings)

    logging.getLogger(__name__).info(
        "agent 监听 http://%s:%d（model=%s，mcp=%s）",
        settings.host,
        settings.port,
        settings.model,
        settings.mcp_url,
    )
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
