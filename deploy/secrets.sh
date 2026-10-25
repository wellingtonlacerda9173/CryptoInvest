#!/bin/sh
# Gera secrets aleatorios fortes e os cria no Swarm (docker secret create, valor por stdin: nunca em argv/log).
#   ./deploy/secrets.sh                 cria os que faltarem (nunca sobrescreve nem imprime valores)
#   ./deploy/secrets.sh --out DIR       dry-run: grava arquivos 0600 em DIR em vez de chamar o docker
# Opcoes: --prefix NOME (padrao cryptoinvest; o stack usa SECRET_PREFIX).
set -eu
umask 077

PREFIX="${SECRET_PREFIX:-cryptoinvest}"
OUT=""
while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) PREFIX="$2"; shift 2 ;;
        --out) OUT="$2"; shift 2 ;;
        *) echo "uso: secrets.sh [--prefix NOME] [--out DIR]" >&2; exit 2 ;;
    esac
done

token() { head -c 96 /dev/urandom | base64 | tr -d '\n=+/' | head -c "$1"; }
master_key() { printf '1:%s\n' "$(head -c 32 /dev/urandom | base64)"; }

generate() {
    case "$1" in
        api_master_keys) master_key ;;
        *_password) token 40 ;;
        *) token 64 ;;
    esac
}

NAMES="postgres_password migrator_password app_password api_database_password redis_password api_session_secret api_recovery_hmac_key command_hmac_key api_master_keys"

[ -z "$OUT" ] || mkdir -p "$OUT"
for name in $NAMES; do
    if [ -n "$OUT" ]; then
        [ -e "$OUT/$name" ] && { echo "existe: $name (mantido)"; continue; }
        generate "$name" > "$OUT/$name"
        chmod 600 "$OUT/$name"
    else
        if docker secret inspect "${PREFIX}_$name" > /dev/null 2>&1; then
            echo "existe: ${PREFIX}_$name (mantido)"
            continue
        fi
        generate "$name" | docker secret create "${PREFIX}_$name" - > /dev/null
    fi
    echo "criado: $name"
done
