"""装配：把工具装到 server 上，并决定它怎么被访问。

和 `backend/bootstrap.py` 同一条规则：**不建模块级 `server`**。导入即建实例会让
测试和工具一 import 就炸，也没法在同一个进程里起两个配置不同的实例。要实例就调
`build_server()`。

另一条也照搬：**配置问题在启动时就说清楚**。工具重名、路径没归一化这类毛病留到
第一次调用才暴露的话，现象是"这个工具调不通"，离原因很远。
"""

from __future__ import annotations

import logging

import uvicorn
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from feishubot_mcp import __version__
from feishubot_mcp.auth import BearerTokenMiddleware
from feishubot_mcp.middleware import build_middleware
from feishubot_mcp.settings import AUTH_TOKENS_ENV, Settings
from feishubot_mcp.tools import register_all

logger = logging.getLogger(__name__)

SERVER_NAME = "feishubot-mcp"

#: 模型在调用任何工具之前**唯一**能看到的说明。它是写给模型的，不是写给维护者的，
#: 所以别在这里写"记得更新我"。加完一批工具之后回来把它改成实际的能力概览：
#: 单个工具的 docstring 只讲得清自己，讲不了"这个服务整体上能干什么、
#: 该在什么场景下先想到它"。
INSTRUCTIONS = "一个提供通用工具的服务。可用工具见 tools/list。"

#: 探活端点，给容器编排和反代用，不在 MCP 协议里。约定见 `_add_health_route`。
HEALTH_PATH = "/healthz"


def build_server(settings: Settings | None = None) -> MCPServer:
    """建一个装好全部工具和中间件的 server。

    只碰协议层（有哪些工具、怎么描述、横切关注点），不碰传输层（监听在哪、怎么鉴权）
    ——后者在 `build_http_app`。分开是为了让测试能直接拿 server 去做进程内调用，
    不用起 HTTP。

    `settings` 只影响中间件（超时、限流）；不传就用默认值，测试和工具演示不需要配。
    """
    resolved = settings if settings is not None else Settings()
    server = MCPServer(
        name=SERVER_NAME,
        version=__version__,
        instructions=INSTRUCTIONS,
        # 中间件和传输无关（HTTP、stdio 都过这一层），所以挂在这里而不是 HTTP 应用上。
        middleware=build_middleware(resolved),
    )
    _add_health_route(server)
    register_all(server)

    logger.info(
        "工具超时 %s",
        f"{resolved.tool_timeout_seconds:g} 秒"
        if resolved.tool_timeout_seconds > 0
        else "关（卡住的工具会把请求一直挂着）",
    )
    # 每一维分别打出来：只报其中一个的话，别的开着也会显示成"不限"。
    logger.info(
        "限流：每调用方 %s；单聊每会话 %s；群聊每会话 %s",
        _quota(resolved.rate_limit_per_minute),
        _quota(resolved.session_rate_limit_per_minute),
        # 群聊额度不配就跟随单聊——日志要显示实际生效的数，不然看日志的人以为群聊不限。
        _quota(resolved.group_rate_limit_per_minute or resolved.session_rate_limit_per_minute),
    )
    return server


def _quota(per_minute: int) -> str:
    """限流额度的一行说法，只用于启动日志。"""
    return f"{per_minute} 次/分钟" if per_minute > 0 else "不限"


def build_http_app(server: MCPServer, settings: Settings) -> Starlette:
    """把 server 包成可以部署的 HTTP 应用：协议端点 + 探活 + 鉴权。"""
    app = server.streamable_http_app(
        streamable_http_path=settings.path,
        transport_security=_transport_security(settings),
        # 成功的响应返回纯 JSON，不套 SSE 的 event/data 外壳。
        #
        # 代价是关掉了"调用过程中回报进度/日志"的能力——那些消息本来是搭在 POST 那条
        # SSE 流上的（见 README「服务端消息到底走哪条流」）。现在的工具都是调完给结果，
        # 用不到；**哪天要写跑很久、想让调用方看到进度的工具，把这行删掉**即可，客户端
        # 不用改（两种响应格式 SDK 都认）。
        #
        # 注意它只改 POST：GET 那条常驻流仍然是 SSE，协议里那条通道删不掉。
        json_response=True,
    )

    if not settings.auth_tokens:
        logger.warning("未配置 %s：任何人都能连上来调工具（仅供本地演示）。", AUTH_TOKENS_ENV)
        return app

    # 只记名字，**不记 token 的值**——启动日志经常被整段收集走。
    logger.info(
        "已加载 %d 个调用方 token：%s",
        len(settings.auth_tokens),
        "、".join(name for name, _ in settings.auth_tokens),
    )
    # 顺序要紧：`AuthContextMiddleware` 要在内层，它读 scope 时调用方才刚被写上
    # （见 `auth.BearerTokenMiddleware.__call__`）。反过来工具里拿到的永远是 None。
    return BearerTokenMiddleware(
        AuthContextMiddleware(app),
        tokens=settings.auth_tokens,
        exempt_paths=[HEALTH_PATH],
    )


def _transport_security(settings: Settings) -> TransportSecuritySettings | None:
    """DNS rebinding 防护：放行哪些 Host 头。

    SDK 只在监听 `127.0.0.1`／`localhost` 时**自动打开**这项防护；绑 `0.0.0.0`
    （容器里必须）时默认是**关的**。挂到域名后面之后 Host 头是域名，这时要在这里
    显式放行——没配就保持 SDK 的默认行为，由 `MCP_ALLOWED_HOSTS` 决定。

    症状对照，配错了只报一个状态码，不看这里会查很久：
    Host 没放行 → `421 Invalid Host header`；Origin 没放行 → `403 Invalid Origin header`。
    """
    if not settings.allowed_hosts:
        return None
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(settings.allowed_hosts),
        allowed_origins=list(settings.allowed_origins),
    )


def _add_health_route(server: MCPServer) -> None:
    """探活端点。

    不带鉴权（见 `build_http_app` 的豁免名单）：探活的调用方通常没有 token，
    需要鉴权的探活等于没有探活。它只回版本号，不暴露任何工具信息。
    """

    @server.custom_route(HEALTH_PATH, methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "server": SERVER_NAME, "version": __version__})


def serve_http(settings: Settings) -> None:
    """起 HTTP 服务（`python -m feishubot_mcp` 的默认行为）。"""
    app = build_http_app(build_server(settings), settings)
    logger.info(
        "MCP 端点：http://%s:%d%s（探活 %s）",
        settings.host,
        settings.port,
        settings.path,
        HEALTH_PATH,
    )
    uvicorn.run(app, host=settings.host, port=settings.port)


def serve_stdio(settings: Settings) -> None:
    """走标准输入输出。本地调试用，一次只能服务一个客户端。

    stdio 没有网络层，也就没有鉴权可言——token 在这个模式下没有意义（限流同理，
    只有一个客户端；审计和超时照常生效）。
    """
    build_server(settings).run("stdio")
