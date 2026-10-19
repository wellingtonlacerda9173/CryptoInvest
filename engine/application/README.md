# engine/application

Casos de uso e orquestracao. Depende apenas de `engine.domain` (e do gateway de backtest, em `backtest/`).

- `indicators`, `detectors`, `strategies`: sinais
- `risk`: gate de entrada, dimensionamento, gestao de posicao e travas (`RiskState`)
- `backtest`: motor vela a vela, metricas, walk-forward e relatorios
