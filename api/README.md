# api

API HTTP do painel (FastAPI), processo SEPARADO do runner. Entrega seguranca, auditoria, usuarios, chaves da
Binance, leituras de trading, controle do robo (comandos assinados), configuracoes versionadas, backtests em
subprocesso, onboarding, WebSocket em tempo real e o contrato (`api/openapi.json` + `api/ws_schema.json`).
**A API nao contem logica de trading**: le o que o runner publica e envia COMANDOS assinados que o runner verifica.

## Execucao

```powershell
uv run python -m api.cli init-secrets                 # dev: gera os segredos que faltarem, inclusive command_hmac_key (nunca sobrescreve)
uv run alembic upgrade head                           # ate a 0009 (raiz unica/FK em config_versions, resultados partially_applied|refused)
uv run python -m api.cli create-admin --username ana.silva    # primeiro admin (senha pedida no terminal)
uv run python -m api.serve --port 8000                # = uvicorn api.app:create_default_app --factory --ws-max-size 8192, UM processo
```

Um unico processo da API (single-worker): a bomba de comandos, o hub de tempo real e os subprocessos de backtest assumem
um dono. Com `uvicorn` direto, passe `--ws-max-size 8192` (o servidor recusa a mensagem do WebSocket grande demais NA LEITURA).

Em dev sem HTTPS defina `API_COOKIE_SECURE=false` (localhost aceita `Secure` em Chrome/Firefox, mas curl nao).
O 2FA e configurado no primeiro login (QR + 10 codigos de recuperacao) e EXIGE o token de ativacao (abaixo).

Contrato: `uv run python -m api.export_openapi` regrava `api/openapi.json`; um teste falha se estiver desatualizado.

## Endpoints

| Tag | Rotas | Quem |
|---|---|---|
| health | `GET /health`, `GET /ready` | publico |
| auth | `POST /auth/login`, `POST /auth/2fa/verify`, `POST /auth/2fa/enroll`, `POST /auth/2fa/enroll/confirm` (2o passo, sessao pendente) | publico / pendente |
| auth | `GET /auth/me`, `POST /auth/logout`, `POST /auth/reauth`, `POST /auth/password`, `POST /auth/2fa/recovery-codes` (reauth recente) | admin e leitor |
| sessions | `GET /sessions`, `DELETE /sessions/{id}`, `DELETE /sessions` | admin e leitor (so as proprias) |
| users | `GET/POST /users`, `PATCH /users/{id}`, `POST /users/{id}/reset-two-factor`, `POST /users/{id}/unlock` (todas as mutacoes com reauth recente) | admin |
| audit | `GET /audit` (keyset, `limit`, `cursor`, `actor`, `action`) | admin |
| credentials | `GET /credentials`, `POST` / `PUT /{id}` / `DELETE /{id}` (reauth recente), `POST /{id}/verify` | admin |
| trading | `GET /status`, `/risk/limits`, `/account/summary`, `/equity`, `/positions`, `/trades` (+ `/trades/export` CSV), `/decisions`, `/performance`, `/live-gate`, `/market/candles`, `/glossary` | admin e leitor |
| control | `POST /control/pause`, `/resume`, `/stop`, `/emergency-stop`, `/power` | admin |
| control | `POST /control/reactivate` (reauth recente), `POST /live/activate` (reauth recente; **sempre 409**) | admin |
| control | `GET /control/commands`, `GET /control/commands/{id}` (estado de entrega) | admin e leitor |
| settings | `GET /settings`, `GET /settings/history` | admin e leitor |
| settings | `POST /settings/validate` (nao salva), `PUT /settings/{pairs\|strategies\|risk\|ai}` (reauth recente) | admin |
| backtest | `GET /backtests`, `GET /backtests/{id}`, `GET /backtests/{id}/report` | admin e leitor |
| backtest | `POST /backtests` (202), `POST /backtests/{id}/cancel` | admin |
| onboarding | `GET /onboarding`; `POST /onboarding/start-paper` | leitor le; admin inicia |
| tempo real | `WS /ws` (cookie de sessao + `Origin` do painel); contrato em `api/ws_schema.json` | admin e leitor |

