"""启动入口：`python -m feishu_bot`。

真正的装配在 `bootstrap.py`；这里只负责把它跑起来。
"""

from __future__ import annotations

import logging

import uvicorn

from feishu_bot.bootstrap import create_app
from feishu_bot.settings import Settings


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )

    settings = Settings.from_environment()
    app = create_app(settings)

    logging.getLogger(__name__).info(
        "回调地址：http://%s:%d%s", settings.host, settings.port, "/feishu/events"
    )
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
