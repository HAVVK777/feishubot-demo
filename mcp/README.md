# MCP 服务

一个独立的 MCP 服务：**工具是普通的 `async` 函数，写在一个清单里；服务把它们按
MCP 协议暴露出去，客户端连上来调**。传输层是 Streamable HTTP，可以远程部署。

已有第一个真实工具 `get_leave_days`（查飞书请假天数），另有三个示例/自检工具。加工具
的地方见[「加一个工具」](#加一个工具)。

## 现有工具

| 工具 | 说明 | 需要的配置 |
|---|---|---|
| `get_leave_days` | 查某人在某段时间内的请假天数（读多维表格） | `FEISHU_APP_ID` / `FEISHU_APP_SECRET` + 表格地址 |
| `whoami` | 自检：报告调用方是谁 | — |
| `ping` / `add` | 示例，写真实工具时可删 | — |

`get_leave_days` 读的是一张**由飞书审批同步过来的**多维表格，用到的列：
用户（人员）/ 开始时间 / 时长 / 审批结果。

- **只统计审批结果为「通过」的行**——待审批、已撤销等一律跳过。
- **按开始日期整条归属**：一条 6/28–7/2 的 5 天假，只有查询区间包含 6/28 时才计入，
  查 7 月时不计入。审批同步表里没有"每月各几天"，按日历摊分在工作日口径下会算错，
  所以用一个可解释的口径，而不是猜。
- **只接受 open_id**（`ou_` 开头），并且**用户列必须是人员字段**：按 id 精确匹配，
  重名的人不会串。传姓名/工号会被拒绝；用户列是文本列也会被拒绝——文本列只能按姓名
  匹配，重名会**静默算错**，宁可不给结果。
- 回复里的姓名直接从人员列取，**不需要通讯录权限**。

## 加一个工具

**第一步**，在 `tools/example.py` 里（或者新建一个模块）写函数。工具就是普通函数，
类型注解生成入参 schema，docstring 就是给模型看的描述：

```python
from feishubot_mcp.tools.hints import hints


@hints(read_only=True)
async def search_notes(query: str, limit: int = 10) -> str:
    """在笔记库里检索。用户问"我记过什么关于 X 的东西"时用这个。"""
    return f"没找到 {query} 相关的笔记"
```

**第二步**，加进这个模块的 `TOOLS`：

```python
TOOLS = (ping, add, search_notes)
```

**第三步**，如果是新建的模块，在 `tools/__init__.py` 的 `MODULES` 里加一行：

```python
MODULES = (example, notes)
```

函数名就是工具名。**重名会在启动时直接报错**——SDK 默认只打一条 warning 然后让后者
盖掉前者，那样被盖住的工具调起来会打到另一个实现上，很难查。

写函数的四条规矩：

- docstring 写**"什么时候该用它"**，不是"这个函数做了什么"。模型是照着这句话选工具的；
  几条描述都长得一样时它只能猜。
- 参数注解要具体（`str` / `int` / `Literal[...]`）。写成 `Any` 或 `dict`，模型就只能
  瞎猜参数了。
- 用 `@hints(...)` 声明**能不能安全地重复调用**（见下）。
- 别在 `async def` 里做阻塞的事（见[「多个 agent 同时调工具」](#多个-agent-同时调工具)）。

### 声明工具的性质：`@hints(...)`

```python
@hints(read_only=True)                # 只读：查东西、算东西，不改任何状态
@hints(idempotent=True)               # 有副作用，但重复调用不会多产生一遍
@hints(destructive=True)              # 破坏性：删除、覆盖、不可逆
```

**这是调用方判断"能不能重试"的唯一依据**，而且会出现在 `tools/list` 里——**模型自己
也看得到**，它据此决定要不要重试、要不要先跟你确认。

- **不声明 = 什么都不说**，调用方只能按"可能有副作用"对待（安全的那一侧）。启动日志里
  会写成"未声明"，提醒你补。
- **把有副作用的工具标成 `read_only` 是这里唯一会出事的错法**：调用方会放心重试，
  于是同一条消息发两遍。
- `read_only` 为真时另外几个没意义；`idempotent` / `destructive` 只在有副作用时才有意义。
- 语义就是 MCP 的 `ToolAnnotations`，别的 MCP 客户端认的是同一套。

> 顺带说清"重试"这件事：**框架不做自动重试**，因为它对调用方不可见——一个已经超时的
> 调用在后台又执行一遍，是最容易出事故的形态。重试该由调用方按 `@hints` 决定。

工具自己需要的配置（某个 API 的 key 之类）由那个工具模块自己读环境变量，不要加到
`settings.py`：加进去之后每加一个工具都得回来改这个文件。

工具里想知道是谁在调，用 `from feishubot_mcp.auth import caller_name`（见
[「token 怎么维护」](#token-怎么维护)）——它是按 token 表反查出来的，不是请求头里
客户端自称的东西。

## 报错：分清"预料到的"和"没预料到的"

| 情况 | 怎么写 | 客户端会看到 |
|---|---|---|
| 查不到、参数不对、上游限流 | `raise ToolError("人话")` | 你写的那句话。模型能据此改参数重试 |
| 空指针、类型错、连不上数据库 | 别接，让它抛 | 只有 `Error executing tool <名字>` |

**异常文本不会漏给客户端**——第二类里的细节（token、SQL、内部主机名）只进服务端
日志，堆栈在 ERROR 级别。`tests/test_server.py` 里有两条测试把这个约定钉住了。

所以 `raise ToolError(...)` 里那句话是**写给模型看的**：说清楚哪里不对、可以怎么办，
不要贴堆栈，也不要把内部 ID 抄进去。

```python
from feishubot_mcp.errors import ToolError


async def fetch_report(report_id: str) -> str:
    """按 ID 取报告正文。"""
    row = await db.get(report_id)
    if row is None:
        raise ToolError(f"没有 {report_id} 这份报告。先用 list_reports 查可用的 ID。")
    return row["body"]
```

## 多个 agent 同时调工具

**并发是默认发生的，服务端不排队。** 实测（工具里睡 0.5 秒，同时发 5 次）：

| 场景 | 总耗时 | 逐个跑会是 |
|---|---|---|
| `async` 工具，5 次调用 / 同一个会话 | 0.54s | 2.50s |
| `async` 工具，5 次调用 / 5 个不同会话 | 0.53s | 2.50s |
| 同步工具（普通 `def`），5 次调用 / 同一个会话 | 0.51s | 2.50s |
| 在 `async def` 里做**阻塞**调用，3 次 | **1.51s** | 1.50s |

前三行是"5 次真的同时在跑"，最后一行退化成了逐个跑。`tests/test_concurrency.py`
用"集合点"（所有调用者到齐了才一起返回）把这几条钉住了，不依赖机器快慢。

### 写工具的三条规矩

**1. 阻塞的事别写在 `async def` 里。** 所有工具、所有会话共享同一个事件循环，一句
`requests.get()` 或 `time.sleep()` 会卡住**所有人**，不只是发这个请求的那个 agent——
上表最后一行就是这么来的。要调阻塞的东西，把工具写成普通的 `def`，SDK 会自动
`anyio.to_thread.run_sync` 丢到工作线程（默认最多 40 个并发），在那里面阻塞不影响别人。

**2. 工具之间不要用模块级变量共享状态。** 每个 `await` 都是一个切换点。实测一个
读-改-写计数器并发调 10 次，**10 次全返回 `1`**——丢了 9 次更新：

```python
_counter = 0


async def bump() -> int:
    """这个写法是错的。"""
    global _counter
    seen = _counter
    await something()  # ← 切换点：别的调用从这里插进来
    _counter = seen + 1
```

要么别共享（每次调用自己算自己的），要么加 `asyncio.Lock`，要么把状态放到外面
（数据库、Redis，让它们自己管并发）。

**3. 外部资源不会因为你的服务是异步的就变宽。** 数据库连接池、上游接口的速率限制
都是按"同时多少个请求"算的，而那个数字现在等于正在用的 agent 数量。工具自己要有
超时和限流——`MCP_AUTH_TOKENS` 挡的是"谁能连"，不是"能调多快"。

### 服务端的并发上限

| 项 | 默认 | 在哪改 |
|---|---|---|
| 同时存在的会话数 | 10000 | `server.build_http_app` 里的 `streamable_http_app(max_sessions=...)` |
| 会话空闲多久回收 | 1800 秒 | 同上，`session_idle_timeout=` |
| 同步工具的线程数 | 40 | anyio 的 limiter，超了的排队等 |
| 工具本身的超时 | 没有 | 工具自己设（`asyncio.timeout` / 客户端的 timeout） |

## 审计、限流、超时

三件事都挂在 SDK 的 `ServerMiddleware` 上（[middleware.py](src/feishubot_mcp/middleware.py)），
**工具一个都不用改**。顺序是外层在前：

```text
AuditLog → RateLimit → ToolTimeout → 工具
```

`AuditLog` 放最外层是有意的：被限流挡掉、被超时放弃的调用也要留下记录，否则日志里会
出现一段"什么都没发生"的空白，而那正是最该看的时候。

### 审计日志

每次 `tools/call` 留一行：

```text
调用完成：feishu-agent[oc_xxx/p2p] → search（0.31s，参数：limit、query）
调用出错：feishu-agent[oc_xyz/group] → fetch（1.02s，参数：id）
调用失败：feishu-agent[无会话/无类型] → slow（0.50s，参数：seconds）：工具 slow 超过 0.5 秒没有返回…
```

`调用出错` 和 `调用失败` 是两回事：前者是工具用 `ToolError` 报的（那是个**正常返回值**，
`isError=true`），后者是抛出来的异常（限流、超时、崩溃都算）。只看有没有抛异常的话，
第一种会被记成成功。

两个刻意的取舍：

- **只记参数名，不记参数值**。值是调用方的数据——飞书那边就是用户发的原文。日志通常比
  数据库活得久、管得松，原样记等于把用户内容复制一份过去。
- **只记 `tools/call`**。`initialize`、`tools/list` 每次建会话都会来一遍（参考实现是每次
  调用新开一个会话），全记会把真正的调用淹掉。

返回给调用方的 `CallToolResult._meta` 里还带 `execution_time_ms`（服务端耗时，毫秒）。
它在 `_meta` 里、不在正文里，模型看不到；调用方可以读它做日志和指标。换算函数是
`timing.execution_time_meta`。

### 超时

`MCP_TOOL_TIMEOUT_SECONDS`（默认 30 秒）。超时后调用方收到一句明确的错误，而不是一直
挂着：

```text
工具 slow 超过 0.5 秒没有返回，已放弃等待（它可能仍在执行，重试前请确认这个工具是否幂等）
```

> ⚠️ **超时只是"不再等它"，不是"中止它"。** 同步工具跑在工作线程里，Python 杀不掉线程，
> 它会继续跑完、副作用照常发生。所以这个值要按"最慢的正常调用"来定，别当成中断手段。

工具自己抛的 `TimeoutError`（比如它调的上游超时了）**不会被误认成工具超时**，原样传上去
——两个都是 `TimeoutError` 类型，实现里查的是"超时窗口有没有真的到期"。

### 限流：两个维度，管的是两件事

| 维度 | 环境变量 | 防的是什么 |
|---|---|---|
| **每个会话** | `MCP_SESSION_RATE_LIMIT_PER_MINUTE`<br>`MCP_GROUP_RATE_LIMIT_PER_MINUTE` | **一个会话把容量打满** |
| 每个调用方 | `MCP_RATE_LIMIT_PER_MINUTE` | 多个会话加起来超过服务容量 |

**会话那层按会话类型分两套额度。** 群聊和单聊在数据上都是一个"会话"，但群聊的额度是
**全群共享**的——一个人能把额度用光、全群等着——所以群聊该有自己的数。依据是调用方自称的
`_meta.chat_type`（取值跟飞书一致）：

| `chat_type` | 用哪个额度 |
|---|---|
| `group` | `MCP_GROUP_RATE_LIMIT_PER_MINUTE` |
| `p2p` | `MCP_SESSION_RATE_LIMIT_PER_MINUTE` |
| `group`，但群聊额度**没配** | **跟随单聊额度**（不会因为少配一个变量就把群聊放成不限） |

两个维度**互相独立**，可以只开一个。这条链路上 MCP 通常只看到一个调用方（agent），所以
"每调用方"那层实际上就是**整个服务的总闸**。注意它的数值是一条**标准线**，不是机器容量
的上限——MCP 的容量可以靠加机器升级；真正的硬约束在上游（飞书 API 的额度），见
[DESIGN.md §4.7](DESIGN.md#47-上游额度飞书-api现在的取舍与将来的做法)。

两个都是令牌桶：容量是一分钟的量、按秒连续补。所以"攒满一分钟的额度一次打出去"是允许
的，之后只能按补充速度来——**正常突发不误伤，跑飞的循环会被压到限速上**。只算
`tools/call`，列工具和握手不吃额度。

超了之后调用方收到：

```text
这个会话调用过于频繁：oc_xxx 每分钟最多 20 次
这个群聊调用过于频繁：oc_xxx 每分钟最多 50 次
调用过于频繁：feishu-agent 每分钟最多 600 次
```

### 按会话限流：调用方要带什么

每个 `tools/call` 的 `_meta` 里带两个标签：

```json
{"method": "tools/call",
 "params": {"name": "search", "arguments": {},
            "_meta": {"session_id": "oc_xxx", "chat_type": "group"}}}
```

`_meta` 是协议里的开放字段（保留键都以 `io.modelcontextprotocol/` 开头），塞自定义键是
规范允许的。参考实现里就是：

```python
await toolbox.call("search", {"query": "x"}, session_id="oc_xxx", chat_type="group")
```

四条规矩：

1. **同一个飞书会话的多次调用必须用同一个 `session_id`。** 每次请求现生成一个的话，
   按会话限流等于没限——每个桶都是满的。
2. **`chat_type` 只认 `p2p` 和 `group`**，别的取值（拼错、大小写不一样、用别的词）
   一律当没带。
3. **配了额度却没带标签 → 直接拒绝**（`-32602`，消息里会列出合法取值）。跳过才是有问题
   的：那样一个不带标签的跑飞循环完全不受这一层管，而那正是它要防的东西。
4. **它们是自称的，不是身份。** 服务端没有任何办法验证这些值——适合做归因和公平性，
   **不能**做权限判断（"这个会话能不能调这个工具"）。工具里要用的话：
   `from feishubot_mcp.session import session_id, chat_type`（返回 `None` 就是没带）。

> 别和 MCP 传输层的 `Mcp-Session-Id` 搞混：那个是连接级的、由传输层自己管，和"一次
> 对话"没有关系。

### 这两个错误码

| 码 | 含义 |
|---|---|
| `-32000` | 被限流 |
| `-32001` | 工具超时 |
| `-32602` | 配了会话额度，但调用没带（或带得认不出）`_meta.session_id` / `_meta.chat_type` |

JSON-RPC 把 `-32000..-32099` 留给实现，MCP 没有这两个的标准码，所以只能自己定——客户端
认不出码就只看 message。

### 为什么框架不做自动重试

**重试的前提是"我知道这次没成功"。** 按失败形态分，结论正好相反：

| 失败形态 | 这次到底做了没有 | 怎么办 |
|---|---|---|
| 连不上（TCP/DNS 失败，请求没发出去） | **确定没做** | 可以自动重试——连接层那部分 HTTP 库已经做了 |
| 上游明确拒绝（4xx 等） | **确定没做** | 可以，但要限次 + 退避 |
| **超时** | **不知道** | **谁都不该自动重试** |

超时是最不该盲目重试的一类，而且**框架在你放弃等待之后并不会中止工具**：同步工具跑在
工作线程里会继续跑完，异步工具停在下一个 `await` 点（已经在飞的写入不会因此收回）。
调用方立刻重试，很可能两个实例同时在跑。所以超时的错误消息里明说了"它可能仍在执行，
重试前请确认这个工具是否幂等"。

重试该由调用方按 [`@hints(...)`](#声明工具的性质hints) 决定——只读和幂等的可以，其余
的不行（不行的那类，正确的动作是**报告"状态不确定"让人去确认**，而不是再发一次）。

## 客户端怎么连

调用方（agent）用 MCP 客户端 SDK 主动连上来。参考实现是
[`scripts/agent_client.py`](scripts/agent_client.py)——**可以直接跑，也可以整段抄走**：

```bash
MCP_TOKEN=<token 表里冒号后面那串> uv run python scripts/agent_client.py
# 工具列表：ping、add、whoami
# whoami → feishu-agent
```

它封装了五件事，都是调用方一定会踩的：

1. **带鉴权头**。mcp 自带的 HTTP 客户端是它 fork 的 `httpx2`（**不是 httpx**），要带
   自定义头就得自己建一个传给它——`Client("http://.../mcp")` 那个简写没法带 header。
2. **每次调用新开会话**（连接 → initialize → 调用 → 关闭）。机器人这种"来一条处理
   一条"的节奏，本地握手是毫秒级，换来的是没有需要维护的长连接状态——服务端重启不会
   让机器人卡在僵死的会话上。要长连接的话再把它养在进程里。
3. **超时**。工具本身不设超时（见[服务端的并发上限](#服务端的并发上限)），调用方必须设。
4. **错误分两类**：`McpToolError`（工具**有意**报的，消息可以直接回给用户）和
   `McpConnectionError`（连不上 / token 不对 / 被限流 / 超时 / 对面挂了，回一句"服务暂时
   不可用"，原文进日志）。客户端 SDK 会把后者埋在 `ExceptionGroup` 里，不剥开的话调用方
   只会看到 `unhandled errors in a TaskGroup`。
5. **把失败分类交出来**：`McpConnectionError.data["kind"]` 是 `unauthorized` /
   `rate_limited` / `timeout` / `missing_meta` 之一。**重试决策读 `data`，不要解析消息
   ——** 那条消息是写给人看的，措辞随时会改。什么该重试、什么该问用户，见
   [DESIGN.md §11.1](DESIGN.md#111-框架不做自动重试但把判断依据交出去)。

`scripts/smoke_http.py` 直接调用这个类（不另写一份），所以那份参考实现每次冒烟都会被
跑一遍，不会悄悄腐烂。

```bash
uv run python scripts/smoke_http.py    # 自己起服务、自己收尾
```

没带 token 或带错的请求会被回 **401**，响应里带 `WWW-Authenticate: Bearer`。消息体写成
了 JSON-RPC 错误对象，所以客户端抛出来的是能读的那句话（`未授权：缺少或错误的 Bearer
token（…）`），而不是一句笼统的 `Server returned an error response`。

## 接口行为（实测）

端点只有一个（`MCP_PATH`，默认 `/mcp`），认三种方法。下面都是实测结果，不是照文档抄的。

### 先记一条：响应格式分两种

| 情形 | 状态码 | Content-Type |
|---|---|---|
| **请求成功** | 200 | **`application/json`**（早期是 `text/event-stream`，见下） |
| 通知类请求 | 202 | `application/json`，body 空 |
| 任何错误（400 / 401 / 404 / 405 / 406） | 对应状态码 | `application/json` + JSON-RPC 错误体 |
| `/healthz`（自建路由） | 200 | `application/json` |

**成功的响应也是纯 JSON**——`server.build_http_app` 里显式开了 `json_response=True`，
理由和代价见「[为什么响应是纯 JSON](#为什么响应是纯-json)」。手写 curl 可以直接
`JSON.parse`，不用去剥 SSE 的 `data:` 那一行。

（唯一的例外是 Content-Type 校验失败那条 400，Starlette 回的是裸 `Response`，连
Content-Type 头都没有。）

### POST /mcp —— 发消息

| 请求 | 结果 |
|---|---|
| `initialize`（还没有会话） | **200** + JSON 响应体，**响应头带 `mcp-session-id`**，之后每次请求都要带上它 |
| 通知（`notifications/initialized` 之类，带会话） | **202** Accepted，空 body |
| `tools/call`（带会话） | **200** + JSON，只有结果（调用途中的消息到不了客户端，见下） |
| 任何请求（不带会话，`initialize` 除外） | **400** `Missing session ID` |
| Content-Type 不是 `application/json` | **400** `Invalid Content-Type header` |
| body 不是合法 JSON | **400** `Parse error`（`-32700`） |
| 未知方法 | **200** + JSON-RPC 错误 `-32601` |
| 调不存在的工具 | **200** + `isError` 结果，文本 `Unknown tool: xxx` |
| 别的 HTTP 方法（PUT 之类） | **405** |

> ⚠️ 最后两行要一起看：HTTP 状态码只表达**协议/传输/鉴权**层的失败，
> **业务错误一律包在 200 里**（靠 JSON-RPC 的 `error` 或结果的 `isError` 判断）。只看
> 状态码会把"方法名拼错了"当成成功——和飞书把业务错误包在 HTTP 200 里是同一类坑。

### GET /mcp —— 挂着收服务端消息

| 请求 | 结果 |
|---|---|
| 带会话 + `Accept: text/event-stream` | **200**、`content-type: text/event-stream`，**连接一直开着不返回** |
| 带会话，完全不带 `Accept` 头 | **200**，同样开流（这一条不强制带） |
| 带会话 + `Accept: application/json` | **406** `Not Acceptable: Client must accept text/event-stream` |
| 不带会话 | **400** `Missing session ID` |

这是一条**长连接**，反代配错的表现是"客户端随机断线"或"连上了但一直没反应"：

```nginx
location /mcp {
    proxy_pass http://127.0.0.1:8765;
    proxy_http_version 1.1;
    proxy_set_header Host $host;      # 别忘了进 MCP_ALLOWED_HOSTS
    proxy_buffering off;              # 不关的话 SSE 被攒在缓冲区，客户端一直等不到
    proxy_read_timeout 1h;            # 默认 60s 会把这条长连接掐断
}
```

### DELETE /mcp —— 结束会话

带 `mcp-session-id` 请求即终止该会话（**200**）；之后再用同一个 id 会得到
**404** `Session not found`。客户端正常退出时会自己发。

### 客户端实际会开哪些连接

实测：**正常的请求-响应用法只发 POST**。参考实现连了 5 次会话、做了 3 轮
`list_tools` + `tools/call`，服务端一共收到 40 个 `POST /mcp`，**`GET` 和 `DELETE`
各 0 次**。也就是说 GET 那条常驻流平时根本不会被打开，结果就在 POST 的响应里。

所以反代的 SSE 配置（`proxy_buffering off` 等）**不是当前的关键路径**——但还是要配：
别的客户端会开这条流，将来要用服务端推送时也得靠它。

### 为什么响应是纯 JSON

`server.build_http_app` 里显式传了 `json_response=True`，POST 的响应体因此是纯 JSON，
不套 `event: message / data: {...}` 那层 SSE 外壳。

**代价是"调用过程中回报进度/日志"这条路被彻底关掉了**——实测：工具里 `ctx.info(...)`
照常执行、调用照常成功，但**那条消息到不了客户端**（POST 响应是纯 JSON 塞不下，
GET 那条流上也是 0 行）。不是"推送变慢"，是"推送消失"。

这是有意的取舍：现在的工具都是"调完给结果"，用不到过程推送，换来的是响应可 curl、
可 grep，反代也不用管 SSE 缓冲。

**哪天要写跑很久、想让调用方看到进度的工具，把 `json_response=True` 那行删掉即可**，
客户端不用改（两种响应格式 SDK 都认）。

> 两个别搞混的点：
>
> - **废弃的是旧的 "HTTP+SSE" 传输**（`/sse` + `/message` 两个端点、必须靠一条常驻
>   连接通信），不是 SSE 这个格式。Streamable HTTP 自己就用 `text/event-stream`
>   做流式响应，默认模式下的 POST 就是。
> - 这个开关**只改 POST**。GET 那条常驻流仍然是 SSE——协议里那条通道删不掉，
>   只是客户端不会去开。

### GET 能不能用 body 拿工具列表

不能。实测 GET 带 `{"method":"tools/list"}` 的 body → 200、照旧开流、**body 被完全忽略**，
工具列表不会回来。

`/mcp` 的 GET 不是 REST 语义的"取资源"，它是 MCP 会话的一条下行通道，规范里唯一的用途
就是开这条流。列工具是 POST `{"jsonrpc":"2.0","id":1,"method":"tools/list"}`。
（何况 HTTP 语义上 GET 带 body 本来就不该依赖，很多中间设施会直接丢掉。）

### 不带 token 时

以上每个方法都先过鉴权：没带对 token 一律 **401**，不区分方法。

## 配置

启动时入口会**自己加载工作目录下的 `.env`**（`env_file.py`，没有引入 dotenv 依赖）：

```bash
cp .env.example .env                    # 然后填凭据和 token（.env 已在 .gitignore 里）
uv run python -m feishubot_mcp
```

**已存在的环境变量优先**——容器 / systemd 注入的值不会被 `.env` 覆盖。路径可以用
`MCP_ENV_FILE` 改。格式写错（有内容但没有 `=`）会直接报错并给出行号。

| 变量 | 默认 | 说明 |
|---|---|---|
| `MCP_HOST` | `127.0.0.1` | 监听地址。容器里必须改 `0.0.0.0`，否则发布了端口也没用 |
| `MCP_PORT` | `8765` | 刻意避开机器人的 `8000` |
| `MCP_PATH` | `/mcp` | 协议端点路径。写 `mcp`、`/mcp/` 都会被归一化成 `/mcp` |
| `MCP_AUTH_TOKENS` | 空 | 调用方 token 表：`名字:token`，逗号分隔。**留空则不校验来源**，只适合本地演示 |
| `MCP_AUTH_TOKEN` | 空 | 旧式的单个 token（没有名字）。**不能和上面那个同时设**，同时设会拒绝启动 |
| `MCP_ALLOWED_HOSTS` | 空 | 反代后放行哪些 Host 头，逗号分隔。见下 |
| `MCP_ALLOWED_ORIGINS` | 空 | 浏览器里的客户端会带 Origin 头才需要配；命令行客户端不用 |
| `MCP_TOOL_TIMEOUT_SECONDS` | `30` | 单次工具调用的上限。`0` = 不限（那就真的会一直挂着） |
| `MCP_RATE_LIMIT_PER_MINUTE` | `0` | 每个调用方每分钟最多多少次**工具调用**。`0` = 不限 |
| `MCP_SESSION_RATE_LIMIT_PER_MINUTE` | `0` | **单聊**每个会话每分钟最多多少次工具调用（群聊没配下面那个时也用它）。`0` = 不限 |
| `MCP_GROUP_RATE_LIMIT_PER_MINUTE` | `0` | **群聊**每个会话每分钟最多多少次。不配就跟随上面那个。`0` = 不限 |

临时改端口可以直接给命令行参数：`uv run python -m feishubot_mcp --port 9000`。

### token 怎么维护

一个调用方一行，名字只是标签（用来记日志和归因，不参与校验）：

```bash
MCP_AUTH_TOKENS="feishu-agent:1Jq...Xk,ops-agent:9Rb...3v"
```

生成一个：

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

**轮换零停机**，四步，任何时刻都有可用的 token：

```bash
# 1. 加上新的，旧的一起留着（重名允许）
MCP_AUTH_TOKENS="feishu-agent:新token,feishu-agent:旧token,ops-agent:..."
# 2. 重启服务：新旧都有效，已连接的 agent 不受影响
# 3. 把 feishu-agent 换成新 token（可以一个一个来）
# 4. 去掉旧的那行，再重启
```

**吊销**就是删掉那一行再重启——只影响那一个调用方，其余不受牵连。这是它比单个共享
密钥强的地方。

**存放**：不进仓库（`.gitignore` 里有 `.env`），本地 `chmod 600 .env`，线上由 compose
`env_file` / systemd `EnvironmentFile` 或密钥管理服务注入。应用只读 `os.environ`，
启动时会加载 `.env`（已存在的环境变量优先）。启动日志只打名字不打值。

> 环境变量不是保险箱：同机同用户能从 `/proc/<pid>/environ` 读到。这是"别把这台机器
> 给不相关的人用"的问题，不是配置问题——要更强的隔离，用 systemd 的 `LoadCredential`
> 或容器 secret。

**工具里想知道是谁在调**（归因、按调用方区分行为）：

```python
from feishubot_mcp.auth import caller_name


async def search(query: str) -> str:
    """检索。"""
    logger.info("%s 查了 %r", caller_name(), query)
```

拿到的名字来自**已验证的 token**，不是请求头里客户端自称的东西。stdio 和进程内直连没有
这层中间件，返回 `None`——工具得处理它。示例工具 `whoami` 就是干这个的：

```bash
# 连上之后调 whoami，返回的就是你这行配置里的名字
```

### 挂到域名后面的两个坑

**1. Host 头。** SDK 只在监听 `127.0.0.1`／`localhost` 时自动打开 DNS rebinding 防护；
绑 `0.0.0.0`（容器里必须）时默认是**关的**。挂到域名后面要显式放行：

```bash
MCP_ALLOWED_HOSTS=mcp.example.com
# 端口不固定就写 mcp.example.com:*，只写主机名匹配不上带端口的 Host
```

配错的症状是请求被回 **421 `Invalid Host header`**（Origin 不对是 **403**），不看这里
会查很久。

**2. TLS。** 服务本身只讲 HTTP，HTTPS 在反代那层终结。MCP 客户端对 `https://` 的
证书校验是默认开的，别用自签证书糊过去。

### 部署形态

```bash
MCP_HOST=0.0.0.0 \
MCP_AUTH_TOKENS="feishu-agent:<刚生成的>" \
MCP_ALLOWED_HOSTS=mcp.example.com \
uv run python -m feishubot_mcp
```

容器、systemd、compose 都还没做——需要的时候再加，不影响现在的代码。

### stdio 模式

```bash
uv run python -m feishubot_mcp --transport stdio
```

本地单客户端调试用（比如临时挂到桌面客户端上）。**这个模式没有鉴权**——stdio 没有
网络层，token 在这里没有意义，能起这个进程的人本来就能读它的管道。

## 测试

```bash
uv run pytest                              # 25 个，进程内直连，<1 秒
uv run python scripts/smoke_http.py        # 端到端，起真进程
```

两层是分开的：`pytest` 用 `Client(build_server())` 走**内存流**，跑的是完整协议往返
（initialize → tools/list → tools/call）但不碰网络，所以又快又稳；`smoke_http.py` 才
覆盖 uvicorn 的 lifespan、鉴权中间件、Host 头校验这些传输层的东西。加工具时前一层
够用，动鉴权/传输时才需要跑后一层。

## 排错

| 现象 | 原因 |
|---|---|
| 客户端抛 `未授权：缺少或错误的 Bearer token` | 客户端没带 `Authorization: Bearer <token>`，或者 token 和环境变量里的对不上 |
| 请求被回 `421 Invalid Host header` | 开了 DNS rebinding 防护但 Host 没放行。配 `MCP_ALLOWED_HOSTS` |
| 请求被回 `403 Invalid Origin header` | 同上，浏览器客户端要配 `MCP_ALLOWED_ORIGINS` |
| 客户端连上来了但工具列表是空的 | `tools/__init__.py` 的 `MODULES` 里漏了这个模块（启动日志里也有一条 warning） |
| 启动直接报 `工具重名：xxx` | 两个模块里有同名函数——函数名就是工具名，改函数名 |
| 启动报 `MCP_PORT 必须是整数` | 环境变量里是字符串。`.env.example` 里写成 `MCP_PORT=8765` 就行，别加引号 |
| 启动报 `MCP_AUTH_TOKENS 和 MCP_AUTH_TOKEN 同时设了` | 迁移到一半。把单个那个并进表里（写成 `名字:token`）再删掉它 |
| 启动报 `每一段都要写成 名字:token` | 表里的某段少了名字或少了冒号。名字是给日志和归因看的，必填 |
| 端口被占 | 机器人占 `8000`。用 `ss -ltnp \| grep 8765` 看是谁 |
| 容器里起来了但外面连不上 | `MCP_HOST` 还是默认的 `127.0.0.1`。容器里必须 `0.0.0.0` |
| 所有工具一起变慢/卡住，不只是某一个 | 某个 `async` 工具里做了阻塞调用（`requests`、`time.sleep`、大计算），把共享的事件循环卡住了。见「多个 agent 同时调工具」 |
| 工具返回的结果偶尔不对、计数对不上 | 工具之间在用模块级变量共享状态。每个 `await` 都是切换点 |

## 已知限制

| 限制 | 后果 | 要做的事 |
|---|---|---|
| 会话状态在进程内存里 | 多副本部署时同一会话要落到同一个副本上，否则报错 | 反代开 sticky，或者启动时用 `stateless_http` |
| 只有共享密钥，没有 OAuth | 能按调用方归因和吊销，但**不能**按调用方给不同权限（谁拿到 token 谁就能调所有工具） | 换 SDK 的 `auth=AuthSettings(...)`，改的只是 `auth.py` 和 `server.py` |
| 没有 TLS | token 在链路上明文 | 反代终结 HTTPS |
| 限流是单进程内的 | 多副本部署时每个副本各算各的额度 | 反代那层限流，或者把桶换成 Redis |
| 没有容器化部署文件 | 上服务器要手工起进程 | 补 Dockerfile + compose，参考 `backend/` |
