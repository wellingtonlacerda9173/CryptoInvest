# LiveGateway (Fase 7B-1): gateway Binance Spot REST assinado

Implementa `ExchangeGateway` + protecao (`raise_stop`, `drop_target` assincronos). **Nao e usado pelo runner**
nesta fase. Nunca foi chamado contra a Binance real: os testes usam a Binance FALSA (`tests/fakes/fake_binance.py`).

## Protecao na corretora (regra inviolavel)

- Entrada = **OPOCO** (`POST /api/v3/orderList/opoco`): compra LIMIT (GTC, de trabalho) + OCO de venda
  (acima `LIMIT_MAKER` = alvo; abaixo `STOP_LOSS` ou `STOP_LOSS_LIMIT`). As pernas da OCO so entram no livro quando
  a compra e **totalmente** executada e a corretora define a quantidade pela recebida LIQUIDA de comissao
  (por isso nao se usa OTOCO, que exige `pendingQuantity` fixa e falharia com comissao em moeda base).
- Fill parcial nao ativa as pernas: apos `partial_fill_grace_seconds` o gateway cancela o resto e coloca uma OCO
  avulsa para o executado. OCO nao ativada apos o fill (`protection_activation_grace_seconds`), recusada ou expirada:
  OCO avulsa com `protection_retries`; se falhar ou o stop "dispararia na hora" (-2021): venda a MERCADO (emergencia).
  Se ate a emergencia falhar: gateway PAUSA, alerta critico e `ProtectionError`.
- Estado `exposed` (base em carteira sem stop ativo): toda excecao apos um cancel/venda (leitura de saldo, rede,
  429/418, relogio, resultado desconhecido) cai no mesmo caminho: recolocar protecao -> venda de emergencia ->
  PAUSA + alerta critico mantendo `exposed`. **Retomada segura**: a PRIMEIRA acao de `poll`, `raise_stop`,
  `drop_target`, saidas e novas entradas e reproteger/vender o exposto (adotando antes uma protecao que um POST de
  resultado desconhecido possa ter criado); novas entradas ficam bloqueadas enquanto houver exposicao (o `resume()`
  do operador nao as libera). `poll` repara TODAS as entradas (falha de uma nao bloqueia as outras) e consolida
  os erros no fim.
- **Ledger por entrada** (fonte unica de quantidade): `owed = comprado liquido de comissao - vendido`, com `vendido`
  indexado por client id (pernas stop/alvo de TODAS as geracoes, saidas e vendas de emergencia; idempotente, `max`
  por ordem). Antes de qualquer venda ou recolocacao de protecao o gateway confere na corretora toda ordem de venda
  da entrada ainda nao encerrada (`_sync_ledger`) e as respostas de DELETE (`orderReports`) tambem alimentam o ledger.
  Toda venda e limitada por `min(owed, saldo livre)` e nunca excede `owed`: saldo alheio (BTC que o robo nao comprou)
  nunca e vendido, nem por stop/alvo executado antes do 1o poll, nem por saida, nem por emergencia. Uma SAIDA so e
  aceita contra uma entrada aberta deste robo e com `quantidade <= owed` (sem entrada aberta, ou acima do devido:
  `FilterViolationError`; apos reinicio o gateway nao conhece posicoes antigas e por isso recusa saidas).
- Compra parcial: o cancel da lista so avanca depois de um `GET order` CONFIRMAR CANCELED/FILLED (ate
  `protection_retries` tentativas com backoff). Sem confirmacao: a entrada continua `working`, o gateway PAUSA com
  alerta critico, o proximo ciclo repete o cancel e um fill posterior da compra viva e protegido normalmente.
- Marcacao `exposed` ANTES de qualquer DELETE de protecao (`raise_stop`, `drop_target`, saidas): `CancelledError`
  (ou qualquer excecao) no meio com a entrada exposta PAUSA + alerta critico e relanca. DELETE sem resposta valida
  vira estado DESCONHECIDO: so um GET prova "ativa" (protecao mantida, estado restaurado) ou "encerrada"; sem GET a
  entrada segue exposta e a retomada segura confere o ledger e adota/recoloca a protecao.
