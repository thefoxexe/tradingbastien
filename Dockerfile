FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:0.4.30 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY bot ./bot
COPY api ./api
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 1000 bot
USER bot
ENV PATH="/app/.venv/bin:$PATH"

CMD ["python", "-m", "bot.main"]
