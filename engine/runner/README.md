# engine/runner

Pontos de entrada do processo: `backtest.py` (CLI do backtest, ver o README da raiz), `candle_source.py` (carga de
velas: Timescale, cache local e Binance publica) e o runner do paper (`paper.py` CLI; `paper_robot.py` o robo;
`paper_journal.py`/`paper_outbox.py` persistencia duravel com outbox; `paper_control.py` comandos do operador;
`paper_lock.py` instancia unica; `paper_report*.py` relatorios e trava do Live). Ver "Paper trading" no README da raiz.
