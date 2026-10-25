#!/bin/sh
# Restauracao.
#   ./deploy/restore.sh verify BACKUP_DIR     restaura num Timescale DESCARTAVEL (sem rede, removido ao fim) e confere
#                                             alembic_version e contagens por tabela contra o manifesto do backup
#   ./deploy/restore.sh apply BACKUP_DIR --container NOME --confirm-database NOME_DO_BANCO [--i-stopped-services]
#                                             troca o banco por uma versao restaurada e VALIDADA (api/runner com 0 replicas)
# Apply, em ordem: checksum + pg_restore --list -> confirmacao digitada -> servicos parados -> pg_dump de seguranca ->
# restauracao num banco temporario <db>_restore_<ts> -> validacao -> troca por renomeacao (o antigo vira <db>_old_<ts>
# e so e descartado por voce). Qualquer divergencia aborta (exit != 0) com o banco original intacto.
# Opcoes: --allow-drift (aceita contagens diferentes: backup feito com o sistema em uso); --i-stopped-services (pula a
# checagem de replicas, p.ex. fora do Swarm). Variaveis: SAFETY_DIR (./backups/pre-restore-<ts>) STACK_NAME DB_NAME DB_USER.
set -eu
umask 077
export MSYS_NO_PATHCONV=1

TOOLS_IMAGE="${TOOLS_IMAGE:-timescale/timescaledb:2.30.2-pg17}"
STACK_NAME="${STACK_NAME:-cryptoinvest}"
DB_NAME="${DB_NAME:-cryptoinvest}"
DB_USER="${DB_USER:-cryptoinvest}"
COUNTS_SQL="SELECT table_name || '|' || (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I.%I', table_schema, table_name), false, true, '')))[1]::text FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name"

MODE="${1:-}"
BACKUP="${2:-}"
[ -n "$MODE" ] && [ -n "$BACKUP" ] || { sed -n 2,13p "$0" >&2; exit 2; }
shift 2
CONTAINER=""
CONFIRM=""
DRIFT=0
STOPPED=0
while [ $# -gt 0 ]; do
    case "$1" in
        --container) CONTAINER="${2:-}"; shift 2 ;;
        --confirm-database) CONFIRM="${2:-}"; shift 2 ;;
        --allow-drift) DRIFT=1; shift ;;
        --i-stopped-services) STOPPED=1; shift ;;
        *) echo "opcao desconhecida: $1" >&2; exit 2 ;;
    esac
done
case "$DB_NAME" in *[!a-z0-9_]* | "") echo "DB_NAME invalido: $DB_NAME" >&2; exit 2 ;; esac
[ -f "$BACKUP/db.dump" ] || { echo "db.dump nao encontrado em $BACKUP" >&2; exit 1; }
(cd "$BACKUP" && sha256sum -c SHA256SUMS > /dev/null) || { echo "checksum do backup nao confere" >&2; exit 1; }

fail() { echo "FALHA: $1" >&2; exit 1; }

ERRFILE="$(mktemp)"
COUNTSFILE="$(mktemp)"
trap 'rm -f "$ERRFILE" "$COUNTSFILE"' EXIT

# Restauracao estrita: o pg_restore nao pode ter erro algum. A extensao timescaledb ja existe no banco de destino
# (template1/CREATE EXTENSION), entao suas entradas saem da lista; qualquer outro erro aborta.
load_dump() { # container usuario banco
    docker cp "$BACKUP/db.dump" "$1:/tmp/restore.dump"
    docker exec "$1" psql -X -U "$2" -d "$3" -v ON_ERROR_STOP=1 -q -c "CREATE EXTENSION IF NOT EXISTS timescaledb" -c "SELECT timescaledb_pre_restore()" > /dev/null
    docker exec "$1" sh -c "pg_restore --list /tmp/restore.dump | grep -v ' EXTENSION ' > /tmp/restore.list"
    if ! docker exec "$1" pg_restore -U "$2" -d "$3" --no-owner --no-acl --exit-on-error -L /tmp/restore.list /tmp/restore.dump 2> "$ERRFILE"; then
        tail -n 5 "$ERRFILE" >&2
        docker exec "$1" rm -f /tmp/restore.dump /tmp/restore.list
        return 1
    fi
    docker exec "$1" psql -X -U "$2" -d "$3" -v ON_ERROR_STOP=1 -q -c "SELECT timescaledb_post_restore()" > /dev/null
    docker exec "$1" rm -f /tmp/restore.dump /tmp/restore.list
}