Rotas novas: leitura com `UserDep`, mutacao com `AdminDep`/`FreshAdminDep` (`api/deps.py`), mutacoes auditadas com
`AuditService.record(...)` na mesma transacao. `tests/unit/api/test_route_policy.py` e `tests/integration/api/test_authorization.py`
falham para rota sem autorizacao explicita.

## Trading, controle e tempo real (esta fase)

### Leituras (`api/services/trading.py`, `status.py`, `livegate.py`)

- Valores monetarios/percentuais saem como numeros para EXIBICAO (o painel nao faz conta); o CSV de operacoes tem os
  decimais exatos, o modo no nome (`operacoes-paper-AAAAMMDD.csv`) e no cabecalho, e neutraliza celulas que parecem formula.
- Status: estado + palavra simples, `connection` (`live`/`stale`/`offline`/`unknown`: o painel nunca mostra dado velho como
  atual), `pause_reasons`, `stops_blind`, `outbox_pending`, travas "valor atual de limite" (`limits`), run, elegibilidade ao
  Live e comandos pendentes. Vem do ultimo heartbeat no Redis (`<prefixo>:last:heartbeat|state`), com `status.json`
  (`API_RUNNER_DATA_DIR`) e o diario como reservas. Motivos de decisao e glossario em pt-BR: `api/reasons.py`.
- Listas por cursor (`next_cursor`), totais sempre de UM modo (paper/live nunca se misturam).
- Trava do Live (`GET /live-gate`): criterios valor x meta (`met`/`failed`/`in_progress`), `eligible_for_live`
  (calendario aberto = inelegivel para sempre). `POST /live/activate` responde **409 "Live ainda nao disponivel"** e nunca muda o
  modo (Fase 7); a tentativa e auditada (`live.activation.refused`).

### Comandos ao robo (`api/services/commands.py`, `engine/runner/command_channel.py`)

Fluxo: rota -> linha em `control_commands` (estado visivel) + auditoria -> entrega assim que o robo da sinal de vida.

- Envelope Redis (`RPUSH <prefixo>:commands`): `{"payload": {v, id, ts, name, operator, reason, liquidate, issued_at, params},
  "sig": hex(HMAC-SHA256(command_hmac_key, json canonico do payload))}`. `command_hmac_key` e um arquivo secreto
  compartilhado (mesmo `SECRETS_DIR` da API e do runner, >= 32 bytes; `python -m api.cli init-secrets`).
- O runner so aceita se: assinatura confere (tempo constante), `ts` a <= 60 s do relogio local, `id` (nonce) inedito
  (memoria + `SET NX` no Redis, sobrevive a reinicio) e corpo valido. Senao DESCARTA e audita (`paper_command_discarded`:
  `bad_signature|stale|future|replay|malformed|too_large|invalid_command`, nunca o segredo). O aceito entra na `CommandQueue`
  em arquivo: valem TODAS as protecoes antigas (TTL 10 min, descarte pre-partida, `emergency-stop` simples nunca expira,
  `liquidate` 30 min, `resume` nunca reativa HALTED, nunca remove stop, nunca cancela OCO). O CLI por arquivo segue funcionando.
- Entrega at-least-once: o runner move cada envelope (`LMOVE`) de `<prefixo>:commands` para `<prefixo>:commands:processing`
  e so o remove depois de gravar o comando na fila local duravel (arquivo `<instante>-<nome>~<id>.json`, idempotente por
  id); SO ENTAO reivindica o nonce (`SET NX`) e confirma `received`. Se o runner cair no meio, o envelope reaparece no
  proximo poll. Verificacao TOTAL por item: assinatura nao-ASCII, JSON aninhado demais (> 8 niveis), > 8 KiB ou lixo viram
  descarte (nunca excecao), e um envelope-veneno nao derruba o lote. Fila acima de `MAX_QUEUE_LENGTH` (200) e drenada ate o
  teto a cada ciclo; descartes auditados ficam limitados a 5/minuto (o excedente sai agregado em
  `paper_command_discard_burst`).
