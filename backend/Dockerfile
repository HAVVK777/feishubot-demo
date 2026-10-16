# 基础镜像做成构建参数：国内服务器直连 Docker Hub 常常拉不动，换个镜像源前缀
# 即可，不用改这个文件（compose.yaml 里有取消注释就能用的写法）。
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

# PyPI 同理，pip 和 uv 都指向它。
ARG PYPI_INDEX=https://pypi.org/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_INDEX_URL=${PYPI_INDEX} \
    UV_PYTHON_PREFERENCE=only-system \
    PATH="/app/.venv/bin:$PATH"

# UV_PYTHON_PREFERENCE=only-system 是必要的：uv 默认偏好自己管理的 Python，会
# 去 GitHub 下载一份 python-build-standalone。基础镜像里已经有 3.12 了，禁掉
# 下载既省一次网络往返，也让构建结果可预期。

RUN pip install --no-cache-dir --index-url "${PYPI_INDEX}" uv \
 && useradd --create-home --uid 10001 app \
 && mkdir -p /app \
 && chown app:app /app

WORKDIR /app

# 以非 root 用户构建并运行。放在 COPY 之前，这样后面所有产物（含 .venv）天然
# 属于 app，不需要事后 chown -R——那个命令会连带改掉 .venv 里指向系统 Python
# 的符号链接的目标属主。
USER app

# 先只拷依赖清单：只改了 src/ 的时候这一层还能命中缓存，重建不必重装依赖，
# 每次 CD 快很多。
COPY --chown=app:app pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY --chown=app:app src/ ./src/
RUN uv sync --frozen --no-dev

# 刻意不拷 routes.json：compose 一定设了 FEISHU_ROUTES_FILE。缺文件时应用自己
# 会在启动阶段报「找不到路由配置文件」，好过悄悄用那份带占位地址的默认配置。
EXPOSE 8000

CMD ["python", "-m", "feishu_bot"]
