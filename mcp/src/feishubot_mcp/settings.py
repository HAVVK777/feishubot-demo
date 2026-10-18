"""启动配置。

只读环境变量，只放**服务本身**的开关——监听在哪、怎么鉴权。将来某个工具自己的
配置（某个 API 的 key 之类）由那个工具模块自己读：放进这里的话，每加一个工具都得
回来改这个文件，"加工具"就不再是"只写一个文件"了。

启动时入口会先调 `env_file.load()` 把当前目录的 `.env` 读进进程环境（见 `env_file.py`）。
**已存在的环境变量优先**——容器 / systemd 注入的覆盖文件。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

HOST_ENV = "MCP_HOST"
PORT_ENV = "MCP_PORT"
PATH_ENV = "MCP_PATH"
AUTH_TOKEN_ENV = "MCP_AUTH_TOKEN"
AUTH_TOKENS_ENV = "MCP_AUTH_TOKENS"
ALLOWED_HOSTS_ENV = "MCP_ALLOWED_HOSTS"
ALLOWED_ORIGINS_ENV = "MCP_ALLOWED_ORIGINS"
TOOL_TIMEOUT_ENV = "MCP_TOOL_TIMEOUT_SECONDS"
RATE_LIMIT_ENV = "MCP_RATE_LIMIT_PER_MINUTE"
SESSION_RATE_LIMIT_ENV = "MCP_SESSION_RATE_LIMIT_PER_MINUTE"
GROUP_RATE_LIMIT_ENV = "MCP_GROUP_RATE_LIMIT_PER_MINUTE"

#: 只设了 `MCP_AUTH_TOKEN`（没写名字）时给这个 token 用的名字。日志里会显示成
#: "default"，提示它是个没归因的旧式配置。
DEFAULT_TOKEN_NAME = "default"

#: 默认端口刻意避开 8000：同一台机器上飞书机器人监听在那里，撞端口第二个服务起不来。
DEFAULT_PORT = 8765

#: 协议端点。客户端连的是 `http://<host>:<port>/mcp`。
DEFAULT_PATH = "/mcp"

#: 单次工具调用的上限。一个卡住的上游不该把请求一直挂着——调用方那边会先超时断开，
#: 而服务端还以为自己在干活。0 表示不限（那就真的会一直挂着）。
DEFAULT_TOOL_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class Settings:
    """一次启动的全部开关。"""

    #: 默认只听本机。容器里必须改成 `0.0.0.0`，否则发布了端口也没用。
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    path: str = DEFAULT_PATH
    #: 调用方 token 表：`(名字, token)`。**留空则不校验来源**，知道地址的人都能连。
    #: 名字不参与校验，只用来记日志、以及在工具里归因（`auth.caller_name()`）。
    #: 允许重名——轮换时同一个 agent 的旧 token 和 token 名会短暂并存。
    auth_tokens: tuple[tuple[str, str], ...] = ()
    #: 反代到公网后要放行的 Host 头（`mcp.example.com`，或 `mcp.example.com:*` 放行该
    #: 主机的任意端口）。留空 = 不开 DNS rebinding 防护，理由见
    #: `server._transport_security`。
    allowed_hosts: tuple[str, ...] = ()
    #: 浏览器里的客户端会带 Origin 头，需要单独放行；命令行客户端不带，不受影响。
    allowed_origins: tuple[str, ...] = ()
    #: 单次工具调用的超时（秒）。见 `middleware.ToolTimeout` 里"超时不是中止"那条说明。
    tool_timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS
    #: 每个调用方每分钟最多多少次工具调用。**0 = 不限**，因为默认值不该把谁挡在门外；
    #: 线上建议设一个，防某个 agent 跑飞打满服务。
    #: 这条链路上 MCP 通常只看到一个调用方（agent），所以它实际上就是**整个服务的总闸**。
    #: 数值按"能接受的峰值"定（并发会话数 × 单会话额度），不是按机器容量的上限定——
    #: 容量可以加机器升级，而真正的硬约束在上游（飞书 API 的额度）。
    rate_limit_per_minute: int = 0
    #: 每个**会话**每分钟最多多少次工具调用（会话来自 `_meta.session_id`，调用方自称）。
    #: 单聊用它；群聊在没配下面那个时也用它。0 = 不限。
    session_rate_limit_per_minute: int = 0
    #: **群聊**每个会话每分钟最多多少次（依据 `_meta.chat_type == "group"`）。
    #: 群聊的额度是全群共享的——一个人能把额度用光、全群等着，所以它值得有自己的数。
    #: **不配就跟随上面那个**，不会因为少配一个变量就把群聊放成不限。0 = 不限。
    group_rate_limit_per_minute: int = 0

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> Settings:
        values = os.environ if environ is None else environ
        return cls(
            host=values.get(HOST_ENV, "127.0.0.1"),
            port=_port(values.get(PORT_ENV)),
            path=_path(values.get(PATH_ENV)),
            auth_tokens=_tokens(values),
            allowed_hosts=_csv(values.get(ALLOWED_HOSTS_ENV)),
            allowed_origins=_csv(values.get(ALLOWED_ORIGINS_ENV)),
            tool_timeout_seconds=_float(values.get(TOOL_TIMEOUT_ENV), DEFAULT_TOOL_TIMEOUT_SECONDS),
            rate_limit_per_minute=int(_float(values.get(RATE_LIMIT_ENV), 0)),
            session_rate_limit_per_minute=int(_float(values.get(SESSION_RATE_LIMIT_ENV), 0)),
            group_rate_limit_per_minute=int(_float(values.get(GROUP_RATE_LIMIT_ENV), 0)),
        )


def _port(raw: str | None) -> int:
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{PORT_ENV} 必须是整数，得到 {raw!r}") from exc


def _float(raw: str | None, default: float) -> float:
    """数值型开关。负数一律当非法——它们没有意义，而且负的超时会让判断反过来。"""
    if not raw or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{raw!r} 不是数字（环境变量：超时用秒、限流用次数）") from exc
    if value < 0:
        raise RuntimeError(f"{raw!r} 是负数。0 表示不限，要关就直接写 0")
    return value


def _path(raw: str | None) -> str:
    """归一化成 `/xxx`。

    客户端拼地址时多一个斜杠或少一个斜杠是"连不上"里最常见的一种，而报出来的只是
    一个 404，看不出是路径没归一化。在这里统一之后，配置里写 `mcp`、`/mcp/`、`/mcp`
    都得到同一个端点；首尾空格（从 `.env` 或 `docker run -e` 里抄过来常带）也一并去掉。
    """
    if not raw or not raw.strip():
        return DEFAULT_PATH
    return "/" + raw.strip().strip("/")


def _tokens(values: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    """两个环境变量都读，但**不允许同时设**。

    同时设的话两个都会生效，而这几乎总是"迁移到一半忘了删旧的"——表现是那个旧的
    开发 token 悄悄还在放行。宁可启动就报错，让它当场被看见。
    """
    named = values.get(AUTH_TOKENS_ENV, "").strip()
    single = values.get(AUTH_TOKEN_ENV, "").strip()

    if named and single:
        raise RuntimeError(
            f"{AUTH_TOKENS_ENV} 和 {AUTH_TOKEN_ENV} 同时设了。把那个单独的并进"
            f"{AUTH_TOKENS_ENV}（写成 `名字:token`）再删掉它——只留一个，避免"
            "一个已经不该用的 token 还在悄悄放行。"
        )

    if single:
        return ((DEFAULT_TOKEN_NAME, single),)

    return tuple(_token_entry(entry) for entry in named.split(",") if entry.strip())


def _token_entry(entry: str) -> tuple[str, str]:
    """`名字:token` → `(名字, token)`。

    只按**第一个**冒号切：token 本身可以含冒号（`secrets.token_urlsafe` 不会产生
    冒号，但别人手写的 base64 里可能有）。
    """
    name, separator, token = entry.strip().partition(":")
    if not separator or not name.strip() or not token.strip():
        raise RuntimeError(
            f"{AUTH_TOKENS_ENV} 的每一段都要写成 `名字:token`（逗号分隔），"
            f"这一段是 {entry.strip()!r}"
        )
    return name.strip(), token.strip()


def _csv(raw: str | None) -> tuple[str, ...]:
    """逗号分隔的列表，跳过空项——方便写成 `a.com, b.com,` 而不留下空字符串。"""
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())
