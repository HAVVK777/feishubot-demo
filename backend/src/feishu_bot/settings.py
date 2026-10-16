"""启动配置。

只读环境变量、只做"能不能启动"的判断（凭据缺失就拒绝启动），不做业务校验。
路由表的**内容**由 `adapters/routes_file.py` 校验，这里只管它在哪里。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

APP_ID_ENV = "FEISHU_APP_ID"
APP_SECRET_ENV = "FEISHU_APP_SECRET"
VERIFICATION_TOKEN_ENV = "FEISHU_VERIFICATION_TOKEN"
HOST_ENV = "FEISHU_BOT_HOST"
PORT_ENV = "FEISHU_BOT_PORT"
BASE_URL_ENV = "FEISHU_BASE_URL"
ROUTES_FILE_ENV = "FEISHU_ROUTES_FILE"

#: 飞书开放平台。测试和本地演示指向假服务，所以它是可配的。
DEFAULT_BASE_URL = "https://open.feishu.cn"

#: 相对路径按**进程的工作目录**解析，所以 `uv run python -m feishu_bot`
#: 要在 `backend/` 下执行。换个位置跑就设 `FEISHU_ROUTES_FILE`。
DEFAULT_ROUTES_FILE = Path("routes.json")


@dataclass(frozen=True, slots=True)
class Settings:
    app_id: str
    app_secret: str
    #: 留空则不校验回调来源。飞书后台「事件与回调 → 加密策略」里能看到这个值。
    verification_token: str = ""
    host: str = "127.0.0.1"
    port: int = 8000
    base_url: str = DEFAULT_BASE_URL
    #: 关键字 → 外部服务 的注册表。
    routes_file: Path = DEFAULT_ROUTES_FILE

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> Settings:
        values = os.environ if environ is None else environ

        missing = [name for name in (APP_ID_ENV, APP_SECRET_ENV) if not values.get(name)]
        if missing:
            raise RuntimeError(
                "缺少环境变量：" + "、".join(missing) + "（飞书后台「凭证与基础信息」里获取）"
            )

        return cls(
            app_id=values[APP_ID_ENV],
            app_secret=values[APP_SECRET_ENV],
            verification_token=values.get(VERIFICATION_TOKEN_ENV, ""),
            host=values.get(HOST_ENV, "127.0.0.1"),
            port=_port(values.get(PORT_ENV)),
            base_url=values.get(BASE_URL_ENV, DEFAULT_BASE_URL).rstrip("/"),
            routes_file=Path(values.get(ROUTES_FILE_ENV) or DEFAULT_ROUTES_FILE),
        )


def _port(raw: str | None) -> int:
    if not raw:
        return 8000
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{PORT_ENV} 必须是整数，得到 {raw!r}") from exc
