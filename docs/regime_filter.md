# Filtro de regime (engine/application/risk/regime_filter.py)

Nova entrada (long, spot) so em alta confirmada; queda, lateral e duvida recusam (`regime_downtrend`,
`regime_sideways`, `regime_unknown`). Saidas e gestao de posicao nunca consultam o regime.

- Janela fixa: o veredito usa sempre as ultimas `REGIME_WINDOW` (400) velas fechadas, no backtest, no paper e no
  live; o aquecimento das medias/ADX depende do tamanho da janela e, sem esse corte, o veredito divergia ~0,5%
  entre 200/400/800 velas.
- Piso de historico: `history_window >= MIN_CANDLES` (95 velas: 23 grupos completos do tempo superior + folga de
  alinhamento ao relogio). A configuracao (paper e backtest) recusa valores menores na validacao.
- O gate sempre calcula o regime a partir das velas; o resultado e reaproveitado so por um cache interno de 1
  entrada com chave nas proprias velas e limiares (o robo mostra o regime e o gate o reavalia sem custo duplo).
  Nenhum regime vindo de fora e aceito.

## Metodologia e limites do "antes/depois" do backtest

O comparativo antigo (28 e 22 trades "depois" contra o "antes") NAO prova vantagem: amostra pequena, o "antes"
foi interrompido pelo halt de drawdown, e as duas pontas nao tinham o mesmo periodo/estado, entao os numeros nao
sao comparaveis. O comparativo abaixo e mais honesto, mas continua descritivo: mesmo periodo
(2024-10-18 19:00 a 2026-10-18 19:00 UTC, 1h, 17.520 velas em cache), mesmas velas, `history_window` 400, mesmas
estrategias (`elephant_bar_breakout` + `ema_cross_trend`), custos padrao (taxa 0,1%, slippage 5 bps, latencia 1
vela), halt de drawdown DESLIGADO so neste experimento. "Antes" = mesmo gate, mas o avaliador forcado em ALTA
(o veredito real e apenas registrado para agrupar as entradas por regime); "depois" = filtro real. Sem
walk-forward, sem varredura de parametros, uma unica rodada por ativo.

| Ativo | Cenario | Trades | Expectativa (R) | Retorno | Drawdown max. | Taxas (USDT) |
|---|---|---|---|---|---|---|
| BTC/USDT | antes (forcado em alta) | 191 | -0,219 | -11,11% | 13,66% | 864,41 |
| BTC/USDT | depois (filtro real) | 28 | -0,129 | -0,13% | 2,00% | 136,36 |
| ETH/USDT | antes (forcado em alta) | 173 | -0,080 | -3,70% | 11,69% | 799,59 |
| ETH/USDT | depois (filtro real) | 22 | +0,187 | +1,88% | 2,22% | 105,92 |

Entradas do cenario "antes" por regime real no momento do sinal: BTC 70 queda, 98 lateral, 22 alta, 1 indefinido;
ETH 78 queda, 77 lateral, 17 alta, 3 indefinido. Isto e: o filtro remove ~88-90% das entradas, quase todas em
queda/lateral. Limites: a expectativa em R do "depois" (BTC -0,129; ETH +0,187) tem sinais opostos e 22-28
trades nao permitem intervalo de confianca util (ETH, +0,187 R com 22 trades, e indistinguivel de zero); periodo unico e dependente de regime
de mercado; o ganho observado vem sobretudo de operar menos (menos taxas e menos perdas), nao de comprovada
vantagem de selecao. Nao usar estes numeros para prometer retorno.
