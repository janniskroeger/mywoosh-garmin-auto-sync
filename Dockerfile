FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv

ENV UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=python3.13 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    DATA_DIR=/data \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first so this layer is cached until the lock file changes.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin app \
    && mkdir /data \
    && chown app:app /data
USER app

VOLUME /data

ENTRYPOINT ["mywhoosh-garmin-sync"]
CMD ["run"]