check_restored() { # container usuario banco
    restored_version="$(docker exec "$1" psql -X -U "$2" -d "$3" -tA -c 'SELECT version_num FROM alembic_version')" || return 1
    expected_version="$(cat "$BACKUP/alembic_version.txt")"
    echo "alembic current: restaurado=$restored_version backup=$expected_version"
    [ "$restored_version" = "$expected_version" ] || { echo "versao do alembic diverge" >&2; return 1; }
    docker exec "$1" psql -X -U "$2" -d "$3" -tA -c "$COUNTS_SQL" > "$COUNTSFILE" || return 1
    if diff "$BACKUP/counts.txt" "$COUNTSFILE" > /dev/null; then
        echo "contagens: $(wc -l < "$BACKUP/counts.txt" | tr -d ' ') tabelas conferem"
        return 0
    fi
    diff "$BACKUP/counts.txt" "$COUNTSFILE" >&2 || true
    if [ "$DRIFT" = 1 ] && [ "$(cut -d'|' -f1 "$BACKUP/counts.txt")" = "$(cut -d'|' -f1 "$COUNTSFILE")" ]; then
        echo "AVISO: contagens diferem (--allow-drift); as tabelas conferem" >&2
        return 0
    fi
    echo "contagens divergem do manifesto" >&2
    return 1
}

case "$MODE" in
verify)
    name="ci-restore-verify-$$"
    trap 'docker rm -f -v "$name" > /dev/null 2>&1 || true; rm -f "$ERRFILE" "$COUNTSFILE"' EXIT
    docker run -d --name "$name" --network none -e POSTGRES_PASSWORD="$(head -c 24 /dev/urandom | base64 | tr -d '\n=+/')" \
        -e POSTGRES_DB=verify "$TOOLS_IMAGE" > /dev/null
    tries=0
    until docker logs "$name" 2>&1 | grep -q "init process complete" && docker exec "$name" pg_isready -q -U postgres -d verify; do
        tries=$((tries + 1))
        [ "$tries" -lt 60 ] || fail "banco descartavel nao subiu"
        sleep 2
    done
    load_dump "$name" postgres verify || fail "pg_restore reportou erro"
    check_restored "$name" postgres verify || fail "o backup restaurado nao confere com o manifesto"
    echo "restauracao verificada"
    ;;
