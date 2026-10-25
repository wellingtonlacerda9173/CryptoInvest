# Pesquisa de fornecimento de liquidez (LP): modelo, limites e vieses

Escopo: **somente analise**. O pacote `engine/research/lp/` nao usa carteira, chave privada, assinatura nem transacao.
Faz apenas HTTP GET a APIs publicas sem chave e, no estudo de faixas estreitas, chamadas RPC de LEITURA (`eth_call`, `eth_getLogs`,
`eth_blockNumber`; qualquer outro metodo e recusado em `data/rpc.py`) a endpoints RPC publicos sem chave. Nao toca em `engine/infrastructure/live`, no banco ou no Docker.

Pergunta: depois de gas e perda impermanente, as taxas de um pool Uniswap superam apenas guardar as moedas (HODL)?

## Uso

```
python -m engine.research.lp fetch                      # baixa/cacheia (data/cache/lp/, TTL 24h) e monta dataset.json
python -m engine.research.lp simulate --mode primary    # regras pre-definidas, uma unica execucao
python -m engine.research.lp simulate --mode sensitivity  # analise separada e rotulada
python -m engine.research.lp simulate --mode stress     # de-peg em pools de estaveis
python -m engine.research.lp report                     # reports/lp/lp_report.md e lp_report.json
```

Testes: `pytest tests/unit/research_lp` (sem rede). `pytest -m integration tests/integration/test_lp_public_apis.py` bate nas APIs.

## Fontes de dados

| Fonte | Uso | Observacao |
|---|---|---|
| DefiLlama Yields `yields.llama.fi/pools` e `/chart/{pool}` | lista de pools Uniswap v3, TVL diario, `apyBase` diario | sem chave |
| DefiLlama Coins `coins.llama.fi/chart/{chain}:{token}` | preco diario em USD de cada token | sem chave; maximo de 500 pontos por chamada (paginamos em blocos de 240 dias) |
| revert.finance | **descartado** | a documentacao publica (docs.revert.finance) descreve o produto, nao uma API; um endpoint `api.revert.finance/v1/...` responde sem chave, mas e **nao documentado**: nao usado (termos de uso desconhecidos) |
| GeckoTerminal API publica | **descartado como fonte principal** | OHLCV diario limitado aos ultimos 180 dias no plano publico (HTTP 401 alem disso: exige plano pago) e 30 req/min; nao permite janelas de 365 dias |
| Subgraphs / ticks por pool | **nao obtidos** | os gateways publicos atuais exigem chave; sem dados de ticks, a liquidez dos outros LPs e aproximada (abaixo) |

Reprodutibilidade: cada resposta e guardada em `data/cache/lp/` com URL, data/hora UTC e sha256; `dataset.json` lista todas as
consultas e o sha256 da serie de cada pool; o relatorio repete essas informacoes.

### Derivacao dos dados por dia

- Preco do pool: `P = USD(token0) / USD(token1)`; `quote` = token1. Sem OHLC: `low/high` do dia = min/max entre o fechamento anterior e o atual
  (subestima a oscilacao intradia; torna o tempo em faixa e o rebalanceamento ligeiramente menos frequentes que o real).
- Volume do dia (implicito): `volume = apyBase/100/365 x TVL / fee_tier`. Depende do calculo de `apyBase` da DefiLlama
  (volume e TVL do proprio dia); nao foi validado contra volume on-chain.
- TVL: serie diaria da DefiLlama.

## Modelo

Todas as contas em unidades do token quote e convertidas a USD pelo preco diario; taxas acumulam em USD (sem juros compostos).

1. **HODL**: carteira 50/50 mantida parada. Linha de base.
2. **LP v2 / faixa total**: perda impermanente exata `IL = 2 sqrt(r)/(1+r) - 1`, `r = P_t/P_0` (o simulador usa `V = 2 L sqrt(P)`
   e os testes confirmam IL = -20% para r = 4). Taxas por dia: `fee_tier x volume x L/(L + L_pool)`, com `L_pool = TVL/(2 sqrt(P))`
   (liquidez do pool tratada como v2). E um cenario **hipotetico**: mesmo volume e TVL, mas distribuicao v2 (kappa = 1).
