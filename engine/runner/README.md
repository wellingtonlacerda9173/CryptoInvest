# engine/runner

Pontos de entrada do processo: `backtest.py` (CLI do backtest, ver o README da raiz), `candle_source.py` (carga de
velas: Timescale, cache local e Binance publica) e o runner do paper (`paper.py` CLI; `paper_robot.py` o robo;
`paper_journal.py`/`paper_outbox.py` persistencia duravel com outbox; `paper_control.py` comandos do operador;
`paper_lock.py` instancia unica; `paper_report*.py` relatorios e trava do Live). Ver "Paper trading" no README da raiz.

Fase 4 (painel): `command_channel.py` consome os comandos ASSINADOS (HMAC, carimbo, nonce) da API pelo Redis e os grava na
`CommandQueue` (as protecoes de sempre valem); `paper_versioned.py` le a ultima configuracao versionada (`config_versions`) no
`start`; `backtest_job.py` e o worker de backtest em subprocesso. Alertas externos opcionais: `engine/infrastructure/notifications.py`.
`signing.py` assina tudo que cruza o Redis (acks e eventos, alem dos comandos); a entrega dos comandos e at-least-once
(`LMOVE` para `commands:processing`, nonce so depois da gravacao local) e o resultado de cada comando e explicito
(`applied|partially_applied|refused|failed`; `apply-config` aplica reducoes por campo). Detalhes e contratos em `api/README.md`.

Fase 5 (seletor): `validate.py` e a validacao fora da amostra (todas as estrategias do registry x pares x timeframes,
seletor em walk-forward 180/60, referencias nulas, correcao de multiplas comparacoes sobre a familia inteira;
relatorios em `reports/validation/`, git-ignored; `--end` fixo e sementes fixas => reproduzivel). `--publish-scores`
grava os scores do backtest em `strategy_scores` (migracao 0011, append-only) so nas celulas em que o seletor passou
(`--allow-unvalidated` ignora o veredito) e salva as amostras em `data/selector/`. O seletor no pipeline e opt-in
(`selector.enabled`, padrao desligado): `PaperRunConfig.selector` / `paper start --selector` e
`BacktestEngine(selector=..., selector_history=...)`. Sem scores salvos o robo NAO opera (decisao `selector_no_scores`);
regime sem operacao ou sem estrategia elegivel gera a decisao `selector_idle` com o motivo em pt-BR; scores sao
recalculados a cada `recalc_hours` a partir dos trades do diario (rotulados pelo regime da decisao de entrada) mais as
amostras de backtest importadas. `selector_store.py` e a porta de scores do runner.

Fase 6b (licoes e valor da IA): `python -m engine.runner.lessons [--no-ai] [--period-days 7]` reavalia os scores com o
seletor deterministico (snapshot novo em `strategy_scores`, origem paper|live) e grava um relatorio PENDENTE em
`ai_lessons` (migracao 0013, append-only; a aprovacao e a tabela filha `ai_lesson_reviews`, tambem append-only, so admin
via API). Sem IA/orcamento o relatorio e so estatistico. O robo agenda o job (`paper start --lessons-hours 168`, 0 desliga)
em segundo plano e so injeta no prompt do analista (`analyst-v2`, campo `lessons`, sanitizado e limitado) as licoes cuja
revisao mais recente e `approved`. `python -m engine.runner.ai_value [--days 30]` compara o que os vetos teriam rendido
(simulacao pessimista de `hypothetical.py`, dimensionamento real) com o executado, sem chamar a IA.

Fase 6 (robustez da IA): o parecer da IA e pedido FORA do lock do robo (`_AiTicket` coletado sob o lock, consulta sem lock,
reentrada com revalidacao do gate; cancelamento nunca readquire o lock e uma parada em curso descarta a entrada). A identidade
do run (`config_hash`) NAO inclui a secao `ai` (ligar/desligar, modelo, orcamento ou prazo nao impedem retomar um run com
posicao aberta; a IA acompanha a versao nova a cada start); `ConfigDocument.identity_digest()` fixa o formato legado e
`digest()` (cadeia `config_versions`) cobre o documento inteiro. O teto mensal vale entre processos: o gasto e lido do livro
`ai_calls` a cada chamada e o custo de pior caso e RESERVADO por INSERT atomico (migracao 0014: linha `reserve` + linha de
liquidacao com `reservation_id`, sem UPDATE; lock consultivo no Postgres). Chamada cancelada no meio fica como `cancelled`.

Smoke manual ANTES de ligar a IA (nao automatizado; usa a API real e custa centavos):
1. `uv sync --extra ai`; chave em `secrets/anthropic_api_key`; `ai.monthly_budget_usd` baixo (ex.: 1) no painel.
2. `python -m engine.runner.lessons --period-days 7` (usa o mesmo analista/livro) e confira `ai_calls`: linhas `reserve` e
   uma liquidacao `ok` por chamada, sem `error`/`invalid`.
3. Em `paper start` com `ai.enabled`, veja no log `ai_effort_unsupported` (modelo recusou `output_config.effort`: o cliente
   reenviou sem o parametro e lembra por modelo; aparece em `client_notes` do status da IA). Se aparecer `invalid`/`error`
   em serie, o circuito abre: corrija antes de ligar.
4. Confirme: gasto do mes (`/api/ai/usage`) <= teto; nenhum `reserve` sem liquidacao apos encerrar o robo com SIGTERM.
