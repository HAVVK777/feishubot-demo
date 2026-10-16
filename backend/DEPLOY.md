# 部署

用 Docker Compose 部署。日常发版一条命令，不做兜底——任何一步失败就中止。

**只能单副本。** 去重状态在进程内存里（`adapters/memory.py`），多副本之间互不可见，
飞书重投的消息会被重复回复。compose 里靠固定 `container_name` 把这条约束钉死了。

## 前提

服务器上要有：

| 需要 | 怎么确认 |
|---|---|
| Docker | `docker --version` |
| **Compose v2 插件** | `docker compose version`（注意是 `docker compose`，中间是空格） |
| git | `git --version` |
| 能访问仓库 | 远程是 SSH 地址，需要配 deploy key |
| 端口放行 | 云厂商安全组 + 服务器上的 ufw/firewalld |

Compose v1 那个 `docker-compose`（带横杠）**不行**，脚本用的是 v2 的子命令形式。

## 首次部署

```bash
git clone git@github.com:HAVVK777/feishubot-demo.git
cd feishubot-demo/backend

cp .env.example .env
chmod 600 .env              # 里面有 App Secret
vi .env                     # 填下面「配置」里那三个必填项

cp routes.json routes.local.json
vi routes.local.json        # 把两条占位路由换成真实地址，见下

bash scripts/deploy.sh
```

`routes.json` 里现在是占位值（`127.0.0.1:9001/search`、`127.0.0.1:9002/summarize`）。
不换的话服务能正常起来，但每次调用都会回一张橙色的"服务暂时不可用"卡片——因为地址
根本不通。换好之后**不用重启**（`routes.local.json` 是只读挂载，改了要重新
`docker compose up -d` 才生效）。

## 日常发版

本地改完 push，服务器上：

```bash
bash backend/scripts/deploy.sh
```

脚本就三步：`git pull` → `docker compose build` → `docker compose up -d`。构建和启动
分两步，所以构建失败时线上正在跑的容器不受影响。

**脚本自己受 git 管理**，`git pull` 是它的第一步，所以每次发版脚本也会跟着更新。

**`.env` 和 `routes.local.json` 永远不会被覆盖**——两个都在 `.gitignore` 里，git 不碰它们。
建一次就行。

## 配置

### `.env`

只被 Docker Compose 读（应用自己不读 `.env`，它只看 `os.environ`；compose 的
`env_file:` 会把变量真正注入容器进程环境）。

| 变量 | 必填 | 说明 |
|---|---|---|
| `FEISHU_APP_ID` | ✓ | 飞书后台「凭证与基础信息」 |
| `FEISHU_APP_SECRET` | ✓ | 同上 |
| `FEISHU_VERIFICATION_TOKEN` | ✓ | 飞书后台「事件与回调 → 加密策略」。**留空则不校验来源**，线上必须填 |
| `FEISHU_BOT_PUBLISH_PORT` | | 对外发布端口，默认 8000 |
| `FEISHU_BOT_BIND_ADDR` | | 发布到哪个地址，默认 `127.0.0.1`。见下 |

`.env` 里那行 `FEISHU_BOT_HOST=127.0.0.1` **保持原样别改**。容器内必须绑 `0.0.0.0`，
这个值已经由 `compose.yaml` 的 `environment:` 段覆盖掉了（那里的优先级高于 `env_file:`）。
那个 `127.0.0.1` 是留给"不走 Docker、直接在宿主机跑"的场景的。

### 两种对外暴露方式

| 方式 | 怎么配 | 回调地址 |
|---|---|---|
| **nginx 转发**（默认） | 什么都不用改 | `https://<域名>/feishu/events` |
| **直接暴露端口** | `FEISHU_BOT_BIND_ADDR=0.0.0.0`<br>`FEISHU_BOT_PUBLISH_PORT=18080` | `http://<公网IP>:18080/feishu/events` |

切这两种模式**只改 `.env`，脚本和代码都不用动**。直接暴露的话记得在安全组放行该端口，
且别选 80/443。

