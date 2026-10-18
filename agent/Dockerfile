# 基础镜像和 PyPI 源都做成构建参数：国内服务器直连 Docker Hub / PyPI 经常不通，
# 换镜像源只用改 compose.yaml 的 build.args，不用改这个文件。
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ARG PYPI_INDEX=https://pypi.org/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_INDEX_URL=${PYPI_INDEX} \
    UV_PYTHON_PREFERENCE=only-system \
    TZ=Asia/Shanghai \
    PATH="/app/.venv/bin:$PATH"

# tzdata 是给 `date.today()` 用的：agent 会把当前日期拼进系统提示，
# 没有时区数据时容器默认按 UTC 走，临近午夜会算错“这个月”。
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir --index-url "${PYPI_INDEX}" uv \
 && useradd --create-home --uid 10001 app \
 && mkdir -p /app \
 && chown app:app /app

WORKDIR /app

# 以非 root 用户构建并运行；后面的产物（含 .venv）天然属于 app。
USER app

# 先只拷依赖清单，改 src/ 时这层还能命中缓存。
COPY --chown=app:app pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY --chown=app:app src/ ./src/
RUN uv sync --frozen --no-dev

EXPOSE 8002

CMD ["python", "-m", "feishubot_agent"]
