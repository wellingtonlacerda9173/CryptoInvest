#!/bin/sh
# Job one-shot de migracao: alembic upgrade head -> scripts/db_roles.sql -> api.cli check-grants.
# Senhas SO de arquivos (Docker secrets em /run/secrets): nunca em argv, log ou variavel exportada ao job.
set -eu
umask 077

SECRETS="${MIGRATE_SECRETS_DIR:-/run/secrets}"
HOST="${DATABASE_HOST:-timescaledb}"
PORT="${DATABASE_PORT:-5432}"
DB="${DATABASE_NAME:-cryptoinvest}"
SUPERUSER="${DATABASE_SUPERUSER:-cryptoinvest}"
MIGRATOR_ROLE="${DATABASE_MIGRATOR_ROLE:-cryptoinvest_migrator}"
APP_ROLE="${DATABASE_APP_ROLE:-cryptoinvest_app}"
API_ROLE="${DATABASE_API_ROLE:-cryptoinvest_api}"
WAIT_TRIES="${MIGRATE_WAIT_TRIES:-60}"

WORK="$(mktemp -d /tmp/migrate.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

secret() { tr -d '\r\n' < "$SECRETS/$1"; }
log() { printf '{"event":"migrate","step":"%s"}\n' "$1"; }

for name in postgres_password migrator_password app_password api_database_password; do
    [ -s "$SECRETS/$name" ] || { echo "secret ausente ou vazio: $name" >&2; exit 1; }
done

escape_pgpass() { printf '%s' "$1" | sed 's/[\\:]/\\&/g'; }
{
    printf '%s:%s:%s:%s:%s\n' "$HOST" "$PORT" "$DB" "$SUPERUSER" "$(escape_pgpass "$(secret postgres_password)")"
} > "$WORK/pgpass"
export PGPASSFILE="$WORK/pgpass" PGHOST="$HOST" PGPORT="$PORT" PGUSER="$SUPERUSER" PGDATABASE="$DB"

log wait_database
tries=0
until pg_isready -q -t 3; do
    tries=$((tries + 1))
    [ "$tries" -lt "$WAIT_TRIES" ] || { echo "banco indisponivel" >&2; exit 1; }
    sleep 2
done

# Primeira implantacao e pos-restauracao (pg_restore --no-owner): as tabelas ainda nao pertencem ao migrador e o
# alembic roda como o superusuario inicial; depois do db_roles.sql o migrador e dono e passa a ser o unico a rodar o alembic.
owned="$(psql -X -tA -c "SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = 'alembic_version' AND tableowner = '$MIGRATOR_ROLE'")"
if [ "$owned" = "1" ]; then
    alembic_user="$MIGRATOR_ROLE"
    alembic_secret=migrator_password
else
    alembic_user="$SUPERUSER"
    alembic_secret=postgres_password
fi
mkdir "$WORK/alembic-secrets"
ln -s "$SECRETS/$alembic_secret" "$WORK/alembic-secrets/database_password"

log alembic_upgrade
SECRETS_DIR="$WORK/alembic-secrets" DATABASE_USER="$alembic_user" DATABASE_HOST="$HOST" \
    DATABASE_PORT="$PORT" DATABASE_NAME="$DB" alembic upgrade head

log db_roles
CRYPTOINVEST_MIGRATOR_PASSWORD="$(secret migrator_password)" \
CRYPTOINVEST_APP_PASSWORD="$(secret app_password)" \
CRYPTOINVEST_API_PASSWORD="$(secret api_database_password)" \
    psql -X -q -v ON_ERROR_STOP=1 -v "migrator_role=$MIGRATOR_ROLE" -v "app_role=$APP_ROLE" \
    -v "api_role=$API_ROLE" -f /app/scripts/db_roles.sql

# A verificacao nao usa as chaves reais da API: so precisa de settings validos e das credenciais do banco
mkdir "$WORK/check-secrets"
ln -s "$SECRETS/app_password" "$WORK/check-secrets/database_password"
ln -s "$SECRETS/api_database_password" "$WORK/check-secrets/api_database_password"
head -c 48 /dev/urandom | base64 | tr -d '\n=+/' > "$WORK/check-secrets/api_session_secret"
printf '1:%s\n' "$(head -c 32 /dev/urandom | base64)" > "$WORK/check-secrets/api_master_keys"

log check_grants
SECRETS_DIR="$WORK/check-secrets" DATABASE_USER="$APP_ROLE" DATABASE_HOST="$HOST" DATABASE_PORT="$PORT" \
    DATABASE_NAME="$DB" API_DATABASE_USER="$API_ROLE" API_ENVIRONMENT=dev \
    python -m api.cli check-grants
log done
