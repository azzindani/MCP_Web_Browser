# syntax=docker/dockerfile:1.7
# ─────────────────────────────────────────────────────────────────────────────
# mcp-web-browser — production container. Generated from MCP_Math's
# templates/Dockerfile.single.template (see MCP_Math/templates/README.md).
#
# Two-stage build: uv sync into a venv + install Chromium, then copy venv +
# source into a slim python:3.12 runtime with Chromium's system deps.
#
# Build:  docker build -t mcp-web-browser:latest .
# Run:    docker run --rm -p 8766:8766 mcp-web-browser:latest
# Auth:   docker run --rm -p 8766:8766 -e WEB_API_KEY=secret mcp-web-browser:latest
# ─────────────────────────────────────────────────────────────────────────────

ARG PYTHON_VERSION=3.12-slim

FROM python:${PYTHON_VERSION} AS builder
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY engine ./engine
COPY shared ./shared
COPY server.py deploy_auth.py ./
RUN uv sync --frozen --no-dev

FROM python:${PYTHON_VERSION} AS runtime
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/engine /app/engine
COPY --from=builder /app/shared /app/shared
COPY --from=builder /app/server.py /app/deploy_auth.py ./
COPY pyproject.toml ./

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH="/app" \
    PYTHONUNBUFFERED=1 \
    WEB_TRANSPORT=http \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8766 \
    PLAYWRIGHT_BROWSERS_PATH=/app/.cache/ms-playwright

# Chromium's system deps + the browser binary itself. Run as root for this
# layer (playwright install-deps needs apt); drop to an unprivileged user
# after.
RUN playwright install --with-deps chromium chromium-headless-shell \
    && groupadd -r app && useradd -r -g app app \
    && chown -R app:app /app \
    && mkdir -p /home/app && chown app:app /home/app

USER app
EXPOSE 8766

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"WEB_PORT\"]}/health', timeout=3)" || exit 1

ENTRYPOINT ["python", "/app/server.py"]
