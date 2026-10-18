# 部署

用 Docker Compose 部署，宿主端口 **8002**。日常发版一条命令，任何一步失败直接退出，不做兜底。

## 能只拉 agent 的代码吗

可以限制工作区，但 git 的拉取单位是提交，不是目录：

- **推荐做法：独立部署目录 + 稀疏检出**

```bash
git clone --filter=blob:none --sparse <仓库地址> /srv/feishubot-agent
cd /srv/feishubot-agent
git sparse-checkout set agent
```

之后的 `git pull` 只更新 `agent/` 的工作区；`--filter=blob:none` 还避免下载其他目录的 blob。
如果仓库已经完整 clone 过，再执行 `git sparse-checkout set agent` 只会隐藏其他目录，不会缩小已有的 `.git`。

- **不要在机器人正在使用的那个工作区做稀疏检出**，用独立目录。
- 真正“只拉 agent、不下载其他对象”最干净的方式是把 agent 拆成独立仓库或 submodule；现在它和 backend 在同一个仓库里。

> ⚠️ **前提：`agent/` 目前还是 untracked**（`git status` 里的 `?? agent/`）。
> 部署前必须先提交并推送 `agent/`、`uv.lock`、`Dockerfile`、`compose.yaml`、`scripts/deploy.sh`，
> 否则部署机 `git pull` 根本拿不到这些文件。

## 前提

| 需要 | 说明 |
|---|---|
| Docker + Compose v2 | `docker compose version` |
| git | 能访问仓库（SSH deploy key 或 token） |
| 端口 8002 | 只给 backend 访问，**不要暴露公网**（agent 的 `/turn` 没有鉴权） |
| 能访问 DeepSeek | 容器需要出网 |
| MCP 服务 | 默认假设跑在宿主机 `127.0.0.1:8765` |

## 首次部署

```bash
git clone --filter=blob:none --sparse <仓库地址> /srv/feishubot-agent
cd /srv/feishubot-agent
git sparse-checkout set agent
cd agent

cp .env.example .env
chmod 600 .env
vi .env
```

`.env` 至少填：

```bash
DEEPSEEK_API_KEY=sk-...
AGENT_MODEL=deepseek-v4-pro
AGENT_REASONING_EFFORT=none

# 宿主机上的 MCP（host 网络下 127.0.0.1 就是宿主机）
AGENT_MCP_URL=http://127.0.0.1:8765/mcp
AGENT_MCP_TOKEN=

# backend 也在宿主机时用 127.0.0.1；backend 在容器里时改成 0.0.0.0 并限制 8002 的访问来源
AGENT_HOST=127.0.0.1
```

启动：

```bash
bash scripts/deploy.sh
```

脚本只做三步：

```text
git pull --ff-only
docker compose build
docker compose up -d
```

- 构建失败不会影响正在跑的旧容器；
- 启动后是否健康，用下面的 `/healthz` 和 `docker compose logs` 确认；
- 没有重试、没有 `|| true`、没有额外分支。

## 网络与端口

compose 使用 `network_mode: host`：

- agent 直接监听宿主机 `AGENT_HOST:8002`，不做 `ports` 映射；
- 因此容器里可以继续用 `http://127.0.0.1:8765/mcp` 访问宿主机上的 MCP；
- 这是 Linux 部署方式。

backend 调用地址：

| backend 位置 | agent endpoint |
|---|---|
| 宿主机进程 | `http://127.0.0.1:8002/turn` |
| Docker 容器 | `http://host.docker.internal:8002/turn` |

如果 backend 在容器里，还需要：

1. agent 的 `AGENT_HOST=0.0.0.0`；
2. backend 的 compose 加 `extra_hosts: ["host.docker.internal:host-gateway"]`；
3. 用防火墙把宿主机 8002 限制给 Docker 网桥 / backend 所在主机。

**agent 的 `/turn` 没有鉴权**，不要直接把 8002 开到公网。

backend 的 `routes.json` 加一条：

```json
{
  "keyword": "助手",
  "endpoint": "http://127.0.0.1:8002/turn",
  "description": "向 agent 提问",
  "timeoutSeconds": 120
}
```

## 日常发版

```bash
cd /srv/feishubot-agent
bash agent/scripts/deploy.sh
```

脚本自身受 git 管理，`git pull` 是第一步，所以更新脚本也会一起生效。

## 验证

```bash
cd /srv/feishubot-agent/agent

docker compose ps
docker compose logs -f agent

curl -sS http://127.0.0.1:8002/healthz
```

不调用模型的空文本自测：

```bash
curl -sS -X POST http://127.0.0.1:8002/turn \
  -H 'Content-Type: application/json' \
  -d '{"keyword":"助手","text":"   ","messageId":"om_check","chatId":"oc_check"}'
# 期望：{"title":"助手","text":"请提供要处理的内容。","tone":"warning"}
```

真实场景再发一条带内容的请求；日志里应能看到模型调用、MCP 工具调用和客户端/服务端耗时。

## 回滚

```bash
git revert <出问题的提交>
git push
cd /srv/feishubot-agent
bash agent/scripts/deploy.sh
```

没有镜像级回滚，旧版本需要重新构建。

## 排错

| 现象 | 原因 |
|---|---|
| `env file .../.env not found` | 没有创建 `.env`，或不在 `agent/` 目录下执行 |
| 启动报 `缺少 DEEPSEEK_API_KEY` | `.env` 没填或没被 compose 注入 |
| 启动日志 `启动时列出 MCP 工具失败` | `AGENT_MCP_URL` 不通；host 网络下确认 MCP 在宿主机 8765 |
| 容器起来后马上退出 | 看 `docker compose logs agent`；确认 8002 没被占用、模型 key 有效 |
| 构建卡在拉基础镜像 | 改 `compose.yaml` 的 `build.args.PYTHON_IMAGE` |
| 构建卡在装依赖 | 改 `compose.yaml` 的 `build.args.PYPI_INDEX` |
| backend 容器连不上 agent | `AGENT_HOST` 改成 `0.0.0.0`，并检查 `host.docker.internal` / 防火墙 |
