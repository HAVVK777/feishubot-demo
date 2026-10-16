# Feishu Bot Demo

一个飞书机器人：**接收事件订阅推送的消息，按关键字路由，把内容转发到你自己注册的外部
服务，把结果当卡片回给用户**。走 webhook，不使用飞书 SDK，协议自己实现。

## 快速开始

```bash
uv sync
uv run python scripts/demo_local.py   # 九节，跑通全链路，不需要凭据和网络
uv run pytest                          # 153 个测试
uv run ruff check . && uv run ruff format --check .
```

`demo_local.py` 用 `httpx.MockTransport` 把飞书和你的外部服务都假装掉，但**读的是仓库里
真实的 `routes.json`**——所以演示的就是你那份配置。加一条路由再跑一次，第四节就会多一个
服务。

## 目录结构与分层规则

```text
src/feishu_bot/
├── core/                  ← 不 import 飞书，也不 import web 框架
│   ├── domain.py          # IncomingMessage / Reply / Tone / MatchKind / ServiceRequest
│   ├── ports.py           # Protocol: Messenger / SeenStore / Service
│   ├── routing.py         # 关键字 → 服务 的路由表
│   └── handler.py         # 用例编排：去重 → 路由 → 分发 → 发送
├── adapters/
│   ├── feishu/            ← 唯一允许出现飞书协议细节的地方
│   │   ├── inbound.py     # 事件 → 命令（校验、解析、ACK）
│   │   └── outbound.py    # 回复 → 卡片 → HTTP 请求
│   ├── http_service.py    ← 对外接口的唯一定义处（请求/响应/失败）
│   ├── routes_file.py     # 读 routes.json
│   ├── help.py            # 内置的 /帮助
│   └── memory.py          # SeenStore 的内存实现
├── settings.py            # 只读环境变量
└── bootstrap.py           # 唯一的装配点
```

四条规则，都是有理由的：

1. **`core/` 不 import 飞书，也不 import FastAPI。** 所以 `tests/test_core.py` 和
   `tests/test_routing.py` 里没有一个"飞书"字样——核心逻辑脱离租户和凭据就能测。
2. **边界两侧各只有一个翻译点。** 飞书的事件体只在 `inbound.parse_message` 里被读一次，
   卡片 JSON 只在 `outbound.build_card` 里被构造一次，外呼协议只在 `http_service.py` 里
   定义一次。
3. **中立的表达 vs 飞书的样子。** 核心只说 `Tone.WARNING`，"橙色"是
   `outbound.CARD_TEMPLATE` 决定的；外部服务返回 `tone: "success"`，也是在这里翻。
4. **服务走统一的 `Service` Protocol。** 路由不认识具体实现，也不认识 HTTP——它只知道
   关键字。所以"这个关键字打到哪个地址"纯粹是 `bootstrap.py` 从配置里读出来的。

## 路由配置：`routes.json`

**加一个外部服务只需要改这个文件，不用碰 Python。**

```json
{
  "routes": [
    {
      "keyword": "搜索",
      "endpoint": "http://127.0.0.1:9001/search",
      "description": "在当前知识库里检索",
      "aliases": ["搜索", "查一下", "搜一下"],
      "headers": {"Authorization": "Bearer xxx"},
      "timeoutSeconds": 30
    }
  ]
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `keyword` | ✓ | 触发词。既是命令词（`/搜索 内容`），也是这条路由的身份 |
| `endpoint` | ✓ | 要 POST 到的地址，必须带 `http://` 或 `https://` |
| `description` | | 帮助卡片里的一句话 |
| `aliases` | | 额外的正文触发词。**留空表示只能通过 `/关键字 内容` 触发** |
| `headers` | | 调用这个服务时附带的请求头，用来放鉴权。**只能是 ASCII** |
| `timeoutSeconds` | | 单次外呼超时，默认 10 |

改完重启即可。字段名拼错（`endpont`）会在启动时直接报错——静默忽略的话表现是"这个服务
永远调不通"。

### 两种触发方式

| 用户说 | 打到哪 | `text` 是什么 |
|---|---|---|
| `/搜索 transformer` | `keyword` 匹配 | `transformer`（斜杠之后那段） |
| `帮我搜索一下 transformer` | `aliases` 匹配 | 整条消息（用户没划出内容边界，我们也不猜） |

**斜杠开头就只走命令匹配**：关键字不认识 → 回帮助卡片并提示"没有 `/xxx` 这个命令"，
不退化成正文匹配（`/搜索transformer` 是打错了，不该悄悄塞给搜索服务）。
斜杠和全角空格会被归一化，`／搜索　x` 同样命中。

多个关键字同时命中时按三级 tiebreak 选一个：`priority` → 最长别名 → 声明顺序。

### 外部服务的接口约定

**请求**（`Content-Type: application/json`）：

```json
{
  "keyword": "搜索",
  "text": "transformer",
  "messageId": "om_xxx",
  "chatId": "oc_xxx",
  "senderId": "ou_xxx",
  "matched": "slash",
  "trigger": "搜索"
}
```

**响应**：

```json
{"title": "检索结果", "text": "找到 3 篇相关论文…", "tone": "success"}
```

`text` 必填；`title` 缺省用关键字；`tone` 取 `info` / `success` / `warning`（配色
蓝/绿/橙），认不出来当 `info`。

