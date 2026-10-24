"""Senha do Redis vinda de arquivo secreto (`redis_password` em SECRETS_DIR), nunca de variavel de ambiente.

Sem o arquivo (ou com senha ja na URL) a URL passa intacta: o dev atual, sem `requirepass`, continua igual.
O Redis carrega estado e comandos do robo; alem da senha, mantenha-o em rede restrita (loopback/VLAN do
runner e da API), pois as mensagens ja sao assinadas mas a disponibilidade depende do acesso a porta.
"""

from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

PASSWORD_FILE = "redis_password"  # noqa: S105 - nome do arquivo, nao o segredo


def read_redis_password(secrets_dir: Path | None) -> str | None:
    if secrets_dir is None:
        return None
    try:
        value = (secrets_dir / PASSWORD_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def with_redis_password(url: str, secrets_dir: Path | None) -> str:
    password = read_redis_password(secrets_dir)
    if password is None:
        return url
    parts = urlsplit(url)
    if parts.password is not None or not parts.hostname:
        return url
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    port = f":{parts.port}" if parts.port else ""
    user = quote(parts.username, safe="") if parts.username else ""
    netloc = f"{user}:{quote(password, safe='')}@{host}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
