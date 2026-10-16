#!/usr/bin/env bash
#
# 部署：拉取最新代码 → 构建镜像 → 启动。
#
# 三步各做一件事，不做兜底：任何一步失败都直接中止，后面的步骤不会在半成品
# 状态下继续跑。所以这里没有 || true、没有重试、没有条件分支——`set -e` 就是
# "遇到错误直接抛出"。
#
# 构建和启动故意分成两步而不是 `up -d --build`：构建失败时线上正在跑的容器
# 不受影响。
#
# 这个脚本本身受 git 管理，而 `git pull` 就是它的第一步，所以每次发布脚本自己
# 也会被更新。

set -euo pipefail

# 切到 backend/：compose.yaml 和 .env 都在这一层。
cd "$(dirname "$0")/.."

git pull
docker compose build
docker compose up -d