3. **LP v3 faixa fixa +-X%**: `L = V / (2 sqrt(P) - P/sqrt(Pb) - sqrt(Pa))`; quantidades `x = L(1/sqrt(P) - 1/sqrt(Pb))`,
   `y = L(sqrt(P) - sqrt(Pa))` dentro da faixa, so token x abaixo, so token y acima. Taxa so em faixa:
   `fee x volume x fracao_em_faixa x L/(L + kappa L_pool)`, onde `fracao_em_faixa` = parte do intervalo `[low, high]` do dia dentro da faixa
   (volume suposto uniforme no intervalo).
   - **Aproximacao central (declarada)**: sem ticks, a liquidez ativa dos demais LPs e `kappa x L_pool` com `kappa = c^rho`,
     `c` = fator de concentracao da propria posicao versus faixa total. `rho = 0`: ninguem mais concentra (otimista, kappa = 1);
     `rho = 1`: os concorrentes concentram tanto quanto eu (vantagem de concentracao anulada); **caso base `rho = 0.5` (escolhido antes de ver resultados)**.
     A sensibilidade a rho e reportada separadamente.
4. **LP v3 com rebalanceamento**: ao **fechamento** em que o preco esta fora da faixa, fecha, troca para a proporcao exigida e reabre
   centrada no preco desse fechamento (sem olhar adiante). Custos por reajuste: gas de fechar + abrir + swap, mais `swap_cost_bps`
   (padrao 10 bps, taxa do pool + deslizamento) sobre o valor trocado.
5. **Estaveis**: mesma maquina com faixa estreita (+-0,5% no primario). **Estresse de de-peg**: sobrepoe ao preco do token base um choque
   (`usdc_mar2023`: queda a 0,88 e retorno em ~6 dias; `ust_colapso`: queda a 0,08 sem retorno), volume x8/x10. Sao cenarios,
   nao previsoes; TVL mantido (nao modela fuga de liquidez).
6. **Custos** (`gas.py`, valores de premissa, nao medidos): unidades de gas abrir 500k, fechar 250k, swap 150k; preco do gas
   Ethereum 3 gwei com ETH a US$ 2.500; L2 (Arbitrum/Base/OP) 0,02-0,05 gwei + US$ 0,03 por tx de dados no L1; Polygon 60 gwei com POL a US$ 0,25.
   Entrada: gas de abrir + custo do swap para ajustar 50/50 a proporcao da faixa. Saida: gas de fechar. O HODL e iniciado ja com as moedas
   (se se partisse de caixa, os dois pagariam o swap inicial). **Impostos fora do escopo.** Taxas de corretora/ponte fora do escopo.

## Metodologia anti-vies

- **Selecao a priori**: pools escolhidos por criterio objetivo na data inicial (janela de 545 dias): pares Uniswap v3 de uma lista fixa de moedas
  (ETH, BTC, estaveis grandes e algumas de capitalizacao media), TVL na data inicial >= US$ 1 mi, ranqueados por **TVL na data inicial**
  (nunca por retorno). Cotas: Ethereum 14, Arbitrum 6, Base 6, Optimism 4, Polygon 4. Moedas desconhecidas/meme excluidas.
  Faixas (+-10% volateis, +-0,5% estaveis), rho, custos e janelas foram fixados antes de rodar; o primario roda **uma vez**.
- **Sobrevivencia (vies OTIMISTA)**: a lista da DefiLlama so contem pools que existem hoje; pools mortos/abandonados nao aparecem, e pools novos
  (sem TVL no inicio) ficam de fora. Mitigacao parcial: ranqueamento por TVL **inicial** e relatorio separado dos pools cujo TVL caiu > 50%.
  Nao ha como recuperar pools que sumiram da fonte.
- **Look-ahead**: o rebalanceamento so usa o fechamento do dia em que decide.
- **Multiplas comparacoes**: informamos o numero de celulas da grade (categoria x rede x estrategia x capital x janela) e aplicamos Bonferroni
  ao teste binomial (pools acima do HODL vs 50%); reportamos mediana, P10-P90, IC95% bootstrap da mediana e % de pools acima do HODL, nao so a media.
  Unidade estatistica = pool (mediana dos blocos da janela). Os pools sao correlacionados (mesmo ETH/BTC); p-valores sao otimistas.
- **Janelas**: blocos sem sobreposicao de 90, 180 e 365 dias ancorados no fim comum; regimes alta/baixa/lateral por variacao do preco do token base (+-20%).
- **Capital**: US$ 1.000, 5.000 e 25.000; o gas pesa em % do capital.
- **Sensibilidade** (`--mode sensitivity`): variacoes de um parametro por vez (rho, gas, swap, larguras de faixa); rotulada como exploratoria, nao usada para escolher parametros.

## Riscos NAO modelados

JIT liquidity e MEV, selecao adversa (informed flow / LVR intradiario), reposicionamento de outros LPs por tick, incentivos/rewards,
fuga de liquidez em crises, risco de contrato/ponte/oracle, falha de de-peg alem dos cenarios, volume/TVL intradiario, composicao de taxas
em juros compostos, impostos, custo de oportunidade do capital.


---

# Faixas estreitas e reajuste preemptivo (L2, dados por hora)

