"""启动入口：`python -m feishubot_mcp`。

真正的装配在 `server.py`；这里只解析参数、配日志、把它跑起来。命令行参数是给
"临时换个端口跑一下"用的，长期配置走环境变量（`settings.py`）。
"""

from __future__ import annotations

import argparse
import dataclasses
import logging

from feishubot_mcp import env_file
from feishubot_mcp.server import serve_http, serve_stdio
from feishubot_mcp.settings import Settings


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )

    # 入口显式加载 `.env`：已有的环境变量优先，文件只是兜底。
    env_file.load()

    args = _parse_args()
    settings = Settings.from_environment()

    if args.transport == "stdio":
        serve_stdio(settings)
        return

    if args.host or args.port:
        settings = dataclasses.replace(
            settings,
            host=args.host or settings.host,
            port=args.port or settings.port,
        )
    serve_http(settings)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="feishubot_mcp", description="MCP 服务")
    parser.add_argument(
        "--transport",
        choices=("streamable-http", "stdio"),
        default="streamable-http",
        help="默认 streamable-http（给远程客户端连）；stdio 是本地单客户端调试用的",
    )
    # 默认留空而不是填上 settings 的默认值：这样"命令行没给"和"命令行给了默认值"
    # 能分开，前者才让环境变量生效。
    parser.add_argument("--host", help="覆盖 MCP_HOST")
    parser.add_argument("--port", type=int, help="覆盖 MCP_PORT")
    return parser.parse_args()


if __name__ == "__main__":
    main()
