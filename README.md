# CryptoInvest

Robo de trading de criptomoedas (Binance Spot, somente long, sem alavancagem). Python 3.12, asyncio.

Fase 1: monorepo, dominio, dados de mercado da Binance (sem chave) e persistencia (PostgreSQL + TimescaleDB).

## Estrutura

| Caminho | Funcao |
|---|---|
| `engine/domain` | Modelos Pydantic imutaveis (Candle, Signal, Order, Position, Trade, Decision, RiskConfig), regras puras e Protocol `ExchangeGateway` |
| `engine/application` | Detectores, indicadores, estrategias, gestao de risco e `backtest` (motor, metricas, walk-forward, relatorios) |
| `engine/runner` | Pontos de entrada: `python -m engine.runner.backtest` (CLI do backtest) e carga de velas |
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
- Taxa de entrada: `max_entry_fee_multiple` padrao 1 (taxa real <= taxa taker configurada), para que a perda no stop
  nao ultrapasse o risco aprovado. Desconto de taxa (ex.: BNB) e folga; taxa real acima da taker configurada gera
  `EmergencyExit`.

## Backtest, metricas e walk-forward

Roda o mesmo pipeline do Paper/Live (estrategia -> `evaluate_entry` -> ordem no `BacktestGateway` -> `open_position`
-> `manage_position` -> `RiskState`) vela a vela, sem olhar o futuro: a decisao na vela i so ve velas <= i e a ordem
executa a partir da vela i + latencia.

```powershell
uv run python -m engine.runner.backtest --symbol BTC/USDT --timeframe 1h --years 2 --strategies elephant,ema --walk-forward
```

Exemplo completo (as premissas do relatorio de exemplo): `--symbol BTC/USDT,ETH/USDT --years 2 --capital 10000
--taker-fee 0.001 --maker-fee 0.001 --slippage-bps 5 --walk-forward --end 2026-10-18T00:00:00+00:00`.

| Opcao | Padrao | Funcao |
|---|---|---|
| `--symbol`, `--timeframe`, `--years`, `--end` | BTC/USDT, 1h, 2, agora | Dados (varios pares separados por virgula) |
| `--strategies` | `elephant,ema` | `elephant` = barra elefante + rompimento; `ema` = cruzamento de EMAs |
| `--params` | (padrao das estrategias) | Parametros fixos: `min_confidence=0.5,rsi_min=50,reward_risk=2` |
| `--capital`, `--taker-fee`, `--maker-fee`, `--slippage-bps`, `--latency` | 10000, 0.001, 0.001, 5, 1 | Premissas de execucao (latencia em velas) |
| `--tick-size`, `--step-size`, `--min-notional` | tabela BTC/ETH | Filtros da corretora (informe os reais para outros pares) |
| `--walk-forward` | desligado | Janelas rolantes treino/teste; `--train-days 180 --test-days 60 [--step-days]` |
| `--grid` | `min_confidence=0.5,0.6;rsi_min=45,50;reward_risk=2,3` | Grade pequena escolhida SO no treino |
| `--min-train-trades` | 5 | Minimo de trades no treino para um conjunto de parametros ser elegivel |
| `--out`, `--cache-dir` | `reports`, `data/cache` | Saidas e cache local de velas (ambos ignorados pelo git) |
| `--no-db`, `--refresh` | | Nao usa o Timescale / ignora o cache |

Origem das velas: Timescale (se acessivel) + cache local (`data/cache/*.csv`); o que faltar vem da Binance publica
(sem chave) e e persistido de volta no banco e no cache. Buracos nos dados: usa-se o maior trecho continuo e o
relatorio avisa. Saidas em `reports/<PAR>_<tf>_<estrategias>/`: `report.md` e `report.json` (parametros, versao git,
dados, metricas, operacoes evitadas), `trades.csv` (cada saida), `roundtrips.csv` (posicao completa, resultado em R),
`equity.csv` (curva marcada a mercado), `decisions.csv` (cada sinal: operou/nao operou, motivo e resultado
hipotetico) e, com walk-forward, `walk_forward.md/json`, `walk_forward_windows.csv`, `oos_equity.csv`.

Metricas (prioridade: drawdown e consistencia, depois lucro): drawdown maximo da curva marcada a mercado, meses
positivos, retorno, taxa de acerto, profit factor, payoff, expectativa em R, Sharpe (retornos diarios UTC, anualizado
por raiz de 365, sem taxa livre de risco), tempo parado, resultado por mes e por estrategia.

Operacoes evitadas: todo sinal nao operado (rejeitado pelo gate, trava, pausa, posicao ja aberta...) e simulado ate
alvo/stop/limite de tempo (72 velas) sem afetar o capital, com o mesmo dimensionamento do gate (risco por operacao e
teto de posicao; poeira = nao simulado). Cada sinal e simulado de forma independente: os totais sao a soma de
simulacoes independentes, nao o efeito sobre o capital. O relatorio separa perda evitada de ganho perdido e os vetos em
tres grupos: capacidade (ja ha posicao/ordem), travas de estado (halt, pausas, meta diaria) e filtros reais de
mercado/risco; so os filtros compoem o "efeito liquido dos vetos de risco/filtros". O efeito do halt aparece em linha
propria, com a data. Se o robo foi desligado, o relatorio traz tambem as metricas so do trecho ativo (antes do halt).

Walk-forward: em cada janela os parametros sao escolhidos so no treino (menor drawdown em faixas de 0,5 p.p.,
depois mais meses positivos, depois maior retorno; exige `--min-train-trades` e retorno positivo) e avaliados na
janela de teste seguinte, com capital reiniciado; o agregado fora da amostra encadeia os retornos. Cada janela
reinicia o `RiskState` (equivale a uma reativacao manual do robo a cada janela; no live o halt por drawdown e
permanente). O veredito tem quatro categorias: `inconclusivo (amostra pequena)` (menos de 30 trades fora da amostra),
`sem edge detectavel fora da amostra` (sem vantagem nem no treino), `indicios de sobreajuste` (so com amostra
suficiente e treino muito acima do teste) e `consistente`. Sem janela completa (periodo menor que treino + teste), o CLI
avisa e encerra com codigo 1 depois de gravar o relatorio do backtest.

`--end` sem fuso e interpretado como UTC (nunca o fuso da maquina); o periodo resolvido vai no
`report.json` (sem caminhos absolutos). Saidas de stop com lucro aparecem como `trailing_stop`/`breakeven_stop` no
relatorio (rotulo so de relatorio).

Premissas e limites: entrada por ordem limite no fechamento do sinal (valida por 2 velas: o limite e cancelado antes de
qualquer vela que abra apos a validade); fill por toque, ao preco limite (otimista quanto a fila, pessimista quanto a
melhora de preco); na mesma vela o stop vem antes do alvo; sem spread/livro simulados; filtros da corretora fixos (tabela BTC/ETH); sem calendario de eventos;
posicoes abertas no fim dos dados nao viram trade (ficam na curva marcada a mercado); tendencia de timeframe superior
nao e alimentada (`htf_trend` ausente).