apply)
    [ -n "$CONTAINER" ] || { echo "informe --container" >&2; exit 2; }
    [ -f "$BACKUP/alembic_version.txt" ] && [ -f "$BACKUP/counts.txt" ] || fail "alembic_version.txt/counts.txt ausentes no backup"
    [ "$CONFIRM" = "$DB_NAME" ] || { echo "confirmacao ausente ou errada: digite --confirm-database $DB_NAME (nome do banco que sera SUBSTITUIDO)" >&2; exit 2; }
    stamp="$(date -u +%Y%m%d%H%M%S)"
    tmp_db="${DB_NAME}_restore_${stamp}"
    old_db="${DB_NAME}_old_${stamp}"
    psql_admin() { docker exec "$CONTAINER" psql -X -U "$DB_USER" -d postgres -v ON_ERROR_STOP=1 -q "$@"; }

    docker cp "$BACKUP/db.dump" "$CONTAINER:/tmp/verify.dump"
    docker exec "$CONTAINER" pg_restore --list /tmp/verify.dump > /dev/null 2>&1 \
        || { docker exec "$CONTAINER" rm -f /tmp/verify.dump; fail "db.dump nao e um dump valido (pg_restore --list). Nada foi alterado."; }
    docker exec "$CONTAINER" rm -f /tmp/verify.dump

    if [ "$STOPPED" != 1 ]; then
        for svc in api runner; do
            line="$(docker service ls --filter "name=${STACK_NAME}_$svc" --format '{{.Name}} {{.Replicas}}' 2> /dev/null | grep "^${STACK_NAME}_$svc " || true)"
            [ -n "$line" ] && [ "${line#* }" = "0/0" ] \
                || fail "${STACK_NAME}_$svc nao esta com 0 replicas (docker service scale ${STACK_NAME}_api=0 ${STACK_NAME}_runner=0) ou passe --i-stopped-services. Nada foi alterado."
        done
    fi

    safety="${SAFETY_DIR:-./backups/pre-restore-$stamp}"
    mkdir -p "$safety"
    docker exec "$CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc -Z 6 --no-owner --no-acl -f /tmp/safety.dump \
        || fail "pg_dump de seguranca falhou. Nada foi alterado."
    docker cp "$CONTAINER:/tmp/safety.dump" "$safety/db.dump"
    docker exec "$CONTAINER" rm -f /tmp/safety.dump
    (cd "$safety" && sha256sum ./db.dump > SHA256SUMS)
    echo "dump de seguranca do banco atual: $safety (SHA256 em SHA256SUMS)"

    drop_tmp() { psql_admin -c "DROP DATABASE IF EXISTS $tmp_db WITH (FORCE)" > /dev/null 2>&1 || true; }
    trap 'drop_tmp; rm -f "$ERRFILE" "$COUNTSFILE"' EXIT
    abort() {
        drop_tmp
        fail "$1 O banco $DB_NAME NAO foi alterado. Dump de seguranca em $safety."
    }
    psql_admin -c "CREATE DATABASE $tmp_db" || abort "nao foi possivel criar o banco temporario."
    load_dump "$CONTAINER" "$DB_USER" "$tmp_db" || abort "pg_restore reportou erro no banco temporario."
    check_restored "$CONTAINER" "$DB_USER" "$tmp_db" || abort "o banco restaurado nao confere com o manifesto do backup."

    psql_admin -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('$DB_NAME', '$tmp_db') AND pid <> pg_backend_pid()" > /dev/null \
        || abort "nao foi possivel encerrar conexoes."
    psql_admin -c "ALTER DATABASE $DB_NAME RENAME TO $old_db" || abort "nao foi possivel renomear o banco atual (conexoes ativas?)."
    if ! psql_admin -c "ALTER DATABASE $tmp_db RENAME TO $DB_NAME"; then
        psql_admin -c "ALTER DATABASE $old_db RENAME TO $DB_NAME" \
            || fail "troca falhou e a reversao tambem: rode ALTER DATABASE $old_db RENAME TO $DB_NAME no postgres."
        fail "troca falhou; o banco original foi restaurado ao nome $DB_NAME."
    fi
    trap 'rm -f "$ERRFILE" "$COUNTSFILE"' EXIT
    echo "banco $DB_NAME restaurado e validado. O anterior foi mantido como $old_db."
    echo "Proximo passo: rode o job migrate (reaplica db_roles.sql e confere permissoes) e suba api/runner."
    echo "Para REVERTER (api/runner parados): ALTER DATABASE $DB_NAME RENAME TO ${DB_NAME}_failed_$stamp; ALTER DATABASE $old_db RENAME TO $DB_NAME"
    echo "Depois de confirmar o resultado, descarte o antigo manualmente: DROP DATABASE $old_db"
    ;;
*)
    echo "modo desconhecido: $MODE" >&2
    exit 2
    ;;
esac