- **Redis e fronteira fraca: tudo que cruza e assinado** (`engine/runner/signing.py`, mesma `command_hmac_key`, sub-chave por
  dominio): confirmacoes `cmd:ack:<id>` (`{result, detail, at, sig}`, dominio `ack`, amarrada ao id) e as mensagens de
  estado/heartbeat/equity (e os demais canais) publicadas pelo runner (`{kind, payload, sig}`, dominio `event`, amarrada ao
  tipo). A API (com a chave) DESCARTA o que nao tem assinatura valida e registra o alerta `runner_message_rejected` (1 log/min
  com a contagem; `RedisRunnerLink.rejections.total`). Sem a chave na API o modo antigo (JSON puro) segue valendo. Heartbeat
  com `heartbeat_at` mais de 30 s no futuro e SUSPEITO: nao conta como sinal de vida (nem idade 0) e e logado.
- Redis com senha: arquivo `redis_password` em `SECRETS_DIR` (API e runner o leem sozinhos; a URL nao leva a senha). Sem o
  arquivo nada muda. `docker compose -f docker-compose.dev.yml --profile redis-auth up -d redis-auth` sobe um Redis com
  `requirepass` desse secret (opcional; pare o `redis` padrao antes, mesma porta). Mantenha o Redis em rede restrita
  (loopback/VLAN do runner e da API): a assinatura impede forjar dados, nao impede negar servico a quem alcanca a porta.
- Resultado do comando (confirmacao do runner -> estado do comando): `received|applied|partially_applied|refused|failed|discarded`.
  Estados: `pending_delivery` -> `delivered` -> **`applied`** (aplicou) | **`partially_applied`** (aplicou parte; o resto fica
  para o proximo inicio, com o detalhe) | **`refused`** (o runner recusou: resume em HALTED, reactivate sem drawdown,
  apply-config que so aumenta risco; NUNCA vira `applied`; `detail` traz o motivo) | `failed` | `discarded` | `expired`.
  Canal de arquivo: a API le as pastas `processed|failed|expired|rejected` da `CommandQueue` (o resultado de `processed` vai em
  `processed/<arquivo>.result`) e resolve o estado do mesmo jeito.
- Parada de emergencia descartada por relogio (`stale`/`future`) NAO e terminal: a API reenvia com NOVO id/carimbo
  (`<id>-<tentativa>`, espera de 5 s x tentativa ate 60 s) ate receber um resultado do runner (o runner trata parada repetida
  como no-op); sem confirmacao em 90 s tambem reenvia, sem limite de tentativas. Outros comandos descartados continuam
  `discarded`.
- Sem Redis ou sem robo (heartbeat velho/parado): o comando FICA em `pending_delivery` no banco (sobrevive a reinicio da API),
  com o motivo da espera; expira no TTL (10 min; vender tudo 30 min), exceto a parada de emergencia simples, entregue
  assim que possivel e reenviada ate confirmar. Comando pedido antes de o robo reiniciar expira (o runner o descartaria).
  Reserva por arquivo: com `API_RUNNER_DATA_DIR` e o Redis recusando, a API grava na `CommandQueue` do run.
- `power on`: retoma se pausado; se o robo esta desligado, so inicia o servico com `API_RUNNER_START_COMMAND`
  (sem ele responde `waiting_runner` com instrucoes). `DELETE /credentials/{id}` e recusado (409) com o robo ligado.

### Configuracao versionada (`api/services/settings_service.py`, `engine/application/trading_config/`)

