FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app

# Dependency layer first — code changes should not reinstall the world.
COPY pyproject.toml uv.lock* ./
COPY libs/core/pyproject.toml libs/core/
COPY apps/api/pyproject.toml apps/api/
RUN uv sync --package atp-api --no-install-project --frozen || uv sync --package atp-api

COPY libs/ libs/
COPY apps/api/ apps/api/
RUN uv sync --package atp-api

# Non-root: this process holds broker credentials.
RUN useradd --create-home --uid 1000 atp && chown -R atp:atp /app
USER atp

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/healthz')"

CMD ["uvicorn", "atp_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
