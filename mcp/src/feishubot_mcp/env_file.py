"""显式加载 `.env`。

依赖里仍然没有 dotenv——那会把"配置从哪来"变成两个来源。但本地开发和单机部署时，
每次都写 `uv run --env-file .env` 或在 shell 里 `source` 太啰嗦，所以由**入口显式调用**
这个模块，规则只有四条：

* **已经在环境里的变量不覆盖。** 容器 / systemd 注入的优先级更高，`.env` 只是兜底。
* **文件不存在是正常的**（部署环境通常没有），返回 0，不报错。
* **格式写错直接报错**，带行号——静默跳过会让人以为"变量没生效"，而真正的原因在别处。
* 支持 `KEY=VALUE`、`export KEY=VALUE`、`# 注释`、值的单双引号。

路径由 `MCP_ENV_FILE` 指定，缺省是**当前工作目录**下的 `.env`（和 `routes.json` 一样，
相对路径按进程工作目录解析）。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

ENV_FILE_ENV = "MCP_ENV_FILE"
DEFAULT_ENV_FILE = Path(".env")

logger = logging.getLogger(__name__)


def load(path: Path | None = None) -> int:
    """把 `.env` 里的变量塞进 `os.environ`，返回实际新设了几个。

    已存在的变量**不覆盖**——这是有意为之：部署时容器环境优先于文件。
    """
    resolved = path if path is not None else _default_path()
    if not resolved.is_file():
        logger.debug("没有 %s，跳过（用进程环境）", resolved)
        return 0

    loaded = 0
    for lineno, raw in enumerate(resolved.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()

        key, separator, value = line.partition("=")
        if not separator or not key.strip():
            raise RuntimeError(f"{resolved} 第 {lineno} 行不是 KEY=VALUE 形式：{raw!r}")

        key = key.strip()
        if key not in os.environ:
            os.environ[key] = _value(value)
            loaded += 1

    logger.info("从 %s 加载了 %d 个环境变量（已存在的未覆盖）", resolved, loaded)
    return loaded


def _default_path() -> Path:
    return Path(os.environ.get(ENV_FILE_ENV, "").strip() or DEFAULT_ENV_FILE)


def _value(raw: str) -> str:
    """去引号；未加引号时把 ` #` 之后当注释。

    `#` 前面必须有空白才算注释——否则 token 里合法的 `#` 会被吃掉。
    """
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    comment = value.find(" #")
    return value[:comment].rstrip() if comment != -1 else value
