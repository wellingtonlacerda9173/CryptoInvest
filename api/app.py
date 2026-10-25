import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api import __version__
from api.binance_keys import BinanceKeyVerifier, ExchangeKeyVerifier
from api.clock import Clock, utc_now
from api.config import ApiSettings
from api.container import Container
from api.deps import enforce_origin
from api.errors import install_error_handlers
from api.middleware import BodyLimitMiddleware, RequestContextMiddleware
from api.routers import PROTECTED_ROUTERS, PUBLIC_ROUTERS, WEBSOCKET_ROUTERS
from api.runner_link import EventSource, RedisRunnerLink, RunnerLink
from api.security.crypto import KeyRing
from api.security.passwords import PasswordService, build_hasher
from api.security.throttle import FailureThrottle
from api.services.backtests import BacktestRunner
from api.services.commands import CommandPump
from api.services.launcher import RunnerLauncher, SubprocessLauncher
from api.services.realtime import RealtimeHub
from engine.infrastructure.config.logging import configure_logging
from engine.infrastructure.config.redis_auth import with_redis_password
from engine.infrastructure.config.settings import resolve_secrets_dir
from engine.infrastructure.persistence.schema_check import SchemaMismatchError, ensure_schema_current
from engine.infrastructure.persistence.session import create_engine, create_session_factory
from engine.runner.paper_events import RedisKeys

log = structlog.get_logger()

DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"

PUBLIC_OPERATIONS = frozenset({"getHealth", "getReadiness", "login"})
TAGS = [
    {"name": "health", "description": "Vivacidade e prontidao (sem autenticacao)."},
    {
        "name": "auth",
        "description": "Login em 2 passos (senha + TOTP), sessao, senha e codigos de recuperacao.",
    },
    {"name": "sessions", "description": "Sessoes ativas do proprio usuario."},
    {"name": "users", "description": "Gestao de usuarios (somente admin)."},
    {"name": "audit", "description": "Trilha de auditoria append-only (somente admin)."},
    {
        "name": "credentials",
        "description": "Chaves da Binance: gravacao write-only, so metadados na leitura.",
    },
    {
        "name": "trading",
        "description": "Leituras: status, saldo, equity, posicoes, operacoes, decisoes, desempenho, Live.",
    },
    {
        "name": "control",
        "description": "Comandos ao robo (assinados, com fila duravel) e ativacao do Live (indisponivel).",
    },
    {"name": "backtest", "description": "Backtests em subprocesso: criar, acompanhar, ler e cancelar."},
    {"name": "settings", "description": "Configuracao versionada: pares, estrategias, risco e IA."},
    {"name": "onboarding", "description": "Primeiro acesso e inicio no Paper."},
]
DESCRIPTION = (
    "API do painel CryptoInvest. Autenticacao por cookie de sessao httpOnly (SameSite=Strict) obtido em "
    "`POST /auth/login` + `POST /auth/2fa/verify`; metodos mutaveis exigem o header `X-CSRF-Token`. "
    "Erros seguem RFC 7807 (`application/problem+json`)."
)


def build_container(
    settings: ApiSettings,
    sessions: async_sessionmaker[AsyncSession],
    *,
    verifier: ExchangeKeyVerifier | None = None,
    clock: Clock | None = None,
    link: RunnerLink | None = None,
    events: EventSource | None = None,
    launcher: RunnerLauncher | None = None,
) -> Container:
    keys = RedisKeys(settings.redis_prefix)
    signing_key = (
        None if settings.command_hmac_key is None else settings.command_hmac_key.get_secret_value().encode()
    )
    link = link or RedisRunnerLink(
        with_redis_password(settings.redis_url or DEFAULT_REDIS_URL, resolve_secrets_dir()),
        keys,
        signing_key=signing_key,
    )
    if launcher is None and settings.runner_start_command:
        launcher = SubprocessLauncher(settings.runner_start_command)
    now = clock or utc_now
    hasher = build_hasher(
        time_cost=settings.argon2_time_cost,
        memory_kib=settings.argon2_memory_kib,
        parallelism=settings.argon2_parallelism,
    )
    container = Container(
        settings=settings,
        sessions=sessions,
        passwords=PasswordService(hasher),
        keyring=KeyRing.parse(settings.master_keys.get_secret_value()),
        verifier=verifier
        or BinanceKeyVerifier(
            settings.binance_api_origin, timeout_seconds=settings.binance_verify_timeout_seconds
        ),
        clock=now,
        ip_throttle=FailureThrottle(threshold=20),
        user_throttle=FailureThrottle(threshold=5),
        link=link,
        launcher=launcher,
        command_key=signing_key,
    )
    container.hub = RealtimeHub(
        events or link.events(),
        clock=now,
        link=link,
        buffer_size=settings.ws_replay_buffer,
        stale_seconds=settings.runner_heartbeat_stale_seconds,
        queue_size=settings.ws_queue_size,
        max_dropped=settings.ws_max_dropped_events,
    )
    container.backtests = BacktestRunner(container)
    return container