Pergunta: LP v3 de faixa MUITO estreita num par dolar x BTC/ETH em rede barata (L2), desfeito e recriado antes de o preco sair da faixa,
lucra depois de TODOS os custos versus HODL? Codigo em `engine/research/lp/narrow/`; relatorio para leigo em `reports/lp/narrow_range_report.md`.

```
python -m engine.research.lp narrow-fetch       # GeckoTerminal: OHLCV horario por pool (cache em data/cache/lp/, gitignored)
python -m engine.research.lp narrow-snapshot    # RPC publico (leitura): slot0, liquidity, tickBitmap/ticks de cada pool
python -m engine.research.lp narrow-calibrate   # replay de eventos Swap (RPC) vs modelo horario
python -m engine.research.lp narrow-run --mode primary       # grade fixada a priori, uma execucao
python -m engine.research.lp narrow-run --mode sensitivity   # sensibilidades rotuladas, a parte
python -m engine.research.lp narrow-report
```

Testes: `pytest tests/unit/research_lp` (sem rede). `pytest -m integration tests/integration/test_lp_narrow_public_apis.py` bate na API/RPC.

## Dados

| Fonte | Uso | Observacao |
|---|---|---|
| GeckoTerminal API publica v2 `/networks/{rede}/pools/{pool}/ohlcv/hour` | OHLCV horario do token volatil em USD, **volume real em USD por hora** | sem chave; **limite de ~180 dias** (HTTP 401 alem disso), 30 req/min nominais (na pratica ha 429 frequente: o cliente espera e repete); horas sem negocio nao vem na resposta (preenchidas com preco anterior e volume zero) |
| GeckoTerminal `/networks/{rede}/dexes/{dex}/pools` | lista de pools Uniswap v3 por rede com `reserve_in_usd` | criterio de escolha abaixo |
| RPC publico (arb1.arbitrum.io, mainnet.base.org, mainnet.optimism.io, polygon-bor-rpc.publicnode.com, mainnet.unichain.org) | `eth_blockNumber`, `eth_call` (slot0, liquidity, tickSpacing, token0/1, decimals, tickBitmap, ticks, Multicall3.getCurrentBlockTimestamp), `eth_getLogs` (Swap) | sem chave; intervalo minimo entre chamadas; todas as respostas de `eth_call` em bloco fixo ficam em cache com hash |
| Subgraphs, revert.finance, fontes com chave | **nao usados** | exigem chave ou sao nao documentados |

Redes cobertas: Arbitrum, Base, OP Mainnet, Polygon, Unichain. Linea/zkSync/Scroll/etc. nao foram sondadas (limite de requisicoes da fonte).

**Selecao de pools (criterio fixo, antes de rodar):** por rede e categoria (BTC/estavel, ETH/estavel, uma moeda media/estavel), o pool Uniswap v3 de maior
`reserve_in_usd` com idade >= 190 dias e cobertura horaria >= 70% das 4.320 horas; ate 3 tentativas por categoria (cobertura insuficiente passa ao proximo).
O ranking usa a liquidez NA DATA DA COLETA (a fonte nao fornece a liquidez historica), nunca retorno. Isso introduz **vies de sobrevivencia**.
Derivados de ETH (wstETH etc.) e pares nao estavel x volatil foram excluidos.

## Concentracao dos outros LPs (snapshot de ticks)

`narrow/snapshot.py` le, num bloco fixo, `slot0`, `liquidity()`, o `tickBitmap` e `ticks(t)` ate +-12% do preco; `build_profile` reconstroi a liquidez
ativa dos OUTROS LPs por intervalo de tick (partindo de `liquidity()` e somando/subtraindo `liquidityNet`). O perfil e **um instante (agora), nao historico**:
supomos que o perfil relativo ao preco corrente e o TVL em USD sao estacionarios nos 180 dias. Para cada pool/largura o relatorio traz `kappa` (liquidez
media dos outros na faixa dividida pela que teria o TVL em faixa total) e o capital em USD que daria 50% da liquidez da faixa.
O replay de eventos `Swap` (`narrow/replay.py`) reconstroi, em janelas de 24h, as taxas que posicoes fixas teriam recebido (liquidez ativa registrada
em cada evento) e compara com o modelo horario: so e viavel para poucos dias (limite de `getLogs` e volume de eventos); eventos Mint/Burn e `fee_growth`
por tick **nao** foram reconstruidos (nao ha historico de ticks sem RPC de arquivo/indexador).

## Modelo (horario, por trajetoria)

