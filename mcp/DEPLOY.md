# 部署

用 Docker Compose 部署 MCP 服务，宿主端口 **8003**。日常发版一条命令，任何一步失败直接退出。

## 只拉 mcp 的代码

推荐独立部署目录 + 稀疏检出：

```bash
git clone --filter=blob:none --sparse <仓库地址> /srv/feishubot-mcp
cd /srv/feishubot-mcp
git sparse-checkout set mcp
```

之后的 `git pull` 只更新 `mcp/` 的工作区。

> ⚠️ 部署前 mcp 的代码和本次新增的 `Dockerfile`、`compose.yaml`、`scripts/deploy.sh`
> 必须已经 commit + push，否则部署机 pull 不到。

## 前提

| 需要 | 说明 |
|---|---|
| Docker + Compose v2 | `docker compose version` |
| git | 能访问仓库 |
| 端口 8003 | 默认只绑 `127.0.0.1`；远程 agent 才需要对外 |
| 飞书应用凭据 | `.env` 里的 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` |
| 多维表格权限 | 应用要被加为表格的「文档应用」，不是「协作者」 |
| 出网 | 容器要能访问飞书开放平台 |

## 首次部署

```bash
git clone --filter=blob:none --sparse <仓库地址> /srv/feishubot-mcp
cd /srv/feishubot-mcp
git sparse-checkout set mcp
cd mcp

cp .env.example .env
chmod 600 .env
vi .env
```

`.env` 至少填：

```bash
FEISHU_APP_ID=cli_...
FEISHU_APP_SECRET=...
FEISHU_LEAVE_BITABLE_APP_TOKEN=...
FEISHU_LEAVE_BITABLE_TABLE_ID=tbl...

# 需要鉴权时填 `名字:token`；agent 侧对应 AGENT_MCP_TOKEN
MCP_AUTH_TOKENS=
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

## 网络与端口

- 容器内监听 `0.0.0.0:8003`，宿主默认发布到 `127.0.0.1:8003`。
- 同机 agent 用 `AGENT_MCP_URL=http://127.0.0.1:8003/mcp` 即可（agent 容器是 host 网络）。
- 远程 agent 时才需要：

```bash
MCP_BIND_ADDR=0.0.0.0
MCP_AUTH_TOKENS="feishu-agent:<token>"
MCP_ALLOWED_HOSTS=mcp.example.com   # 走域名/反代时
```

同时记得放行防火墙，并给 agent 配对应的 `AGENT_MCP_TOKEN`。

## 验证

```bash
cd /srv/feishubot-mcp/mcp

docker compose ps
docker compose logs -f mcp
curl -sS http://127.0.0.1:8003/healthz
# {"status":"ok","server":"feishubot-mcp","version":"0.1.0"}
```

工具自检（在仓库 checkout 的 `mcp/` 目录下跑，脚本不在镜像里）：

```bash
MCP_URL=http://127.0.0.1:8003/mcp MCP_TOKEN=<token> \
  uv run python scripts/agent_client.py
```

## 日常发版

```bash
cd /srv/feishubot-mcp
bash mcp/scripts/deploy.sh
```

## 回滚

```bash
git revert <出问题的提交>
git push
cd /srv/feishubot-mcp
bash mcp/scripts/deploy.sh
```

没有镜像级回滚，旧版本需要重新构建。

## 排错

| 现象 | 原因 |
|---|---|
| `env file .../.env not found` | 没有创建 `.env`，或不在 `mcp/` 目录下执行 |
| 启动日志提示未配置 `MCP_AUTH_TOKENS` | 没开鉴权；本地可以接受，线上应配置 |
| `bind: address already in use` | 宿主机已有一个 MCP 进程占着 8003；先停掉它 |
| 工具调用报飞书权限错误（如 91403） | 应用没被加为多维表格的「文档应用」 |
| 工具调用报 401 | agent 的 `AGENT_MCP_TOKEN` 和 `MCP_AUTH_TOKENS` 不匹配 |
| 构建卡在拉基础镜像 | 改 `compose.yaml` 的 `build.args.PYTHON_IMAGE` |
| 构建卡在装依赖 | 改 `compose.yaml` 的 `build.args.PYPI_INDEX` |

## 已知限制

- 会话状态、限流桶、飞书 token 缓存都在进程内存里；容器重启清零。
- 单副本；多副本需要 sticky 或共享存储。
- 没有 TLS，对外必须走反代。