**什么算失败**：非 2xx、响应不是 JSON、`text` 缺失——都会给用户回一张橙色的"服务暂时
不可用"卡片，错误进日志。想表达"没查到结果"**不要**用错误码，正常返回一条 `text` 就行，
语气自己定。

## 接真的飞书

### 1. 后台配置

| 步骤 | 位置 | 填什么 |
|---|---|---|
| 建应用 | 开发者后台 → 创建企业自建应用 | 拿到 App ID / App Secret |
| 开机器人 | 应用能力 → 添加应用能力 | 机器人 |
| 加权限 | 权限管理 | `im:message`、`im:message:send_as_bot` |
| 订阅事件 | 事件与回调 → 订阅方式 | 选「将事件发送至开发者服务器」，填请求地址 `https://<你的域名>/feishu/events` |
| 选事件 | 事件与回调 → 添加事件 | 「接收消息 v2.0」`im.message.receive_v1` |
| 加密策略 | 事件与回调 → 加密策略 | Verification Token 抄到 `.env`；**Encrypt Key 留空** |

> **群聊权限要收窄。** 只订阅「接收群聊中 @ 机器人消息」这类权限，**不要**申请
> 「接收群聊全部消息」。配了 `aliases` 的路由意味着任何含这些词的群消息都会触发一次外呼
> 并回一张卡片——拿到全部群消息权限的机器人在群里就是刷屏。代码里没有"是否 @ 了我"的
> 判断，这个风险靠权限侧兜住。

### 2. 启动

```bash
cp .env.example .env      # 填 App ID / App Secret / Verification Token
uv run python -m feishu_bot
```

默认监听 `127.0.0.1:8000`，回调地址 `/feishu/events`，路由表是工作目录下的 `routes.json`
（可用 `FEISHU_ROUTES_FILE` 换地方）。

**请求地址必须是公网可达的 IPv4 地址**，本机开发需要内网穿透（`ngrok http 8000` 之类）。

### 3. 本地验一下不通外网的那部分

```bash
curl -s localhost:8000/feishu/events \
  -H 'Content-Type: application/json' \
  -d '{"challenge":"abc123","type":"url_verification"}'
# → {"challenge":"abc123"}
```

## 协议上容易踩的坑

这几条都已在代码里处理，列出来是因为它们不是"写错代码"而是"不知道就会中招"：

- **URL 校验要 1 秒内返回，事件推送要 3 秒内返回。** 超时飞书会按
  15s → 5min → 1h → 6h 重发，共 4 次。所以事件是先 ACK、外呼和发卡片都在响应之后。
- **URL 校验的请求不带 `X-Lark-Signature` 头。** 在那条路径上验签会让"保存请求地址"
  这一步直接失败。
- **`content` 是字符串化的 JSON**，要再 `json.loads` 一次。事件体的 `content` 是这样，
  发送接口的 `content` 也是这样——后者传对象是最常见的发送失败原因。
- **去重按 `message_id`，不是 `event_id`。** 飞书 IM 文档明确要求前者，因为 `event_id`
  在某些情况下是空字符串。飞书是"至少发送一次"，重复投递是正常的。
- **飞书把业务错误也包在 HTTP 200 里**，错误码在 body 的 `code` 字段。只看状态码会把
  发送失败当成成功。
- **群聊正文里的 `@_user_1` 占位符**要按 `mentions[].key` 剥掉，而且要在拆分内容之前剥。
- **HTTP 头只能是 ASCII。** 在 token 旁边写一句中文备注是最容易踩的一种，所以在配置
  加载时就拦掉。

## 已知限制

| 限制 | 后果 | 要做的事 |
|---|---|---|
| 没实现加密事件解密 | 后台配了 Encrypt Key 会收到 501 | 加 `cryptography`，在 `inbound.py` 补解密分支 |
| 没实现 `X-Lark-Signature` 验签 | 来源校验只靠请求体里的 Verification Token，强度弱于签名 | 按 app_secret 做 HMAC 校验 |
| 去重是进程内的 | 多副本部署时各进程内存互不可见，去重失效 | `SeenStore` 换 Redis（`SET NX` + TTL） |
| ACK 后处理失败会丢事件 | 已 ACK 但崩溃，飞书不会重发 | 先落库/入队再 ACK |
| 外呼失败不会自动重试 | 去重标记在路由之前就落下了，用户重发同一条也只会被忽略 | 标记改成外呼成功后落（代价是可能重复回复），或引入队列 |
| 不支持不带内容的关键字 | `/状态` 这类命令也会被要求给内容 | `Rule` 加一个 `requires_argument=False`，配置里开放出来 |
| 用户输入进卡片有注入面 | `lark_md` 认 `<at id=...>`，正文里混进用户原文就能让人借机器人 @ 别人 | 出口统一把"像标签的 `<`"中和成全角（见 `outbound._neutralise_tags`） |
| 没有"是否 @ 了我"的判断 | 群聊刷屏风险靠权限侧兜住（见上） | 调 `/open-apis/bot/v3/info` 拿机器人 open_id 并缓存 |
| 帮助正文随路由增长 | 路由多到几十条后会逼近飞书卡片内容长度上限 | 到量了再做截断或分页 |
| `routes.json` 里有 token | 容易误提交 | 换成 `routes.local.json` 并设 `FEISHU_ROUTES_FILE` |