- Documento unico (`pairs`, `strategies`, `risk`, `ai`) em `config_versions` (migracao 0007): append-only (trigger),
  encadeado por hash, com ator, motivo, diff e hash. `PUT /settings/{secao}` mescla sobre a versao atual (campo omitido
  mantem o valor; chave desconhecida e erro, nunca vira padrao em silencio) e exige reauth recente.
- Os tetos rigidos (`RiskConfig`: 1% por operacao, 5% perda diaria/drawdown, 2 posicoes, 2 perdas seguidas, pausa >= 24 h,
  exposicao <= 80%, retorno/risco >= 1:2) sao impostos pelo SERVIDOR: acima deles, 422 com o erro por campo.
- `POST /settings/validate` nao salva e devolve, por campo, `antes/agora/custo por perda` e `increases_risk`.
- **Quando vale**: reducao de risco vale na hora SE o robo esta ligado e reporta a configuracao em vigor: a API compara com o
  `risk` que o PROPRIO runner reporta (nao com a ultima versao salva); campo que subiu em relacao ao robo, ou robo
  desligado/sem sinal/sem `risk`, vale so no proximo inicio e nao gera comando (a tela diz isso). O comando `apply-config`
  assinado leva so as reducoes; o runner REVALIDA e aplica POR CAMPO (reducoes valem, aumentos sao recusados; campo
  desconhecido ou valor invalido recusa o pacote): resultado `applied`, `partially_applied` ou `refused` (acima). A versao
  mostra `application` (`none|pending|applied|partially_applied|refused|expired|failed`) derivada do comando; versoes sao
  imutaveis, entao o estado nao e gravado nelas. Reducao cujo comando expirou/foi recusado aparece como `expired`/`refused`
  e e reenviada na proxima gravacao (o patch e sempre robo-efetivo x documento salvo). `expected_version` e OBRIGATORIO
  no `PUT` (perda de atualizacao por campo vira 409). Piso de `risk_per_trade`: 0,0001. Aumento (ou efeito que nao da para medir: estrategias, taxas, trailing...) exige `acknowledge_risk_increase`
  (segundo clique) e so vale no PROXIMO (re)inicio. O runner le a ultima versao no `start`, grava `config_source`
  (versao, hash, documento efetivo) no `paper_start` e inclui o hash do documento no `config_hash` do run. Retomando um run
  com posicao aberta, pares/estrategias ficam como estavam e o risco so aceita reducoes (`effective_document`).
  `--no-versioned-config` volta a usar so a CLI.

### Backtests (`api/services/backtests.py`, `engine/runner/backtest_job.py`)

`POST /backtests` grava o job (`backtest_jobs`) e dispara `python -m engine.runner.backtest_job` em SUBPROCESSO (fora do
event loop): `API_BACKTEST_MAX_CONCURRENT` (1), `API_BACKTEST_TIMEOUT_SECONDS` (1800, mata o processo),
`API_BACKTEST_MAX_MEMORY_MB` (2048, vigia dentro do worker), cancelavel. Progresso em tabela; relatorio com metricas,
curva, operacoes, operacoes evitadas e walk-forward (veredito em linguagem simples). Apos reinicio da API, jobs "rodando"
viram `failed` (salvo se o DONO, outro processo da API, ainda renova o sinal de vida `owner_beat` em `progress`) e os "na fila"
voltam a fila; single-worker (ver Execucao). Parametros da grade com faixa (`min_confidence` 0-1, `rsi_min` 0-99,
`reward_risk` 2-10), estrategias/pares repetidos e `start` antes de 2017-08-17 sao 422. Dados: Timescale/cache/Binance (`API_BACKTEST_USE_DB`, `API_BACKTEST_CACHE_DIR`).

### WebSocket `/ws` (`api/routers/ws.py`, `api/services/realtime.py`, `api/ws_schema.py`)

