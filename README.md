# CryptoInvest

Robo de trading de criptomoedas (Binance Spot, somente long, sem alavancagem). Python 3.12, asyncio.

Fase 1: monorepo, dominio, dados de mercado da Binance (sem chave) e persistencia (PostgreSQL + TimescaleDB).

## Estrutura

| Caminho | Funcao |
|---|---|
| `engine/domain` | Modelos Pydantic imutaveis (Candle, Signal, Order, Position, Trade, Decision, RiskConfig), regras puras e Protocol `ExchangeGateway` |
| `engine/application`, `engine/runner` | Reservados para as proximas fases |
| `engine/infrastructure` | Cliente Binance (REST paginado + WebSocket), persistencia SQLAlchemy/Alembic, configuracao |
| `api`, `web` | Placeholders |
| `tests/unit`, `tests/integration` | Testes (integracao marcada com `integration`) |

## Pre-requisitos

Python 3.12, [uv](https://docs.astral.sh/uv/), Docker, PowerShell.

## Execucao (PowerShell)

```powershell
./scripts/dev.ps1 setup      # uv sync + cria .env e secrets/database_password (aleatorio)
./scripts/dev.ps1 up         # sobe timescaledb e redis (docker-compose.dev.yml)
./scripts/dev.ps1 migrate    # alembic upgrade head
./scripts/dev.ps1 check      # ruff + mypy --strict + pytest (unitarios)
./scripts/dev.ps1 test-integration   # Binance publica + Timescale (pula se indisponiveis)
./scripts/dev.ps1 down
```

Equivalentes diretos: `uv sync`, `uv run ruff check .`, `uv run mypy --strict engine`, `uv run pytest`,
`uv run pytest -m integration`, `uv run alembic upgrade head`.

## Segredos

Segredos nunca ficam em codigo, `.env` ou logs. Cada segredo e um arquivo em `SECRETS_DIR`
(`/run/secrets` por padrao, `./secrets` em dev), com o nome do campo: `database_password`,
`binance_api_key`, `binance_api_secret`. O compose usa o mesmo arquivo como Docker secret.
A fase 1 usa apenas dados publicos e nao requer chave da Binance.

## Regras de dominio garantidas por tipo

- `EntryOrder` nao existe sem `stop_loss` (campo obrigatorio, abaixo do preco de entrada); `Signal` exige alvo e
  retorno/risco minimo (padrao 1:2).
- Apenas spot long: nao ha campos de alavancagem, margem, futuros ou venda a descoberto.
- `Candle` valida OHLC e volume, com timestamps em UTC; o repositorio persiste somente velas fechadas.
- `RiskConfig`: risco por operacao padrao 0,5%, teto 1%.
