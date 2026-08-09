FROM ghcr.io/astral-sh/uv:0.12.0 AS uv

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=uv /uv /uvx /bin/

RUN groupadd --system --gid 10001 orbit-data \
    && useradd --system --uid 10001 --gid orbit-data --home-dir /app orbit-data \
    && mkdir -p /app /data \
    && chown orbit-data:orbit-data /app /data

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY vendor ./vendor
RUN uv sync --frozen --no-dev --no-editable

COPY --chown=orbit-data:orbit-data config/orbit-data.toml /etc/orbit-data.toml

ENV PATH="/app/.venv/bin:$PATH"
USER 10001:10001
VOLUME ["/data"]

ENTRYPOINT ["orbit-data"]
CMD ["--help"]