Handshake: cookie de sessao verificado (senha + 2FA), `Origin` obrigatorio e igual a um de `API_PANEL_ORIGINS`, papel
`admin|reader`, no maximo `API_WS_MAX_CONNECTIONS_PER_USER` (5) conexoes por usuario; a sessao e revalidada a cada
`API_WS_SESSION_CHECK_SECONDS` SEM renovar (revogada/vencida/usuario desativado = fecha 1008). Recusas: 1008.

Servidor -> cliente (`type`): `hello` (epoca, canais, `ping_seconds`, `runner_connected`, `role`), `subscribed`, `event`
(`id` = `epoca:seq`, `channel`, `at`, `data`), `runner` (`connected` + texto "Desconectado do robo..."), `resync`
(refaca a leitura por REST), `ping`, `error`. Cliente -> servidor: `subscribe` (`channels`, `last_event_id`), `unsubscribe`,
`pong` (a conexao sem nenhuma mensagem em 3 pings cai). Canais: `status` (runner `state`), `decisions`, `orders`, `trades`,
`equity`, `alerts` (runner `alerts` + `risk`), `heartbeat`; tambem por `?channels=a,b`. O canal e SOMENTE LEITURA.

Backpressure: fila limitada por cliente (`API_WS_QUEUE_SIZE`); cheia, descarta as mais antigas e envia `resync`; passando
de `API_WS_MAX_DROPPED_EVENTS` (ou 5 s sem conseguir enviar) fecha com 1013, sem atrasar os outros. Retomada: anel de
`API_WS_REPLAY_BUFFER` eventos; `last_event_id` da mesma epoca repete o que faltou (sem duplicar o que o tempo real ja
entregou: cada conexao lembra os ids enviados), senao `resync`. Teto GLOBAL de 200 conexoes (`MAX_TOTAL_CONNECTIONS`); mensagem
do cliente acima de 4 KiB fecha com 1009 (e `--ws-max-size` no servidor).

### Notificacoes externas (`engine/infrastructure/notifications.py`)

Telegram e SMTP, DESLIGADOS por padrao (`NOTIFY_ENABLED=true` + canal completo). Credenciais so de arquivo em
`SECRETS_DIR` (`telegram_bot_token`, `smtp_password`); o resto em `NOTIFY_*` (`TELEGRAM_CHAT_ID`, `SMTP_HOST`, `SMTP_PORT`,
`SMTP_USER`, `SMTP_FROM`, `SMTP_TO`). Disparam em `halted`, `halt_inherited`, `persistence_down` (FAILED_PERSISTENCE),
`stops_blind_prolonged` e `daily_loss_pause`, com intervalo minimo; em segundo plano, com tempo limite: falha de
notificacao nunca afeta o robo. O SMTP usa STARTTLS (ou TLS implicito na porta 465) com `ssl.create_default_context()`:
certificado e nome do servidor sao verificados antes de enviar a senha.

### Configuracao nova (`API_*`)

`REDIS_URL` (o da API usa o mesmo do runner; `API_REDIS_URL` sobrescreve; em algumas maquinas `localhost` resolve para `::1`
e o Docker so publica em `127.0.0.1`: prefira `redis://127.0.0.1:6379/0`), `API_REDIS_PREFIX`, `API_RUNNER_HEARTBEAT_STALE_SECONDS`
(90), `API_COMMAND_PUMP_SECONDS`, `API_RUNNER_DATA_DIR`, `API_RUNNER_START_COMMAND`, `API_WS_*`, `API_BACKTEST_*`.
Cursores de paginacao (`<instante ISO com fuso>|<id>`) invalidos sao 422; byte NUL em `q`/`strategy` tambem. Corpos de controle
e de configuracao com campo desconhecido sao 422 (`liquidat: true` nunca vira uma parada sem venda). Papeis do banco
(`scripts/db_roles.sql`): o robo nao enxerga `control_commands`/`backtest_jobs` e so le `config_versions`; a API so atualiza as
colunas de estado dessas tabelas.
Segredo: arquivo `command_hmac_key` (sem ele a API nao assina e usa so o arquivo, se configurado; o runner so liga o canal
assinado com a chave presente e `engine.runner.paper start` sem `--no-redis`).

