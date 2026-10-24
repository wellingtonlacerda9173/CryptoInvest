# CryptoInvest

Robo de trading de criptomoedas (Binance Spot, somente long, sem alavancagem). Python 3.12, asyncio.

Fase 1: monorepo, dominio, dados de mercado da Binance (sem chave) e persistencia (PostgreSQL + TimescaleDB).

## Estrutura

| Caminho | Funcao |
|---|---|
| `engine/domain` | Modelos Pydantic imutaveis (Candle, Signal, Order, Position, Trade, Decision, RiskConfig), regras puras e Protocol `ExchangeGateway` |
| `engine/application` | Detectores, indicadores, estrategias, gestao de risco e `backtest` (motor, metricas, walk-forward, relatorios) |
| `engine/runner` | Pontos de entrada: `python -m engine.runner.backtest` (CLI do backtest), `python -m engine.runner.paper` (runner do paper) e carga de velas |
| `engine/infrastructure` | Cliente Binance (REST paginado + WebSocket), persistencia SQLAlchemy/Alembic, configuracao |
| `api` | API do painel (FastAPI): autenticacao 2FA, auditoria, usuarios, chaves e contrato OpenAPI (ver `api/README.md`) |
| `web` | Painel (em construcao) |
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

## Paper trading (runner)

Processo SEPARADO da API que roda, ao vivo e vela a vela, o MESMO codigo de estrategia e risco do backtest
(`evaluate_entry` -> ordem no `PaperGateway` -> `open_position` -> `manage_position` -> `RiskState`) sobre dados
publicos da Binance (sem chave). Nenhuma ordem e enviada a corretora: a execucao e simulada (`PaperGateway`).

```powershell
./scripts/dev.ps1 up && ./scripts/dev.ps1 migrate
uv run python -m engine.runner.paper start --allow-open-calendar          # BTC/USDT,ETH/USDT, 1h, capital 10000
uv run python -m engine.runner.paper status
uv run python -m engine.runner.paper pause  --reason "manutencao"
uv run python -m engine.runner.paper resume --operator ana --reason "ok"   # operador e motivo obrigatorios
uv run python -m engine.runner.paper stop                                  # parada ordenada (tambem Ctrl+C / SIGTERM)
uv run python -m engine.runner.paper emergency-stop --reason "incidente"   # com --liquidate: --operator e --reason obrigatorios
uv run python -m engine.runner.paper reactivate --operator ana --reason "drawdown revisado"
uv run python -m engine.runner.paper report [--date 2026-10-19] [--no-compare]
```

| Opcao do `start` | Padrao | Funcao |
|---|---|---|
| `--symbols`, `--timeframe`, `--capital`, `--strategies`, `--params` | BTC/USDT,ETH/USDT, 1h, 10000, elephant,ema | Universo e estrategias (mesmos nomes do backtest) |
| `--run-id` | `paper-<AAAAMMDD-HHMMSS>` | Run estavel: reiniciar com o MESMO id recupera o estado (posicoes, stops, risco, halt). Config diferente com o mesmo id e recusada |
| `--ws-url` | `PAPER_WS_URL` = `wss://stream.binance.com:443` | WebSocket da Binance. A porta 9443 (padrao do `BINANCE_WS_URL`) da timeout em algumas redes; a 443 e o mesmo servico |
| `--calendar` | `config/volatility_events.json` | Calendario de eventos. Sem cobertura valida toda entrada e recusada (`event_calendar_stale`) |
| `--allow-open-calendar` | desligado | PAPER sem calendario: aviso estruturado `CALENDAR_OPEN_PAPER_MODE` no inicio e em CADA decisao. O run fica **INELEGIVEL para o Live para sempre**: basta UM `paper_start` do `audit_log` ou UMA decisao com `calendar_open` (reiniciar sem a flag nao "lava" o historico); o relatorio mostra `INELEGIVEL: calendario aberto`, nunca `APROVADO` |
| `--taker-fee`, `--maker-fee`, `--slippage-bps`, `--latency-ms` | 0.001, 0.001, 5, 100 | Premissas do `PaperGateway` (`taker-fee` tambem alimenta gate e estrategias) |
| `--reconcile-minutes`, `--equity-seconds`, `--heartbeat-seconds`, `--max-stop-blind-seconds` | 5, 60, 30, 30 | Reconciliacao, foto de equity, heartbeat e limite de dados velhos com stop ativo |
| `--duration-minutes`, `--no-redis`, `--emergency-liquidate` | | Parada automatica (smoke), sem Redis, `emergency-stop` sempre liquida |

Comandos do operador (`pause`, `resume`, `stop`, `emergency-stop`, `reactivate`) sao arquivos atomicos em
`data/paper/<run_id>/commands/` (diretorio 0700 onde o SO permite) lidos pelo robo a cada segundo, sem depender de
rede. `reactivate` e o UNICO caminho para religar o robo desligado por drawdown (5%), e `resume` e o caminho para sair
de uma pausa (divergencia de reconciliacao, `emergency-stop`); ambos gravam operador e motivo em `control_events` e
`audit_log`. `reactivate` e `emergency-stop --liquidate` exigem `--operator` e `--reason`, e o `audit_log` guarda o
SHA-256 do comando. Travas do canal: um comando expira em 10 min (vai para `commands/expired/`), os emitidos ANTES
da partida do processo sao descartados (so o `emergency-stop`, que apenas reduz risco, vale sempre), e um comando
que falha nao e arquivado como processado: segue pendente, alerta e e rejeitado na 3a falha deterministica.
**Este canal nao autentica ninguem** (quem escreve no diretorio controla o robo); a API da Fase 4 traz
autenticacao/2FA e autorizacao por papel. `data/paper/<run_id>/status.json` traz o heartbeat e o estado (`status` o
le); um leitor que prenda o arquivo (Windows) nao derruba o robo.

