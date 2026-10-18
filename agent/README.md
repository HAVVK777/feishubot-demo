# Feishu Bot Agent

飞书机器人的 agent 服务。它接在机器人和 MCP 工具服务之间：

```text
飞书用户 → backend（飞书协议、卡片）→ agent（模型 + 工具循环）→ MCP 服务（工具）
```

backend 仍然是唯一接触飞书协议的地方；agent 只接收 HTTP 请求、返回卡片需要的字段。
agent 不知道自己被谁调用，也不直接给飞书发消息。

## 当前状态

回合循环、DeepSeek 适配层、MCP 客户端和回合服务已实现，`POST /turn` 可以完整
跑通。启动时会连 MCP 列工具；MCP 不可用时以**空工具列表**启动，日志里留明确的
错误，不会因此阻止进程起来。模型是必需依赖：`DEEPSEEK_API_KEY` 没配时启动会
直接拒绝，而不是等到第一条消息才失败。

## 快速开始

```bash
uv sync
cp .env.example .env          # 填 DEEPSEEK_API_KEY 和 AGENT_MCP_TOKEN
uv run --env-file .env python -m feishubot_agent

# 另一个终端
curl http://127.0.0.1:9003/healthz
```

测试与检查：

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Docker Compose 部署（宿主端口 8002）见 [DEPLOY.md](DEPLOY.md)。

## 对外契约

backend 的 `adapters/http_service.py` 已经定义了外部服务的请求/响应形状，agent 直接
沿用它。**请求**（camelCase 是 boundary 上的既有字段名）：

```json
{
  "keyword": "助手",
  "text": "我今年还剩几天年假？",
  "messageId": "om_xxx",
  "chatId": "oc_xxx",
  "senderId": "ou_xxx",
  "chatType": "group",
  "matched": "slash",
  "trigger": "助手"
}
```

`chatType` 是 MCP 会话限流需要的标签（`p2p` / `group`）。backend 目前的外呼 payload
还没有这个字段；如果 MCP 侧配置了群聊额度，需要在
`backend/src/feishu_bot/adapters/http_service.py` 的 payload 里补一行
`"chatType": request.message.chat_type`。不补也能跑，只是 agent 调用工具时不带这个
标签，MCP 在需要区分群聊/单聊额度时会拒绝。

**响应**：

```json
{
  "title": "助手",
  "text": "你今年还剩 5 天年假。",
  "tone": "info"
}
```

`text` 必须非空；`tone` 取 `info` / `success` / `warning`。backend 负责把这三个字段
翻译成飞书卡片，agent 不构造卡片 JSON。

接入时在 backend 的 `routes.json` 加一条路由，并把 `timeoutSeconds` 配到大于 agent
的 `AGENT_TURN_TIMEOUT_SECONDS`：

```json
{
  "keyword": "助手",
  "endpoint": "http://127.0.0.1:9003/turn",
  "description": "向 agent 提问",
  "timeoutSeconds": 120
}
```

## 目录结构

```text
src/feishubot_agent/
├── __main__.py             # python -m feishubot_agent
├── settings.py             # 环境变量 → Settings，启动时校验
├── bootstrap.py            # 唯一装配点
├── server.py               # FastAPI 路由，只做 HTTP 翻译
│
├── harness/                # 通用 agent 运行时：不出现飞书概念
│   ├── loop.py             # 模型 ↔ 工具循环
│   ├── model.py            # ModelClient 协议 + 中立消息类型
│   ├── providers/
│   │   └── deepseek.py     # 唯一 import OpenAI SDK 的地方
│   ├── mcp_client.py       # MCP 连接、调用、错误归一
│   ├── tools.py            # 工具目录：schema、白名单、hints 渲染
│   ├── session.py          # 历史 + 每会话 Lock + 空闲清理
│   ├── context.py          # 历史裁剪与工具结果截断
│   ├── policy.py           # 步数、重试、超时
│   └── errors.py
│
└── app/                    # agent 主体：决定「是谁、怎么答」
    ├── contracts.py        # HTTP 请求/响应模型（camelCase 别名）
    ├── service.py          # 请求 → 会话 → harness → 回复
    ├── profile.py          # 模型、工具白名单、预算、提示词
    ├── prompts/system.md
    └── reply.py            # 最终文本 → {title, text, tone}
```

四条边界规则：

1. **依赖只能 `app → harness`。** `harness` 不 import `app`、`server`、`bootstrap`；
   `tests/architecture/` 会把这条规则钉住。
2. **模型 SDK 只出现在 `harness/providers/deepseek.py`。** 其余代码只依赖
   `model.py` 里的 `ModelClient` 协议，换模型时只加一个 provider。