## Seguranca (decisoes)

- Sessao: cookie opaco `httpOnly; Secure; SameSite=Strict` (`__Host-` em producao); so o SHA-256 do token fica no banco. Expiracao
  por inatividade (30 min) e absoluta (12 h); o login e rotacionado ao concluir o 2FA (nova sessao, a pendente morre).
- CSRF: double-submit (`X-CSRF-Token` == cookie `ci_csrf`) e o valor tem que ser o HMAC(session_secret, id da sessao); alem
  disso `Origin` fora de `API_PANEL_ORIGINS` e recusado em metodos mutaveis. CORS sem curinga.
- Login em 2 passos; 2FA obrigatorio; TOTP janela +-1 com anti-replay (contador atomico); codigos de recuperacao one-time (HMAC).
- Throttle do login: bloqueio DURO progressivo (5 falhas: 30 s, dobrando ate 1 h) por par (usuario, IP) e por IP (20 falhas).
  Por usuario (todos os IPs) so ha atraso progressivo limitado (`API_LOGIN_USER_DELAY_CAP_SECONDS`, 2 s) e alerta de auditoria
  (`auth.login.targeted`): um anonimo nao tranca o admin legitimo, que entra de outro IP. A tentativa e contada ANTES de verificar a
  senha (rajada concorrente nao passa inteira) e devolvida se a senha estiver certa. Usuario inexistente tem o mesmo estado (chave =
  HMAC do nome + IP) e respostas uniformes. Argon2 roda fora de transacao/lock, atras de um semaforo global
  (`API_LOGIN_MAX_CONCURRENT_HASHES`, fila de `API_LOGIN_HASH_QUEUE_TIMEOUT_SECONDS`, depois 429). Bloqueios (429) sao auditados uma vez por
  janela/chave. Falhas no 2o fator mantem o bloqueio no banco (`users.locked_until`); `POST /users/{id}/unlock` ou
  `python -m api.cli unlock-user` o removem (auditado).
- Token de ativacao: criar usuario e redefinir 2FA devolvem UMA vez um token (24 h, hash no banco). Sem ele ninguem registra TOTP
  (`POST /auth/2fa/enroll` exige `activation_token`); consumido ao confirmar o 2FA. Repasse fora de banda, nunca junto da senha.
  Contas anteriores: `python -m api.cli issue-activation --username ...`. `create-admin` imprime o token.
- Usuario: somente ASCII `[a-z0-9._@-]` (3 a 64); nome com Unicode e recusado (Kelvin U+212A nao vira `k`). Rotulos: sem controle,
  bidi nem invisiveis. Texto livre digitado no login nunca vai para a auditoria (so o nome de quem existe ou um rotulo HMAC).
- Senhas Argon2id; politica: >= 12 caracteres, lista de comuns, entropia estimada, sem o nome de usuario.
- Segredos TOTP e chaves Binance: AES-256-GCM, nonce aleatorio, AAD = `tabela:id:campo`; chave mestra versionada
  (`api_master_keys`, linhas `versao:base64`). Rotacao: `python -m api.cli new-master-key`, reiniciar a API, `python -m api.cli reencrypt`.
- Chaves da Binance: write-only; verificadas (somente leitura, endpoint `apiRestrictions`) antes de gravar; chave com permissao de
  saque e recusada. Nenhuma ordem e enviada pela API.
- Erros: RFC 7807; validacao nunca devolve o valor recebido; excecoes inesperadas viram 500 generico com `request_id`.
- Auditoria no `audit_log` (append-only): ator, IP, user-agent, request id, acao, alvo, resultado; detalhes passam por `sanitize`.
- Segredos: `api_session_secret` (sessao/CSRF), `api_recovery_hmac_key` (HMAC dos codigos de recuperacao, proprio), `api_master_keys`.
  Sem `api_recovery_hmac_key` cai no `session_secret` (legado). Com os dois, codigos antigos continuam aceitos
  (`API_RECOVERY_ACCEPT_LEGACY_HASHES=true`); depois que todos regenerarem (`POST /auth/2fa/recovery-codes`) desligue a opcao. Trocar
  `api_recovery_hmac_key` invalida os codigos gerados com o valor anterior (cada usuario regenera).
