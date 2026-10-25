# syntax=docker/dockerfile:1.7
# Imagens Python do CryptoInvest: alvos `runner`, `api` e `migrate` (docker build --target <alvo> .)

FROM ghcr.io/astral-sh/uv:0.11.2@sha256:c4f5de312ee66d46810635ffc5df34a1973ba753e7241ce3a08ef979ddd7bea5 AS uv

FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /build
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra ai --no-install-project

FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS base
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin app \
    && install -d -o app -g app -m 0750 /var/lib/cryptoinvest /var/lib/cryptoinvest/paper /var/lib/cryptoinvest/cache
COPY --from=builder /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SECRETS_DIR=/run/secrets \
    ENVIRONMENT=prod
WORKDIR /app
COPY engine ./engine
COPY api ./api
COPY config ./config
COPY alembic.ini ./
COPY deploy/healthcheck.py ./deploy/healthcheck.py

FROM base AS runner
USER 10001:10001
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD ["python", "/app/deploy/healthcheck.py", "runner"]
STOPSIGNAL SIGTERM
CMD ["python", "-m", "engine.runner.paper", "start", "--run-id", "paper-main", "--data-dir", "/var/lib/cryptoinvest/paper", "--json-logs"]

FROM base AS api
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "/app/deploy/healthcheck.py", "api"]
CMD ["python", "-m", "api.serve", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS migrate
USER root
# Versao fixa do cliente (build reprodutivel). O Debian remove versoes antigas do espelho de seguranca: ao falhar com
# "Version not found", consulte `apt-cache madison postgresql-client-15` e atualize o ARG.
ARG PG_CLIENT_VERSION=15.19-0+deb12u1
RUN apt-get update \
    && apt-get install -y --no-install-recommends "postgresql-client-15=${PG_CLIENT_VERSION}" "libpq5=${PG_CLIENT_VERSION}" \
    && rm -rf /var/lib/apt/lists/*
COPY scripts/db_roles.sql ./scripts/db_roles.sql
COPY deploy/migrate.sh ./deploy/migrate.sh
USER 10001:10001
ENTRYPOINT ["sh", "/app/deploy/migrate.sh"]
