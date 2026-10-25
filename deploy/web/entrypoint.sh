#!/bin/sh
# Renderiza a configuracao do proxy a partir do ambiente e inicia o nginx. A raiz da imagem e somente leitura:
# tudo o que e gerado vai para /tmp/nginx-conf (tmpfs).
#   PANEL_DOMAIN     obrigatorio: unico Host aceito (server_name); qualquer outro recebe 444
#   TRUSTED_LB_CIDR  obrigatorio no alvo `web` (HTTP atras de balanceador): CIDRs (separados por virgula ou espaco) do
#                    balanceador cujo X-Forwarded-For e confiavel. No alvo `web-tls` o cliente conecta direto: sem real_ip.
set -eu

OUT=/tmp/nginx-conf
mkdir -p "$OUT"

die() { echo "web: $1" >&2; exit 1; }

[ -n "${PANEL_DOMAIN:-}" ] || die "defina PANEL_DOMAIN (dominio do painel, ex.: painel.exemplo.com)"
case "$PANEL_DOMAIN" in
    *[!A-Za-z0-9.-]* | .* | *..*) die "PANEL_DOMAIN invalido: use apenas o nome do host, sem esquema, porta ou barras" ;;
esac

: > "$OUT/real_ip.conf"
if [ "${WEB_MODE:-http}" = "http" ]; then
    [ -n "${TRUSTED_LB_CIDR:-}" ] || die "defina TRUSTED_LB_CIDR (IP/CIDR do balanceador que termina o TLS); sem ele todos os clientes aparecem com o IP do balanceador e o limite por IP deixa de funcionar"
    for cidr in $(printf '%s' "$TRUSTED_LB_CIDR" | tr ',' ' '); do
        printf '%s' "$cidr" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}(/[0-9]{1,2})?$|^[0-9A-Fa-f:]+(/[0-9]{1,3})?$' \
            || die "TRUSTED_LB_CIDR invalido: $cidr"
        case "$cidr" in
            */0 | */00 | */000) die "TRUSTED_LB_CIDR com mascara /0 confia no X-Forwarded-For de QUALQUER origem: $cidr" ;;
        esac
        printf 'set_real_ip_from %s;\n' "$cidr" >> "$OUT/real_ip.conf"
    done
    printf 'real_ip_header X-Forwarded-For;\nreal_ip_recursive on;\n' >> "$OUT/real_ip.conf"
fi

envsubst '${PANEL_DOMAIN}' < /etc/nginx/templates/server.conf.template > "$OUT/web.server.conf"
nginx -t -q || die "configuracao do nginx invalida"
exec nginx -g 'daemon off;'
