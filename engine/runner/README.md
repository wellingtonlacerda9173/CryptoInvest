# engine/runner

Pontos de entrada do processo: `backtest.py` (CLI do backtest, ver o README da raiz), `candle_source.py` (carga de
velas: Timescale, cache local e Binance publica) e o runner do paper (`paper.py` CLI; `paper_robot.py` o robo;
`paper_journal.py`/`paper_outbox.py` persistencia duravel com outbox; `paper_control.py` comandos do operador;
`paper_lock.py` instancia unica; `paper_report*.py` relatorios e trava do Live). Ver "Paper trading" no README da raiz.

Fase 4 (painel): `command_channel.py` consome os comandos ASSINADOS (HMAC, carimbo, nonce) da API pelo Redis e os grava na
`CommandQueue` (as protecoes de sempre valem); `paper_versioned.py` le a ultima configuracao versionada (`config_versions`) no
`start`; `backtest_job.py` e o worker de backtest em subprocesso. Alertas externos opcionais: `engine/infrastructure/notifications.py`.
`signing.py` assina tudo que cruza o Redis (acks e eventos, alem dos comandos); a entrega dos comandos e at-least-once
(`LMOVE` para `commands:processing`, nonce so depois da gravacao local) e o resultado de cada comando e explicito
(`applied|partially_applied|refused|failed`; `apply-config` aplica reducoes por campo). Detalhes e contratos em `api/README.md`.