- Proxy: exposta direto, deixe `API_TRUST_FORWARDED_FOR=false` (o cliente forjaria o `X-Forwarded-For`). Atras de proxy ligue-o E liste
  `API_TRUSTED_PROXIES` (IPs/CIDRs): so conexoes desses IPs tem o header acreditado, e vale o primeiro salto nao confiavel da direita.
  Em producao `trust_forwarded_for` sem lista e recusado; `binance_api_origin` precisa ser https.
- Proxy (detalhe): TODAS as linhas `X-Forwarded-For` sao juntadas (proxy que acrescenta linha nao deixa o cliente forjar o IP) e peers
  IPv4 mapeados em IPv6 (`::ffff:a.b.c.d`) casam os CIDRs IPv4. Em producao atras de proxy com `API_TRUST_FORWARDED_FOR=false` todos os
  clientes dividem o IP do proxy no throttle e na auditoria (a API loga `forwarded_for_ignored_behind_proxy` e `prod_without_trust_forwarded_for`).
- Throttle: IPv4 por endereco, IPv6 pelo prefixo /64 (por IP e por usuario+IP). Mapa lotado so de bloqueios ativos e fail-closed: chave NOVA
  recebe 429 generico (30 s) ate algum bloqueio expirar. O estado e em memoria, por processo (N workers = limite efetivo N vezes maior; prefira
  um unico processo ou limite no proxy). O DESBLOQUEIO e efetivo entre processos: `POST /users/{id}/unlock` e `python -m api.cli unlock-user`
  incrementam `users.lock_epoch` (migracao 0010) e cada processo da API, ao ver bloqueio ativo, compara a epoca a cada
  `API_UNLOCK_RECHECK_SECONDS` (padrao 5 s) e solta o bloqueio de senha que guardava em memoria.
- HashGate: o login de alto custo (Argon2) e limitado a `API_LOGIN_MAX_CONCURRENT_HASHES` e recusa (429) apos a fila curta. E um limite global:
  inundacao distribuida (muitos IPs) pode recusar logins legitimos; mitigue com rate limit no proxy. Saturacao: log `hash_gate_saturated` e
  contadores `rejected`/`peak`. Argon2 nunca roda dentro de transacao com lock (login, troca de senha).
- Limites conhecidos: "same-site" (SameSite=Strict) entre painel e API.
- Papeis do banco (duas URLs): o robo usa `cryptoinvest_app` (`DATABASE_*`, sem acesso a `users`, `user_sessions`, `recovery_codes`,
  `exchange_credentials`); a API usa `cryptoinvest_api` (`API_DATABASE_USER` + secret `api_database_password`; UPDATE por coluna em
  `users`, sem DELETE em usuarios/auditoria/diario). Aplique `scripts/db_roles.sql` com `CRYPTOINVEST_API_PASSWORD` apos CADA migracao e confira com `python -m api.cli check-grants`.
  Producao exige `API_DATABASE_USER` + `api_database_password` (papel distinto do robo).
  Migracao 0008: colunas do token de ativacao. A API ainda precisa trocar `users.role` (PATCH /users): mitigado por reauth TOTP e auditoria.
- Mutantes M06/M11/M15/M19 da revisao de QA: a lista original nao esta no repositorio, entao nao foi possivel mapea-los um a um;
  os caminhos que mutam (verificador de chave fail-closed, ultimo admin, reserva/refund do throttle, evicao sem liberar bloqueio,
  normalizacao de usuario, Label) tem teste proprio em `tests/unit/api` e `tests/integration/api`.
