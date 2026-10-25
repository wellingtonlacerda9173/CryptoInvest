#!/bin/sh
# Backup: pg_dump comprimido (formato custom) do Timescale + copia do volume data/paper + manifesto de verificacao.
#   ./deploy/backup.sh                       (no no do Swarm que hospeda o timescaledb)
# Variaveis: BACKUP_DIR (./backups) RETENTION_DAYS (14) TIMESCALE_CONTAINER (autodeteccao do servico
# ${STACK_NAME}_timescaledb) PAPER_VOLUME (${STACK_NAME}_paper-data) DB_NAME DB_USER TOOLS_IMAGE.
# Nao usa senha: o pg_dump roda DENTRO do container pelo socket local.
set -eu
umask 077
export MSYS_NO_PATHCONV=1

STACK_NAME="${STACK_NAME:-cryptoinvest}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
DB_NAME="${DB_NAME:-cryptoinvest}"
DB_USER="${DB_USER:-cryptoinvest}"
PAPER_VOLUME="${PAPER_VOLUME:-${STACK_NAME}_paper-data}"
TOOLS_IMAGE="${TOOLS_IMAGE:-timescale/timescaledb:2.30.2-pg17}"
CONTAINER="${TIMESCALE_CONTAINER:-$(docker ps -q -f "label=com.docker.swarm.service.name=${STACK_NAME}_timescaledb" | head -n 1)}"
[ -n "$CONTAINER" ] || { echo "container do timescaledb nao encontrado (defina TIMESCALE_CONTAINER)" >&2; exit 1; }

COUNTS_SQL="SELECT table_name || '|' || (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I.%I', table_schema, table_name), false, true, '')))[1]::text FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
dest="$BACKUP_DIR/$stamp"
mkdir -p "$dest"
ok=0
cleanup() {
    docker exec "$CONTAINER" rm -f /tmp/backup.dump > /dev/null 2>&1 || true
    [ "$ok" = 1 ] || rm -rf "$dest"
}
trap cleanup EXIT

psql_in() { docker exec "$CONTAINER" psql -X -U "$DB_USER" -d "$DB_NAME" -tA -c "$1"; }

psql_in "SELECT version_num FROM alembic_version" > "$dest/alembic_version.txt"
psql_in "$COUNTS_SQL" > "$dest/counts.txt"
docker exec "$CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc -Z 6 --no-owner --no-acl -f /tmp/backup.dump
docker cp "$CONTAINER:/tmp/backup.dump" "$dest/db.dump"

if docker volume inspect "$PAPER_VOLUME" > /dev/null 2>&1; then
    docker run --rm --network none -v "$PAPER_VOLUME:/data:ro" --entrypoint tar "$TOOLS_IMAGE" czf - -C /data . > "$dest/paper-data.tar.gz"
else
    echo "aviso: volume $PAPER_VOLUME nao existe; data/paper nao incluido" >&2
fi

(cd "$dest" && sha256sum ./* > SHA256SUMS)
ok=1
find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -name '2*T*Z' -mtime "+$RETENTION_DAYS" -exec rm -rf {} +
echo "backup ok: $dest"