- Resposta 2xx com corpo nao-JSON (proxy/CDN/portal cativo) e `OutcomeUnknownError`, nunca sucesso.
- Gateway cego: `poll_blind_threshold` (5) falhas consecutivas de leitura numa entrada aberta => pausa + alerta.
  Compensacao de relogio em chamada critica registra log de erro e alerta (`warning`) com o desvio.
- Capital: a reserva do teto `capital_cap_quote` ocorre sem `await` entre a conferencia e o registro da entrada
  (duas `place_order` concorrentes nao ultrapassam o teto); falha posterior libera a reserva.
- Venda de emergencia: so a quantidade devida pelo ledger da entrada (`owed`), nunca o saldo total; confere
  `executedQty` (parcial/EXPIRED => repete com backoff, `emergency_max_attempts` falhas), respeita `LOT_SIZE` e
  `MARKET_LOT_SIZE` (passo e `maxQty`: divide em ordens). 418 => pausa na hora (o cliente bloqueia localmente ate o
  fim do banimento, sem martelar); 429 => respeita Retry-After e retenta.
- Entrada LIMIT GTC e cancelada no `poll` ao vencer `guards.expires_at` (corrida com fill: o fill e protegido
  normalmente).
- `raise_stop`: so sobe, abaixo do alvo e do bid corrente (margem `min_stop_distance_bps`); cancel+replace da OCO
  (nao existe substituicao atomica de order list) e, se o replace falhar apos retries, venda a mercado.
  **Risco residual**: entre o DELETE e o novo POST (2 requisicoes) a posicao fica sem stop na corretora.
- Nenhum caminho cancela protecao fora de raise_stop/drop_target (com recolocacao imediata), saida executada e
  emergencia; `cancel_order` de `<id>:oco`/ids protetivos e recusado.
- Saidas: MARKET ou LIMIT IOC (nunca ordem ociosa sem stop); a protecao e cancelada so para vender e recolocada
  no restante, mesmo se a venda falhar.

## Demais regras

Allowlist EXATA de (metodo, caminho) Spot no cliente (`time`, `exchangeInfo`, `avgPrice`, `ticker/price|bookTicker`,
`account`, `myTrades`, `openOrders` so GET, `order` GET/POST/DELETE, `orderList` GET/DELETE, `orderList/oco|opoco`
POST): `DELETE /openOrders`, `cancelReplace`, `userDataStream`, `/sapi`, `/fapi`, saques e transferencias =>
`ForbiddenEndpointError`; redirecionamentos HTTP nunca sao seguidos; simbolo fora da lista e ordem fora dos
filtros (LOT_SIZE, PRICE_FILTER, NOTIONAL, PERCENT_PRICE_BY_SIDE; `exchangeInfo` com TTL) recusados antes de enviar;
entradas exigem `ApprovedEntry` (selo) apos `revalidate_order`; ids deterministicos (`derived_id`) e, apos
timeout/5xx, consulta por id ANTES de reenviar; `read_only` bloqueia todo POST/DELETE no cliente HTTP; relogio
sincronizado com `/api/v3/time` (recusa acima de `max_clock_drift_ms`); 429 respeita Retry-After; 418 bloqueia
localmente ate o fim do banimento; segredo nunca logado (`ApiCredentials` mascara; logs passam por `scrub_text`).
Saldo livre limitado por `capital_cap_quote`. Fills em `FillLog` (append-only, idempotente por trade id, com
outbox: falha do sink nao perde nem duplica o fill nem interrompe a protecao; reentrega a cada ciclo).

## Ambiente e host (`LiveConfig`)

