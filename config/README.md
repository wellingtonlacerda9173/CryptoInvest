# Calendario de eventos de volatilidade (`volatility_events.json`)

O gate de risco so aprova entradas ao vivo se o calendario tiver cobertura valida. Arquivo ausente, sem
`loaded_at`/`valid_until`, vencido ou vazio sem confirmacao => toda entrada e recusada com
`event_calendar_stale` (falha fechada). O backtest dispensa a cobertura (`mode="backtest"` em `evaluate_entry`); o
paper so a dispensa com a flag explicita `--allow-open-calendar` (aviso a cada decisao); o Live nunca.

## Campos

| Campo | Significado |
|---|---|
| `loaded_at` | Quando voce conferiu/atualizou o calendario (ISO 8601 UTC, ex.: `2026-10-18T12:00:00Z`) |
| `valid_until` | Ate quando o calendario cobre os eventos (ex.: fim do ultimo mes que voce conferiu) |
| `acknowledged_no_events` | `true` apenas se, de ciencia, NAO ha eventos relevantes ate `valid_until` |
| `windows` | Lista de `{"start", "end", "label"}` em UTC (ex.: FOMC, CPI). O robo nao entra de 30 min antes a 30 min depois |

## Como preencher

1. Liste os eventos de alto impacto do periodo (FOMC, CPI, payroll etc.) e coloque cada um em `windows`.
2. Defina `loaded_at` com a data/hora atual e `valid_until` com o fim do periodo que voce cobriu.
3. Se nao ha eventos no periodo, deixe `windows: []` e defina `acknowledged_no_events: true`
   (decisao consciente sua; sem isso o calendario vazio e recusado).
4. Renove antes de `valid_until`: depois dele o robo para de abrir posicoes (as abertas continuam geridas).

Exemplo:

```json
{
  "loaded_at": "2026-10-18T12:00:00Z",
  "valid_until": "2026-12-31T23:59:59Z",
  "acknowledged_no_events": false,
  "windows": [
    {"start": "2026-10-28T18:00:00Z", "end": "2026-10-28T19:30:00Z", "label": "FOMC"}
  ]
}
```

## Cobertura atual (verificada em 2026-10-25)

`valid_until` = 2027-01-15T23:59:59Z. Fora dessa data o gate volta a recusar entradas (falha fechada); renove antes.
O schema nao aceita campos extras, entao as fontes ficam aqui.

| Evento | Data (UTC) | Fonte |
|---|---|---|
| FOMC Out | 2026-10-28 (decisao 14:00 ET = 18:00Z, horario padrao do FOMC) | federalreserve.gov/monetarypolicy/fomccalendars.htm |
| FOMC Dez | 2026-12-09 (14:00 ET = 19:00Z) | idem |
| Payroll (Employment Situation) | 2026-11-06, 2026-12-04, 2027-01-08 as 13:30Z (8:30 ET) | bls.gov/schedule/news_release/empsit.htm |
| CPI | 2026-11-10, 2026-12-10, 2027-01-13 as 13:30Z (8:30 ET) | bls.gov/schedule/news_release/cpi.htm |
| Deribit BTC/ETH (08:00 UTC) | 2026-10-30, 2026-11-27, 2026-12-25 (trimestral) | API publica Deribit `get_instruments` (expirations listadas) |

A confirmar (nao incluidos): FOMC de janeiro/2027 e vencimento Deribit de 2027-01-29 (ainda nao listado), alem de
qualquer evento apos 2027-01-15. Horarios do BLS e da decisao FOMC em ET assumidos (a pagina nao declara fuso/hora).

## Atualizacao automatica

O servico `calendar-updater` (compose local e stack Swarm) roda `python -m engine.runner.calendar_update --loop` a
cada 24 h (2 h apos falha) e e o UNICO escritor do volume `calendar`
(`/var/lib/cryptoinvest/calendar/volatility_events.json`); runner e API montam o volume somente leitura. O arquivo
desta pasta e so a semente da imagem: o runner/atualizador usam o de maior `valid_until` entre os dois.

Na API, `API_CALENDAR_PATH` aponta para o arquivo do volume (`GET /api/calendar` usa o de maior cobertura entre ele e
a semente); sem a variavel a API le somente a semente `config/volatility_events.json`, como o runner.

Teste manual: `python -m engine.runner.calendar_update --calendar config/volatility_events.json --dry-run`.

Fontes (HTTPS, sem chave): federalreserve.gov (FOMC), bls.gov (empsit e cpi), Deribit `get_instruments` (BTC e ETH).
Qualquer erro, estrutura alterada ou dado ambiguo em UMA fonte => nada muda (falha fechada). `valid_until` avanca
ate o MINIMO das coberturas confirmadas e nunca recua; janelas so sao adicionadas/estendidas.

| Evento | Hora padrao | Janela gravada |
|---|---|---|
| FOMC (decisao) | 14:00 America/New_York (horario de verao via zoneinfo) | decisao a decisao + 90 min |
| Payroll / CPI (BLS) | 08:30 America/New_York | divulgacao a divulgacao + 60 min |
| Deribit mensal/trimestral (ultima sexta) | 08:00 UTC | vencimento -1 h a +1 h |

Cobertura por fonte: FOMC = fim do ultimo ano com as 8 reunioes listadas; BLS = ultima divulgacao listada (meses de
referencia consecutivos); Deribit = ultimo mes contiguo com o vencimento listado para BTC e ETH. O gate ainda
acrescenta as suas margens antes/depois. Reuniao FOMC nao agendada nao e prevista por nenhuma fonte.

Ao lado do arquivo: `.bak` (versao anterior), `.audit.jsonl` (trilha de cada mudanca) e `.status.json` (ultima
tentativa; exposto em `GET /api/calendar`). O runner recarrega o arquivo a cada hora; arquivo invalido ou com
cobertura menor e ignorado com aviso (`event_calendar_reload_ignored`) e o calendario em memoria e mantido.
