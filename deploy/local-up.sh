#!/bin/sh
# CryptoInvest LOCAL (docker-compose.yml, projeto `cryptoinvest-local`). NAO e producao: ver docs/local.md.
#   ./deploy/local-up.sh up [--open-calendar]   gera segredos (1a vez), constroi e sobe tudo; mostra a URL do painel
#   ./deploy/local-up.sh down                   para tudo e PRESERVA os dados
#   ./deploy/local-up.sh reset                  apaga os volumes do projeto (pede confirmacao digitada)
#   ./deploy/local-up.sh logs [servico]         acompanha os logs
#   ./deploy/local-up.sh status                 estado dos servicos
#   ./deploy/local-up.sh create-admin [usuario] cria o administrador (senha por prompt) e imprime o token do 2FA
#   ./deploy/local-up.sh backup                 pg_dump + volume paper-data em ./backups
#   ./deploy/local-up.sh demo [--remove]        carrega (ou remove) dados FICTICIOS de demonstracao
#   ./deploy/local-up.sh secrets                so gera os segredos (sem Docker)
#   ./deploy/local-up.sh tool backtest|validate|lessons|admin [args]   ferramentas sob demanda
# Variaveis: CRYPTOINVEST_WEB_PORT (8088), CRYPTOINVEST_SECRETS_DIR (deploy/local-secrets), CRYPTOINVEST_PROJECT.
set -eu
umask 077

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT="${CRYPTOINVEST_PROJECT:-cryptoinvest-local}"
SECRETS_DIR="${CRYPTOINVEST_SECRETS_DIR:-$ROOT/deploy/local-secrets}"
PORT="${CRYPTOINVEST_WEB_PORT:-8088}"
DEMO_DB=cryptoinvest_demo
DEMO_STATE="$SECRETS_DIR/.demo-active"
export CRYPTOINVEST_SECRETS_DIR="$SECRETS_DIR" CRYPTOINVEST_WEB_PORT="$PORT"
export MSYS_NO_PATHCONV=1

die() { echo "erro: $*" >&2; exit 1; }
need_docker() { docker info > /dev/null 2>&1 || die "o Docker nao esta em execucao (abra o Docker Desktop)"; }

FILES="-f $ROOT/docker-compose.yml"
dc() { docker compose -p "$PROJECT" --project-directory "$ROOT" $FILES "$@"; }

demo_active() { [ -f "$DEMO_STATE" ]; }
if demo_active; then export CRYPTOINVEST_DB_NAME="$DEMO_DB"; fi