飞书官方 FAQ 要求的是"公网可达的 IPv4 地址，以 `http://` 或 `https://` 开头"，
没提端口限制。裸 IP + 非标端口能不能过，**以控制台保存那一下为准**——保存时飞书会发
真实校验请求，1 秒内收不到 challenge 就报错。

## 验证

```bash
cd backend
docker compose ps                    # 看到 feishubot 是 Up
docker compose logs bot              # 看启动日志
```

自测 challenge。**要带上 token**，因为配了 token 之后校验请求也要带（`inbound.py:78`）：

```bash
curl -s http://127.0.0.1:8000/feishu/events \
  -H 'Content-Type: application/json' \
  -d '{"type":"url_verification","challenge":"abc123","token":"<FEISHU_VERIFICATION_TOKEN>"}'
# 期望输出：{"challenge":"abc123"}
```

把 `127.0.0.1:8000` 换成你的实际地址，**而且要从外网再打一次**——在服务器本机打测不出
安全组有没有放行。

然后去飞书后台「事件与回调 → 订阅方式 → 将事件发送至开发者服务器」填回调地址并保存。
**顺序不能反**：保存时飞书立刻发真请求，服务没起来就保存不了。

最后在飞书里给机器人发 `/帮助`，卡片能回来才算全链路通了。

## 回滚

代码回退，然后重新部署：

```bash
git revert <出问题的提交>
git push
bash backend/scripts/deploy.sh
```

用 `git revert` 而不是 `git checkout <旧提交>`：后者会进入 detached HEAD，脚本第一步的
`git pull` 会直接失败。

**没有镜像级的回滚。** 每次构建都覆盖同一个镜像 tag，旧镜像不会留档，所以退回旧版本
必须重新构建。要真正的秒级回滚得给镜像打版本 tag，目前没做。

## 排错

| 现象 | 原因 |
|---|---|
| 构建卡在拉基础镜像 / `registry-1.docker.io` 超时 | Docker Hub 不通。改 `compose.yaml` 的 `build.args.PYTHON_IMAGE` 换镜像源 |
| 构建卡在装依赖 | 同上，改 `build.args.PYPI_INDEX` |
| 启动报 `缺少环境变量：FEISHU_APP_ID、FEISHU_APP_SECRET` | `.env` 没建或那两项没填 |
| `up` 报 `bind source path does not exist: .../routes.local.json` | 没建 `routes.local.json`。这是**故意的**——默认行为会把它建成一个空目录，报出来的是"读不了配置"，离真正的原因很远 |
| 启动报 `找不到路由配置文件：/app/routes.local.json` | 路由文件没挂上（正常走 compose 不会遇到，一般是手工 `docker run` 漏了 `-v`） |
| 收到 `501` | 飞书后台开了 Encrypt Key，但本项目没实现解密。去「加密策略」清空它，或在 `inbound.py` 补解密分支 |
| 服务起来了，但每次调用回橙色"服务暂时不可用"卡片 | `routes.local.json` 里还是占位地址，外呼根本不通。详情看 `docker compose logs bot` |
| 飞书保存回调地址失败 | 服务没起 / 安全组没放行 / challenge 没在 1 秒内返回。先用上面的 curl 从外网测一次 |

## 已知限制

- **单副本**（见开头）。
- **无镜像级回滚**（见「回滚」）。
- **明文链路 + 没有签名校验**：`X-Lark-Signature` 没实现，来源校验只靠请求体里的
  Verification Token。走 HTTP 时它没有传输层保护，建议在反代那层配上 HTTPS。
- **`/docs` 会随服务暴露**（FastAPI 默认开启）。只是一条 POST 路由的 API 文档，
  介意的话在 `bootstrap.py` 建 `FastAPI(...)` 时加 `docs_url=None`。
- **日志上限 10m × 3**（见 `compose.yaml` 的 `logging`）。要留更久就调大。