3. **MCP 只在 `harness/mcp_client.py` 里出现。** `tools.py` 和 `loop.py` 看到的是
   `ToolSpec` / `ToolCall` / `ToolResult` 这些中立类型。
4. **装配只有一个入口。** 不在模块级创建 app、客户端或工具单例；`bootstrap.py`
   负责把实现接起来，测试可以注入假的模型和工具。

## 配置

完整清单见 `.env.example`。应用本身不读 `.env`，由启动命令或部署环境注入。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AGENT_HOST` | `127.0.0.1` | 监听地址 |
| `AGENT_PORT` | `9003` | 监听端口 |
| `AGENT_MCP_URL` | `http://127.0.0.1:8765/mcp` | MCP 流式 HTTP 端点 |
| `AGENT_MCP_TOKEN` | 空 | MCP 调用方 token |
| `AGENT_MCP_TIMEOUT_SECONDS` | `35` | 单次工具调用的客户端超时 |
| `DEEPSEEK_API_KEY` | 空（必填） | DeepSeek API key |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容端点 |
| `AGENT_MODEL` | `deepseek-v4-pro` | 模型 id（`deepseek-flash` 更快但本机实测输出不稳） |
| `AGENT_REASONING_EFFORT` | `none` | `none` / `low` / `high` / `max`；复杂任务再调 `high` |
| `AGENT_MAX_STEPS` | `8` | 单个回合最多几轮工具调用 |
| `AGENT_TURN_TIMEOUT_SECONDS` | `100` | 单个回合的总预算 |
| `AGENT_SESSION_IDLE_TTL_SECONDS` | `1800` | 会话空闲回收时间 |
| `AGENT_SESSION_LOCK_WAIT_SECONDS` | `10` | 同会话并发时等待锁的上限 |
| `AGENT_HISTORY_MAX_TURNS` | `20` | 保留最近多少个用户回合 |

`DEEPSEEK_API_KEY` 在 `bootstrap.py` 装配真实服务时检查；`DEEPSEEK_BASE_URL`
默认是 DeepSeek 官方 OpenAI 兼容端点，换代理/网关时改它即可。

MCP 工具列表在**启动时拉取一次**并缓存。MCP 新增工具，或者启动时 MCP 不可用
（agent 会以空工具列表运行），都需要重启 agent 才会重新拉取。

MCP 的工具结果会在 `_meta.execution_time_ms` 里返回服务端耗时。agent 只把它写进
日志（`客户端 X ms，服务端 Y ms`），**不会**放进给模型的工具结果正文。

## 会话与并发

同一个 `chatId` 的消息在 agent 内**串行**执行：`harness/session.py` 给每个会话一把
`asyncio.Lock`。这不是性能优化，而是语义要求——群聊里两条消息并发处理会让历史互相
污染。锁等待超过 `AGENT_SESSION_LOCK_WAIT_SECONDS` 时返回一条明确的「上一条还在
处理」，不要无限排队。

历史目前只在进程内存里。重启会丢；多副本部署时同一会话会落到不同实例。要持久化时
在 `harness/session.py` 后面加一个 `SessionStore` 协议实现，不要把存储细节漏进 loop。

## 重试与工具调用预算

- 自动重试只针对 `rate_limited` 和连接失败：最多 2 次尝试，指数退避封顶 4 秒，
  并受回合剩余时间约束。
- 每次真正执行工具前都要向 `ToolExecutionBudget` 申请额度：
  - 相同 `(工具名, 参数)` 一个回合最多执行 **2 次**（含自动重试）；
  - 整个回合最多执行 **16 次**工具（含自动重试）。
- 达到上限后不再执行，把拒绝原因作为 `is_error=True` 的工具结果交给模型。即使模型
  无视提示继续调用，也只会拿到拒绝结果，不会产生真实调用。
- `timeout` 和工具语义错误（`is_error=True`）不自动重试，由模型结合工具 hints
  决定；自动重试耗尽时，错误正文会明确写「不要再调用」。

## 已知限制

- **没有工具级权限。** `sender_id` 只作为上下文传给模型，模型仍可以把用户给的
  其他 `open_id` 传给工具。真正的授权必须在工具内部做，不能依赖提示词。
- **工具列表启动时拉取一次**，新增工具或 MCP 启动时不可用都需要重启 agent。
- **没有流式输出。** backend 的契约是一次性响应，所以一个回合结束才返回。
- **没有持久化会话**，多副本部署需要 sticky 或者换 `SessionStore` 实现。
