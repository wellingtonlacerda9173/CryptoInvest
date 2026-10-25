# Prontidao para o modo live

O `LiveGateway` (`engine/infrastructure/live`) segue bloqueado e nao integrado ao runner.

## Exigencia antes de qualquer integracao

A guarda `market_data_stale` (feed de velas obsoleto: novas entradas recusadas, stops ativos, estado PAUSED
automatico) e a deteccao de book parado (`stops_blind`) vivem hoje no `PaperRobot`. Antes de integrar o gateway
live e obrigatorio existir teste automatizado, rodando com o `LiveGateway`, que prove:

1. `market_data_stale`: sem vela fechada esperada alem da tolerancia, nenhuma entrada nova e aceita, stops
   existentes continuam sendo geridos e o alerta e emitido; ao voltarem as velas, o estado limpa sozinho.
2. Book parado: sem atualizacao do book alem do limite, `stops_blind` pausa novas entradas e alerta, e a
   recuperacao do book retoma sem intervencao manual.
3. A pausa por obsolescencia e automatica: nao oferece "Retomar" e nao sobrescreve halt, kill-switch ou pausa
   manual (as razoes se somam).

Sem esses testes verdes contra o gateway live, a integracao nao deve ser feita.