def _secure_openapi(app: FastAPI) -> dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema
    schema = FastAPI.openapi(app)
    components = schema.setdefault("components", {})
    components["securitySchemes"] = {
        "cookieAuth": {
            "type": "apiKey",
            "in": "cookie",
            "name": "ci_session",
            "description": "Cookie de sessao httpOnly (em producao `__Host-ci_session`).",
        },
        "csrfToken": {
            "type": "apiKey",
            "in": "header",
            "name": "X-CSRF-Token",
            "description": "Valor do cookie `ci_csrf` (ou `csrf_token` das respostas de login/me).",
        },
    }
    for path_item in schema["paths"].values():
        for method, operation in path_item.items():
            if operation.get("operationId") in PUBLIC_OPERATIONS:
                continue
            mutating = method.upper() not in {"GET", "HEAD", "OPTIONS"}
            operation["security"] = [{"cookieAuth": [], "csrfToken": []} if mutating else {"cookieAuth": []}]
    app.openapi_schema = schema
    return schema


def create_app(
    settings: ApiSettings,
    *,
    database_url: URL | str | None = None,
    sessions: async_sessionmaker[AsyncSession] | None = None,
    verifier: ExchangeKeyVerifier | None = None,
    clock: Clock | None = None,
    link: RunnerLink | None = None,
    events: EventSource | None = None,
    launcher: RunnerLauncher | None = None,
    background: bool = True,
    verify_schema: bool = False,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = None
        if not hasattr(app.state, "container"):
            if database_url is None:
                raise RuntimeError("database_url ou sessions e obrigatorio")
            engine = create_engine(database_url)
            if verify_schema:
                try:
                    await ensure_schema_current(engine)
                except SchemaMismatchError:
                    await engine.dispose()
                    raise
            app.state.container = build_container(
                settings,
                create_session_factory(engine),
                verifier=verifier,
                clock=clock,
                link=link,
                events=events,
                launcher=launcher,
            )
        container: Container = app.state.container
        tasks: list[asyncio.Task[None]] = []
        if background:
            if container.hub is not None:
                tasks += [
                    asyncio.create_task(container.hub.run()),
                    asyncio.create_task(container.hub.monitor()),
                ]
            tasks.append(asyncio.create_task(CommandPump(container).run()))
            if container.backtests is not None:
                await container.backtests.recover()
                tasks.append(asyncio.create_task(container.backtests.run_sweeper()))
        log.info("api_started", environment=settings.environment, version=__version__)
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if container.backtests is not None:
                await container.backtests.shutdown()
            if container.link is not None:
                with contextlib.suppress(Exception):
                    await container.link.close()
            if engine is not None:
                await engine.dispose()

    app = FastAPI(
        title="CryptoInvest API",
        version=__version__,
        description=DESCRIPTION,
        openapi_tags=TAGS,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
        dependencies=[Depends(enforce_origin)],
    )
    if sessions is not None:
        app.state.container = build_container(
            settings, sessions, verifier=verifier, clock=clock, link=link, events=events, launcher=launcher
        )

    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_body_bytes)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.panel_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-CSRF-Token", "X-Request-ID"],
        expose_headers=["X-Request-ID", "Retry-After"],
        max_age=600,
    )
    app.add_middleware(RequestContextMiddleware, hsts_max_age=settings.hsts_max_age_seconds)
    install_error_handlers(app)
    for router in [*PUBLIC_ROUTERS, *PROTECTED_ROUTERS, *WEBSOCKET_ROUTERS]:
        app.include_router(router)
    app.openapi = lambda: _secure_openapi(app)  # type: ignore[method-assign]
    return app


def create_default_app() -> FastAPI:
    """Fabrica para `uvicorn api.app:create_default_app --factory`."""
    from api.config import api_database_url, load_api_settings
    from engine.infrastructure.config.settings import load_settings

    settings = load_api_settings()
    configure_logging(settings.log_level, json=settings.environment != "dev")
    engine_settings = load_settings()
    if settings.redis_url is None:
        settings = settings.model_copy(update={"redis_url": engine_settings.redis_url})
    return create_app(
        settings,
        database_url=api_database_url(settings, engine_settings.database_url),
        verify_schema=not settings.skip_schema_check,
    )
