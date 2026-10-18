# 和 backend / agent 一样：基础镜像和 PyPI 源做成构建参数，国内服务器可换镜像源。
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

# 基础镜像已带 tzdata（leave.py 用 ZoneInfo("Asia/Shanghai")），不跑 apt。
RUN pip install --no-cache-dir --index-url "${PYPI_INDEX}" uv \
 && useradd --create-home --uid 10001 app \
 && mkdir -p /app \
 && chown app:app /app

WORKDIR /app

USER app

# 先只拷依赖清单，改 src/ 时这层还能命中缓存。
COPY --chown=app:app pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY --chown=app:app src/ ./src/
RUN uv sync --frozen --no-dev

EXPOSE 8003

CMD ["python", "-m", "feishubot_mcp"]
