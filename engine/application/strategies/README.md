# Estrategias

Todas long-only, com stop obrigatorio, alvo pelo R:R liquido de taxas (`compute_levels`), apenas velas
fechadas e continuas, e confluencia de >= 3 familias independentes (gatilhos nao contam). Registro:
`STRATEGY_REGISTRY` (`registry.py`), nome -> fabrica com parametros padrao pre-fixados.

## Fora do escopo (decisao do cliente)

- **GRID** e **DCA**: adiados. Ambos aumentam posicao perdedora (promedio), o que conflita com a
  proibicao de aumentar posicao em prejuizo. Nao implementar sem revisar essa regra.

## Experimentais

- `pullback_trend` (`pullback_trend.py`): compra a retomada de um pullback na EMA20 dentro de tendencia de alta (EMA200). Catalogada em `STRATEGY_REGISTRY`, mas listada em `EXPERIMENTAL_STRATEGIES`: fica fora de `default_strategy_names()` e de todo padrao de paper/producao. Metodologia, resultados e veredito em `docs/pullback_trend.md`.
