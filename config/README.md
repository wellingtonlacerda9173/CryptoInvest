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
