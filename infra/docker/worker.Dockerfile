FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app

COPY pyproject.toml uv.lock* ./
COPY libs/core/pyproject.toml libs/core/
COPY apps/worker/pyproject.toml apps/worker/
RUN uv sync --package atp-worker --no-install-project --frozen || uv sync --package atp-worker

COPY libs/ libs/
COPY apps/worker/ apps/worker/
RUN uv sync --package atp-worker

RUN useradd --create-home --uid 1000 atp && chown -R atp:atp /app
USER atp

ENV PATH="/app/.venv/bin:$PATH"

# SIGTERM must reach Python so the worker shuts down cleanly rather than being
# killed mid-order. No shell wrapper.
STOPSIGNAL SIGTERM
CMD ["python", "-m", "atp_worker.main"]
