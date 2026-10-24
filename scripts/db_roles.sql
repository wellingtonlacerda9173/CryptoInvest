-- Papeis do banco: o robo (aplicacao) nao e dono de nada e nao altera o diario; so o papel de migracao altera o schema.
--
-- NAO aplicar em producao antes da Fase 7. Executar como superusuario, DEPOIS de `alembic upgrade head`
-- (e de novo apos CADA migracao que criar tabela ou coluna escrita pela API: sem isso a API perde UPDATE e
-- falha em producao; `python -m api.cli check-grants` verifica), uma vez por banco, no banco alvo:
--
--   CRYPTOINVEST_MIGRATOR_PASSWORD=... CRYPTOINVEST_APP_PASSWORD=... CRYPTOINVEST_API_PASSWORD=... --       psql -X -v ON_ERROR_STOP=1 -d cryptoinvest -f scripts/db_roles.sql
--
-- As senhas vem do ambiente (`\getenv`), nunca de `-v`, para nao aparecerem em argv/logs. Opcionais:
-- `-v app_role=... -v api_role=... -v migrator_role=...` trocam os nomes dos papeis (padrao cryptoinvest_app,
-- cryptoinvest_api e cryptoinvest_migrator; usado pelos testes para nao tocar nos papeis reais).
--
-- Papeis:
--   migrator  dono das tabelas; usado SOMENTE pelo alembic (DATABASE_URL de migracao).
--   app       usado SOMENTE pelo robo (DATABASE_USER/DATABASE_PASSWORD); sem ownership, sem DDL e sem
--             UPDATE/DELETE/TRUNCATE nas tabelas imutaveis (alem dos triggers append-only, que o dono poderia
--             desligar). NAO enxerga users, user_sessions, recovery_codes, exchange_credentials,
--             control_commands nem backtest_jobs; le config_versions (nao grava).
--   api       usado SOMENTE pela API do painel (API_DATABASE_USER / secret `api_database_password`, duas URLs de
--             banco: a do robo e a da API). Le o diario, enfileira comandos/backtests (UPDATE so nas colunas de
--             estado), grava a auditoria sem poder altera-la e e dona dos dados de usuarios/sessoes/credenciais (UPDATE por coluna em users,
--             nunca em id/username/created_at; sem DELETE em users nem em tabelas de auditoria/diario).
--             Limite conhecido: a API precisa trocar `users.role` (PATCH /users); um comprometimento do
--             processo da API pode se promover. Mitigacoes: reautenticacao TOTP recente na rota, auditoria
--             append-only e este papel nao ter acesso ao diario alem de leitura/fila.
--
-- Limites conhecidos: superusuario e o dono ignoram REVOKE; `session_replication_role = replica` (so
-- superusuario) pula triggers. Por isso o robo nunca conecta como nenhum dos dois, e o event trigger no
-- fim deste arquivo recusa desabilitar/rebaixar (DISABLE, ENABLE REPLICA) e dropar os triggers e funcoes
-- append-only. Consequencia: um `alembic downgrade` que os remova exige antes `DROP EVENT TRIGGER`
-- (superusuario) e reaplicar este script depois. `CREATE OR REPLACE FUNCTION forbid_modification` pelo
-- dono continua possivel (nao coberto).

\if :{?app_role} \else \set app_role cryptoinvest_app \endif
\if :{?api_role} \else \set api_role cryptoinvest_api \endif
\if :{?migrator_role} \else \set migrator_role cryptoinvest_migrator \endif
\if :{?migrator_password} \else \getenv migrator_password CRYPTOINVEST_MIGRATOR_PASSWORD \endif
\if :{?app_password} \else \getenv app_password CRYPTOINVEST_APP_PASSWORD \endif
\if :{?api_password} \else \getenv api_password CRYPTOINVEST_API_PASSWORD \endif
\if :{?migrator_password} \else \echo 'CRYPTOINVEST_MIGRATOR_PASSWORD ausente' \quit \endif
\if :{?app_password} \else \echo 'CRYPTOINVEST_APP_PASSWORD ausente' \quit \endif
\if :{?api_password} \else \echo 'CRYPTOINVEST_API_PASSWORD ausente' \quit \endif
SELECT current_database() AS db \gset

SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'migrator_role', :'migrator_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'migrator_role') \gexec
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE', :'app_role', :'app_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'app_role') \gexec
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE', :'api_role', :'api_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'api_role') \gexec
SELECT format('ALTER ROLE %I PASSWORD %L', :'migrator_role', :'migrator_password') \gexec
SELECT format('ALTER ROLE %I PASSWORD %L', :'app_role', :'app_password') \gexec
SELECT format('ALTER ROLE %I PASSWORD %L', :'api_role', :'api_password') \gexec

SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'db') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I, %I, %I', :'db', :'app_role', :'api_role', :'migrator_role') \gexec

GRANT ALL ON SCHEMA public TO :"migrator_role";
GRANT USAGE ON SCHEMA public TO :"app_role", :"api_role";
REVOKE CREATE ON SCHEMA public FROM PUBLIC, :"app_role", :"api_role";

-- ownership: tudo que o alembic criou passa ao papel de migracao (hypertables propagam aos chunks)
SELECT format('ALTER TABLE public.%I OWNER TO %I', tablename, :'migrator_role')
FROM pg_tables WHERE schemaname = 'public' \gexec
SELECT format('ALTER SEQUENCE public.%I OWNER TO %I', sequencename, :'migrator_role')
FROM pg_sequences WHERE schemaname = 'public' \gexec
ALTER FUNCTION public.forbid_modification() OWNER TO :"migrator_role";

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC, :"app_role", :"api_role";
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC, :"app_role", :"api_role";

