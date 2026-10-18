#!/usr/bin/env bash
#
# 部署：拉取最新代码 → 构建镜像 → 启动。
#
# 每一步失败都直接中止（set -e），不做重试/兜底。构建和启动分开，构建失败时
# 线上正在跑的容器不受影响。
#
# 这个脚本本身受 git 管理，`git pull` 是第一步，所以每次发版脚本也会更新。

set -euo pipefail

# 切到 mcp/：compose.yaml 和 .env 都在这一层。
cd "$(dirname "$0")/.."

git pull --ff-only
docker compose build
docker compose up -d