`environment` obrigatorio (`mainnet` | `testnet` | `local_fake`) e conferido contra o host (allowlist exata, https,
sem userinfo/porta/caminho): mainnet = `api.binance.com`, `api-gcp.binance.com`, `api1..4.binance.com`; testnet =
`testnet.binance.vision` SOMENTE com `testnet=True`; fake = `127.0.0.1`/`localhost`/`::1` exatos SOMENTE com
`allow_local_fake=True` (flag de testes: nunca vem de env/producao). `ApiCredentials.environment` e conferido
contra o da config: chave de testnet nao serve na mainnet e vice-versa. Em chamadas criticas um relogio desviado e
compensado pelo offset medido (ate 60 s) em vez de recusar; nas demais continua recusando acima de `max_clock_drift_ms`.

## Comissao

`LiveFill.commission` fica na moeda paga; `commission_quote` e o custo na moeda de cotacao: base => `qtd x preco`;
BNB/outras => cotacao do momento (`GET /ticker/price`); sem cotacao => estimativa CONSERVADORA
(`quoteQty x fallback_fee_rate`, padrao 0,1%) com `commission_estimated=True`.

## Orcamento de tempo da protecao (`protection_deadline_seconds`, padrao 20 s)

A exposicao comeca em `_expose` (alerta critico imediato, `on_alert`) e tem um ORCAMENTO TOTAL de tempo: toda a
cadeia protecao (leituras do ledger/adocao + POSTs da OCO + lookups + retries) roda sob `asyncio.timeout`; estourado,
o gateway PAUSA e parte DIRETO para a venda de emergencia (antes de vender adota uma protecao que o POST
cancelado possa ter criado). Calculo da janela maxima ate o inicio da emergencia: `protection_deadline_seconds`
(20 s) + a requisicao em voo cancelada (imediata) ~ 20 s. Antes deste orcamento o pior caso com os padroes
(`request_timeout_seconds=10`, `read_retries=3`, `order_lookup_retries=3`, `protection_retries=3`) era de ~8,5 min.
Medicao na fake com POST da OCO pendurado (script de medicao local; teste
`test_protection_budget_sends_the_emergency_sale_within_the_deadline`): deadline 0,5 s => 0,531 s ate a posicao
estar vendida; 1,0 s => 1,016 s; 2,0 s => 2,015 s (overhead da emergencia no loopback ~15-30 ms). Na corretora real
some-se o RTT da cadeia de emergencia (sincronia do ledger, saldo, ordem a mercado e confirmacao: ~4-5 RTT). A
cadeia de EMERGENCIA em si nao tem orcamento de tempo (cada chamada limitada por `request_timeout_seconds`, no maximo
`emergency_max_attempts` falhas); o alerta critico ja saiu no inicio da exposicao e, esgotada, o gateway fica
pausado/`exposed` com retomada segura no proximo ciclo. `raise_stop` e uma janela deliberada (sem alerta proprio)
sob o mesmo orcamento.

## Janela de exposicao (medida na fake: `tests/unit/live/test_safe_resume.py`)

| Cenario | Janela sem stop | Observacao |
| --- | --- | --- |
| Entrada OPOCO preenchida | 0 | pernas ativam no proprio fill |
| `raise_stop` (cancel+replace) | ~0,6 ms e 1 requisicao (o POST do novo OCO) | na corretora real: 1 RTT; nenhuma leitura na janela |
| Fill parcial | ate `partial_fill_grace_seconds` (5 s) + 1 `poll_interval` (1 s); depois ~4 ms/9 requisicoes na fake | grace logico verificado por teste |
| Pernas nao ativam apos fill total | ate `protection_activation_grace_seconds` (3 s) + 1 `poll_interval`; depois ~3 ms/7 requisicoes | idem |

Os tempos em ms vem do loopback da fake (indicativos); os limites em segundos sao os da configuracao e sao
afirmados por testes com relogio logico.
`reconcile()` compara contas/ordens/trades e **pausa** em divergencia (nunca altera stops).

Documentacao Binance consultada: Spot API REST `trading-endpoints` (OPOCO/OTOCO/OCO, Cancel Order list, Query
Order list) e `faqs/opo.md` (quantidade das pernas liquida de comissao; apenas BUY de trabalho e SELL pendente).
