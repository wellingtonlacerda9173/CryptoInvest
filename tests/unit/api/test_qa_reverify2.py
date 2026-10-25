"""Reverificacao (2a rodada) da seguranca da API: sondas unitarias.

Sondas de regressao (itens que eram `xfail(strict=True)` ja corrigidos na producao).
As sondas de mensagens de senha vivem em test_passwords.py.
"""

import types
from typing import Any

import pytest
from starlette.requests import Request

from api.deps import client_ip
from tests.api_support import make_settings

# ----------------------------------------------------------------------------- X-Forwarded-For


def _http_request(peer: str, forwarded_lines: list[str]) -> Request:
    raw = [(b"x-forwarded-for", line.encode()) for line in forwarded_lines]
    scope = {"type": "http", "method": "POST", "path": "/", "headers": raw, "client": (peer, 5000)}
    return Request(scope)


def _container(**overrides: Any) -> Any:
    return types.SimpleNamespace(settings=make_settings(**overrides))


def test_single_forwarded_line_is_handled_with_real_starlette_headers() -> None:
    container = _container(trust_forwarded_for=True, trusted_proxies=["10.0.0.0/8"])
    assert client_ip(_http_request("10.0.0.5", ["6.6.6.6, 198.51.100.4"]), container) == "198.51.100.4"


def test_forwarded_for_split_across_header_lines_uses_the_proxy_appended_line() -> None:
    container = _container(trust_forwarded_for=True, trusted_proxies=["10.0.0.0/8"])
    # linha 1: forjada pelo cliente; linha 2: acrescentada pelo proxy confiavel com o IP real
    request = _http_request("10.0.0.5", ["6.6.6.6", "198.51.100.4"])
    assert client_ip(request, container) == "198.51.100.4"


def test_ipv4_mapped_ipv6_peer_of_a_trusted_proxy_is_trusted() -> None:
    container = _container(trust_forwarded_for=True, trusted_proxies=["10.0.0.0/8"])
    request = _http_request("::ffff:10.0.0.5", ["198.51.100.4"])
    assert client_ip(request, container) == "198.51.100.4"


# ----------------------------------------------------------------------------- papel do banco em producao


def test_production_requires_the_dedicated_api_database_role() -> None:
    from pydantic import SecretStr

    from api.config import ApiSettings

    with pytest.raises(ValueError, match="database_user"):
        ApiSettings(
            environment="prod",
            panel_origins=["https://painel.exemplo.com"],
            hsts_max_age_seconds=3600,
            session_secret=SecretStr("x" * 40),
            master_keys=SecretStr("1:" + "A" * 43 + "="),
        )