-- imutaveis: so SELECT e INSERT
GRANT SELECT, INSERT ON
    trades, decisions, control_events, fills, risk_state, gateway_state, audit_log, equity_snapshots,
    strategy_scores
    TO :"app_role";
-- configuracao versionada: o robo so LE a ultima versao no start (quem grava e a API)
GRANT SELECT ON config_versions TO :"app_role";

-- mutaveis: UPDATE apenas nas colunas que a aplicacao de fato altera (SELECT ... FOR UPDATE o exige)
GRANT SELECT, INSERT ON runs, orders, positions, candles TO :"app_role";
GRANT UPDATE (ended_at) ON runs TO :"app_role";
GRANT UPDATE (status, exchange_order_id, filled_quantity, average_price, updated_at) ON orders TO :"app_role";
GRANT UPDATE (
    quantity, stop, take_profit, highest_price, breakeven_reached, partial_done, realized_pnl, closed_at
) ON positions TO :"app_role";
GRANT UPDATE (open, high, low, close, volume) ON candles TO :"app_role";

-- control_commands e backtest_jobs: o robo NAO os usa (recebe comandos pelo Redis assinado ou pelo
-- diretorio do run, e backtests rodam em subprocesso da API); por isso nenhum GRANT ao papel do robo.

-- ---------------------------------------------------------------------------------------------
-- Papel da API do painel
-- ---------------------------------------------------------------------------------------------
-- Leitura do diario e da configuracao (nunca escrita: o diario e do robo).
GRANT SELECT ON
    trades, decisions, control_events, fills, risk_state, gateway_state, equity_snapshots,
    runs, orders, positions, candles, strategy_scores
    TO :"api_role";
-- Auditoria e configuracao versionada: so SELECT e INSERT (append-only tambem por privilegio).
GRANT SELECT, INSERT ON audit_log, config_versions TO :"api_role";
-- Fila de comandos e jobs: a API cria a linha; depois so avanca o ESTADO (nunca reescreve o pedido).
-- `SELECT ... FOR UPDATE` exige UPDATE em ao menos uma coluna: as de estado bastam.
GRANT SELECT, INSERT ON control_commands, backtest_jobs TO :"api_role";
GRANT UPDATE (state, channel, delivered_at, resolved_at, result) ON control_commands TO :"api_role";
GRANT UPDATE (status, progress, result, error, cancel_requested, started_at, finished_at)
    ON backtest_jobs TO :"api_role";

-- Usuarios nunca sao apagados (so desabilitados); UPDATE apenas nas colunas que a API altera.
GRANT SELECT, INSERT ON users TO :"api_role";
GRANT UPDATE (
    password_hash, role, totp_secret, totp_enabled, totp_last_counter, failed_attempts, locked_until,
    password_changed_at, disabled, activation_token_hash, activation_expires_at, lock_epoch
) ON users TO :"api_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON user_sessions, recovery_codes, exchange_credentials TO :"api_role";

-- alembic_version nao e visivel ao robo (nenhum GRANT).

-- Event trigger (exige superusuario). Protege os triggers append-only (`*_append_only`, `*_no_truncate`) e
-- `forbid_modification()`, inclusive de quem e dono:
--   ddl_command_end ALTER TABLE: derruba a transacao se algum deles ficou fora de 'O' (origin) ou 'A'
--     (always), isto e, DISABLE ou ENABLE REPLICA (que o faria nao disparar em sessoes normais de replica).
--   sql_drop: derruba a transacao que dropar um deles (DROP TRIGGER, DROP FUNCTION, DROP TABLE ... CASCADE).
CREATE OR REPLACE FUNCTION public.guard_append_only_triggers() RETURNS event_trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF tg_event = 'sql_drop' THEN
        IF EXISTS (
            SELECT 1 FROM pg_event_trigger_dropped_objects() AS dropped
            WHERE (dropped.object_type = 'trigger'
                   AND (dropped.object_identity LIKE '%\_append\_only on %'
                        OR dropped.object_identity LIKE '%\_no\_truncate on %'))
               OR (dropped.object_type = 'function'
                   AND dropped.object_identity IN ('public.forbid_modification()', 'public.guard_append_only_triggers()'))
        ) THEN
            RAISE EXCEPTION 'triggers e funcoes append-only nao podem ser removidos'
                USING ERRCODE = 'restrict_violation';
        END IF;
    ELSIF EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgenabled NOT IN ('O', 'A')
          AND (tgname LIKE '%\_append\_only' OR tgname LIKE '%\_no\_truncate')
    ) THEN
        RAISE EXCEPTION 'triggers append-only nao podem ser desabilitados'
            USING ERRCODE = 'restrict_violation';
    END IF;
END;
$$;

DROP EVENT TRIGGER IF EXISTS guard_append_only_triggers;
DROP EVENT TRIGGER IF EXISTS guard_append_only_drops;
CREATE EVENT TRIGGER guard_append_only_triggers ON ddl_command_end
    WHEN TAG IN ('ALTER TABLE') EXECUTE FUNCTION public.guard_append_only_triggers();
CREATE EVENT TRIGGER guard_append_only_drops ON sql_drop
    EXECUTE FUNCTION public.guard_append_only_triggers();