ensure_secrets() {
    if [ -f "$SECRETS_DIR/api_master_keys" ] && [ -f "$SECRETS_DIR/live_authorization_hmac_key" ]; then
        return
    fi
    echo "Gerando segredos locais em $SECRETS_DIR (so os que faltam; nunca sobrescreve)..."
    sh "$ROOT/deploy/secrets.sh" --out "$SECRETS_DIR" > /dev/null
    if [ ! -e "$SECRETS_DIR/live_authorization_hmac_key" ]; then
        head -c 96 /dev/urandom | base64 | tr -d '\n=+/' | head -c 64 > "$SECRETS_DIR/live_authorization_hmac_key"
    fi
    # Pasta 0700 (so o seu usuario entra); arquivos 0644 DENTRO dela: o Compose monta cada arquivo como bind e
    # nao troca dono/modo, entao os usuarios nao-root dos conteineres (10001, 70, 999) precisam conseguir le-los.
    chmod 700 "$SECRETS_DIR"
    chmod 644 "$SECRETS_DIR"/* 2> /dev/null || true
    echo "Segredos prontos. FACA COPIA de $SECRETS_DIR: sem api_master_keys os 2FA ficam irrecuperaveis."
}

db_psql() { # $1 = banco, $2 = SQL
    dc exec -T timescaledb psql -X -U cryptoinvest -d "$1" -v ON_ERROR_STOP=1 -tAc "$2"
}

print_access() {
    echo
    echo "Painel: http://localhost:$PORT   (abra no navegador; use 'localhost', nao 127.0.0.1)"
    echo "Primeiro acesso:"
    echo "  1) ./deploy/local-up.sh create-admin          (informe usuario e senha; anote o TOKEN DE ATIVACAO)"
    echo "  2) no painel, entre com usuario e senha, informe o token e leia o QR code no app autenticador"
    echo "  3) o robo esta em PAPER, com o calendario de eventos fechado: ele recusa entradas ate haver cobertura"
    echo "Para ver as telas preenchidas: ./deploy/local-up.sh demo   (dados ficticios, removiveis)"
}

cmd_up() {
    open_calendar=0
    for arg in "$@"; do
        case "$arg" in
            --open-calendar) open_calendar=1 ;;
            *) die "opcao desconhecida: $arg" ;;
        esac
    done
    need_docker
    ensure_secrets
    if [ "$open_calendar" = 1 ]; then
        echo "ATENCAO: --open-calendar e so para TESTE: o robo opera sem o calendario de eventos e o run"
        echo "         'paper-local-open' fica INELEGIVEL para o Live para sempre."
        FILES="$FILES -f $ROOT/deploy/local-open-calendar.yml"
    fi
    scale=""
    if demo_active; then
        echo "Modo demonstracao ativo: o robo fica parado e o painel le o banco $DEMO_DB (remova com: demo --remove)."
        scale="--scale runner=0"
    fi
    # shellcheck disable=SC2086
    dc up -d --build --wait $scale || die "falha ao subir. Veja: ./deploy/local-up.sh logs   (porta $PORT ocupada? defina CRYPTOINVEST_WEB_PORT)"
    print_access
}

cmd_down() { need_docker; dc --profile tools --profile demo --profile ai down --remove-orphans; echo "Parado. Os dados foram preservados."; }

cmd_reset() {
    need_docker
    echo "Isto APAGA todos os volumes do projeto '$PROJECT' (banco, Redis, diario do robo, relatorios, cache)."
    echo "Nada fora do projeto e tocado. Os segredos em $SECRETS_DIR sao mantidos."
    printf "Digite %s para confirmar: " "$PROJECT"
    read -r answer
    [ "$answer" = "$PROJECT" ] || die "confirmacao diferente; nada foi apagado"
    dc --profile tools --profile demo --profile ai down -v --remove-orphans
    rm -f "$DEMO_STATE"
    echo "Projeto $PROJECT apagado."
}

cmd_create_admin() {
    need_docker
    user="${1:-}"
    [ -n "$user" ] || { printf "Usuario: "; read -r user; }
    printf "Senha: "; stty -echo 2> /dev/null || true; read -r pass; stty echo 2> /dev/null || true; echo
    printf "Repita a senha: "; stty -echo 2> /dev/null || true; read -r again; stty echo 2> /dev/null || true; echo
    [ "$pass" = "$again" ] || die "as senhas nao conferem"
    if demo_active; then echo "Aviso: modo demonstracao ativo; este usuario valera so no banco demo."; fi
    printf '%s\n' "$pass" | dc run --rm -T admin create-admin --username "$user" --password-stdin
}

cmd_backup() {
    need_docker
    container="$(dc ps -q timescaledb)"
    [ -n "$container" ] || die "o banco nao esta em execucao (rode up)"
    TIMESCALE_CONTAINER="$container" PAPER_VOLUME="${PROJECT}_paper-data" STACK_NAME="$PROJECT" \
        BACKUP_DIR="${BACKUP_DIR:-$ROOT/backups}" sh "$ROOT/deploy/backup.sh"
}

cmd_demo() {
    need_docker
    if [ "${1:-}" = "--remove" ]; then
        demo_active || echo "A demonstracao nao estava ativa (removendo o banco demo, se existir)."
        rm -f "$DEMO_STATE"
        unset CRYPTOINVEST_DB_NAME
        dc up -d --wait
        db_psql postgres "DROP DATABASE IF EXISTS $DEMO_DB WITH (FORCE)" > /dev/null
        echo "Demonstracao removida (banco $DEMO_DB descartado); API e robo voltaram ao diario real."
        return
    fi
    demo_active && die "a demonstracao ja esta ativa (remova com: demo --remove)"
    [ -n "$(dc ps -q --status running timescaledb)" ] || die "rode 'up' antes"
    users="$(db_psql cryptoinvest "SELECT count(*) FROM users")"
    [ "$users" != "0" ] || die "crie o administrador antes (create-admin): o usuario e copiado para o banco demo"
    echo "Criando o banco separado $DEMO_DB (o diario real nunca e tocado)..."
    db_psql postgres "DROP DATABASE IF EXISTS $DEMO_DB WITH (FORCE)" > /dev/null
    db_psql postgres "CREATE DATABASE $DEMO_DB OWNER cryptoinvest" > /dev/null
    dc --profile demo run --rm demo-migrate > /dev/null
    dc exec -T timescaledb sh -c "pg_dump -U cryptoinvest -d cryptoinvest --data-only -t users -t recovery_codes | psql -X -q -U cryptoinvest -d $DEMO_DB -v ON_ERROR_STOP=1" > /dev/null
    dc --profile demo run --rm demo-seed
    : > "$DEMO_STATE"
    export CRYPTOINVEST_DB_NAME="$DEMO_DB"
    dc stop runner
    dc up -d --wait --scale runner=0
    echo
    echo "Demonstracao ATIVA em http://localhost:$PORT: dados FICTICIOS (run demo-paper, motivos com 'DEMO'), robo parado."
    echo "Entre com o seu usuario de sempre (sessoes antigas foram encerradas). Remova com: ./deploy/local-up.sh demo --remove"
}

case "${1:-}" in
    up) shift; cmd_up "$@" ;;
    down) cmd_down ;;
    reset) cmd_reset ;;
    logs) need_docker; shift; dc logs -f --tail 100 "$@" ;;
    secrets) ensure_secrets ;;
    status) need_docker; dc --profile tools --profile demo ps -a; echo "Painel: http://localhost:$PORT" ;;
    create-admin) shift; cmd_create_admin "$@" ;;
    backup) cmd_backup ;;
    demo) shift; cmd_demo "$@" ;;
    tool) need_docker; shift; svc="${1:-}"; case "$svc" in backtest|validate|lessons|admin) ;; *) die "uso: tool backtest|validate|lessons|admin [args]" ;; esac; shift; dc run --rm "$svc" "$@" ;;
    *) sed -n '2,14p' "$0"; exit 2 ;;
esac
