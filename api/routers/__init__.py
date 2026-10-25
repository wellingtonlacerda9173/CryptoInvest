from fastapi import APIRouter

from api.routers import (
    ai,
    audit,
    auth,
    backtest,
    control,
    credentials,
    health,
    onboarding,
    sessions,
    settings,
    trading,
    users,
    ws,
)

# Rotas novas: leitura com `UserDep`, mutacao com `AdminDep`/`FreshAdminDep`. A politica e verificada por
# tests/unit/api/test_route_policy.py, que falha para rota nova sem autorizacao explicita. O WebSocket nao
# entra no OpenAPI: tem politica propria (cookie, Origin, papel) e contrato em `api/ws_schema.json`.
PUBLIC_ROUTERS: list[APIRouter] = [health.router]
PROTECTED_ROUTERS: list[APIRouter] = [
    auth.router,
    sessions.router,
    users.router,
    audit.router,
    credentials.router,
    trading.router,
    control.router,
    backtest.router,
    settings.router,
    onboarding.router,
    ai.router,
]
WEBSOCKET_ROUTERS: list[APIRouter] = [ws.router]