- Trajetoria: cada barra horaria vira 3 trechos lineares em log (abertura -> minima -> maxima -> fechamento se subiu; ordem invertida se caiu), com duracoes
  proporcionais ao tamanho do trecho. A abertura de cada barra e o fechamento da anterior. A ordem minima/maxima e uma suposicao (os dados horarios nao a informam).
- Taxas: o volume USD da barra e repartido entre os trechos proporcionalmente a medida `integral de L_total d sqrt(P)` (volume que atravessa um intervalo de preco e proporcional
  a liquidez ali); minha taxa = `fee x volume x integral(L_me)/(integral(L_outros + L_me))`. Isso captura a concentracao real do snapshot sem parametro `rho`.
  Barras sem movimento negociam num intervalo de +-1 pb. Competicao (JIT/gestores) reduz a parcela por um fator.
- Regras de reajuste (todas sem look-ahead, decididas so com o preco ate o instante): `fixa`; `ao fechar fora` (fechamento horario fora da faixa); `preemptiva X%`
  (preco toca X% do meio-caminho ate a borda, com monitoramento continuo; recentra no preco do gatilho); `cooldown` (intervalo minimo entre giros, estado: se o gatilho
  persiste ao fim do cooldown, dispara); `atraso` (execucao N minutos depois do gatilho, ao preco entao vigente).
- Custo por giro: gas de fechar + swap + abrir (por rede, `gas.py`: baixo x0,4 / medio x1 / alto x5, premissas nao medidas), taxa do swap (taxa do pool, parametro) +
  impacto de preco = valor trocado / (L_ativo x sqrt(P)) do snapshot (teto de 5%).
- Decomposicao exata: `excesso = taxas - gas - swap + IL realizada + efeito de exposicao`, onde `IL realizada = soma por ciclo (valor da posicao - valor de manter o
  inventario do inicio do ciclo)` (perda por selecao adversa/LVR realizada, medida no caminho de preco) e `efeito de exposicao = soma por ciclo (variacao de valor do inventario mantido)
  - ganho do HODL` (ficou com menos/mais BTC que o HODL). Soma conferida por teste.
- Checagem teorica: LVR = `sigma^2/4 x L x sqrt(P)` por unidade de tempo dentro da faixa (Milionis et al.; em produto constante equivale a sigma^2/8 do valor do pool).
  Em passeio aleatorio sintetico, a IL simulada com giros fica a 0,7-1,3x do LVR teorico (testado); nos dados reais o relatorio mostra a razao.

## Metodologia anti-vies

Grade (6 larguras x 7 regras = 42 celulas) e parametros base fixados no codigo antes de rodar (`narrow/runner.py`: `WIDTHS`, `RULES`, `BASE`); o primario roda **uma vez**.
Sensibilidades (gas, profundidade dos outros, taxa de swap, atraso, competicao, capital) rodam em `--mode sensitivity`, rotuladas, sem retroalimentar o primario.
Bonferroni com 42 testes sobre o teste binomial (pools acima do HODL vs 50%); reportamos mediana, P10-P90 e % de pools acima do HODL. Regimes: 6 blocos de 30 dias por pool
(alta/baixa/lateral por variacao de +-10%; volatilidade alta/baixa por mediana de sigma). Pools correlacionados (mesmo BTC/ETH): p-valores sao otimistas.

## Nao medido

Historico de profundidade por tick (so um instante), Mint/Burn/fee_growth historico, JIT/MEV reais, custo real de gas (premissa), ordem intrabarra real, volume por trecho de preco,
impostos, risco de contrato/ponte, janelas > 180 dias (limite da fonte), pools mortos (sobrevivencia).

## Resultados e limites do replay (execucao de 2026-10-25)

- 14 pools (5 redes x BTC/ETH/moeda media), 4.320 candles horarios, volume real. Primario: 42 celulas, nenhuma combinacao com reajuste teve mediana acima do HODL.
- Replay de eventos Swap: so foi possivel em Polygon e Unichain (9 janelas de 24h); Arbitrum devolve `blockTimestamp = 0` e nao guarda estado antigo
  (impossivel datar eventos sem `eth_getBlockByNumber`), Base devolveu 429 persistente e OP Mainnet HTTP 413 no `eth_getLogs`. Razao volume por eventos / GeckoTerminal = 1,00;
  taxas reais / modelo (media geometrica) entre 0,87 e 1,11 por largura (dispersao grande por janela nas faixas +-0,25% e +-0,5%).
- Correcao de modelo durante o desenvolvimento (nao e ajuste de parametro): o denominador da repartição do volume e refeito a cada reajuste dentro da barra; janelas dos pools alinhadas
  ao mesmo intervalo de 4.320 horas; criterio de escolha das moedas medias passou de liquidez para volume 24h apos ver NOMES/volumes (pools inativos), antes de qualquer simulacao.