Instancia unica: `start` toma um lock de arquivo (`data/paper/<run_id>/robot.lock`, mesma maquina) e advisory locks
no Postgres por run e por simbolo (qualquer maquina); o segundo robo falha com mensagem clara. Um run NOVO herda o
HALT do run de paper anterior ainda desligado por drawdown (nasce `HALTED`; so `reactivate` religa).

Diario (Timescale, sempre na MESMA transacao do estado do gateway e do risco): decisoes (toda vela avaliada gera
uma: operou/nao operou, codigo e motivo; append-only), ordens, fills, posicoes, trades, `risk_state`,
`gateway_state` (compacto), `equity_snapshots` (a cada minuto, a cada vela e a cada trade — tambem sao a prova de
"dias de operacao") e `control_events`.

Eventos em Redis (pub/sub, JSON, canais `cryptoinvest:paper:<tipo>`): `decisions`, `orders`, `trades`, `state`
(RUNNING/PAUSED/HALTED/STOPPED, `stops_blind`, travas), `equity`, `heartbeat`, `alerts`, `risk`. Redis fora do ar =
degradacao (fila em segundo plano; o robo nunca espera) e log `redis_unavailable`.

Seguranca operacional:

- `cancel_order` e usado so em ordens de entrada pendentes e na parcial; o OCO (stop) NUNCA e cancelado (o ponto
  unico de cancelamento levanta `RunnerInvariantError` para ids `:oco`, e os testes espiam o gateway).
- Dados de mercado parados (`stops_blind`): pausa novas entradas, alerta, reconecta o stream com backoff; com posicao
  aberta por mais de `blind_alert_seconds` (5 min) escala (`STOPS_BLIND_PROLONGED`). No paper o stop so executa com dados.
- Reconciliacao periodica (e na partida, antes de aceitar sinais): `reconcile` do gateway x posicoes do diario x
  memoria; posicao sem stop ativo, quantidade diferente ou stop do gateway MENOR que o persistido => PAUSA, alerta e
  registro. Nunca "corrige" reduzindo stop.
- Banco fora (`OSError`, `TimeoutError`, erro de conexao do driver): a gravacao e retentada com backoff (e
  idempotente). Se persiste, o evento NUNCA e descartado: vai para o outbox duravel
  `data/paper/<run_id>/outbox/` (fsync + rename), o robo entra em `FAILED_PERSISTENCE` (pausado, alerta
  `persistence_down`, entradas bloqueadas, stops seguem no gateway) e a cada ciclo tenta reaplicar o outbox em
  ordem; ao voltar, retoma sozinho SO a pausa causada pela queda. Na partida o outbox e reaplicado ANTES de
  recuperar o estado (se o banco ainda esta fora, o `start` recusa). So o iterador do feed e supervisionado como
  "feed caiu"; erro de gravacao nunca e engolido como tal e `_seen_seq` so avanca depois do evento gravado.
- Saida e HALT atomicos: o trade, o fill, as ordens (inclusive o OCO encerrado numa venda a mercado), a posicao, o
  `risk_state`, o `gateway_state` e o evento HALT do trade vao na MESMA transacao; ordens de saida/cancelamento
  gravam a linha e o estado do gateway juntos, e ids de saida ja usados (mesmo orfaos) nunca sao reutilizados.
- Desligamento limpo (Ctrl+C, SIGTERM, Ctrl+Break, `stop`): sem novas entradas, entradas pendentes canceladas,
  `gateway_state`/`risk_state` salvos; posicoes abertas continuam protegidas no estado salvo. Falha fatal (diario
  indisponivel, invariante): NAO grava estado possivelmente incoerente; vale o ultimo estado atomico.
- Recuperacao apos queda: restaura o `PaperGateway` do ultimo `gateway_state`, reconstroi posicoes (diario) e estado do
  runner, cancela entradas pendentes (a aprovacao do gate nao sobrevive ao processo), reprocessa as velas perdidas
  (`fetch_candles` desde a ultima vela tratada; stops ultrapassados executam; nenhuma entrada nova em vela reprocessada)
  e reconcilia ANTES de aceitar sinais. Halt e pausa persistem.

`report` grava em `reports/paper/<run_id>/`: `daily-AAAA-MM-DD.{json,md}` e `cumulative.{json,md}` com metricas
(`compute_paper_metrics`), dias de operacao, criterios do Live (`evaluate_live_gate`), comparacao paper x backtest nos
MESMOS candles do periodo (`compare_paper_backtest`) e notas de honestidade (parado em USDT e resultado valido).
Um run que usou `--allow-open-calendar` em qualquer momento sai `INELEGIVEL: calendario aberto` (criterio
`eligible_for_live` reprovado) no JSON, no Markdown e no CLI.

Limites conhecidos: o book e a fila de ordens sao simulados (sem impacto de mercado proprio); a tendencia de timeframe
superior (`htf_trend`) nao e alimentada; filtros da corretora sao a tabela fixa BTC/ETH; um unico processo por run.
