"""Reverificacao (2a rodada) da seguranca da API: sondas unitarias.

Sondas de regressao (itens que eram `xfail(strict=True)` ja corrigidos na producao).
"""

import types
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from starlette.requests import Request

from api.deps import client_ip
from api.security.passwords import MAX_LENGTH, MIN_LENGTH, password_problems
from tests.api_support import make_settings

# Texto FIXO de toda recusa possivel: qualquer interpolacao da entrada produziria uma mensagem fora deste
# conjunto. E a versao sem falso positivo de `test_problems_never_echo_the_password` (que reprova quando a
# senha gerada coincide com uma palavra da propria mensagem, ex.: 'senha', 'caracteres').
FIXED_MESSAGES = frozenset(
    {
        f"Use ao menos {MIN_LENGTH} caracteres.",
        f"Use no maximo {MAX_LENGTH} caracteres.",
        "Essa senha e muito comum. Escolha uma frase mais dificil de adivinhar.",
        "Essa senha e muito parecida com uma senha comum.",
        "A senha nao pode conter o nome de usuario.",
        "A senha e previsivel demais. Misture palavras, numeros e simbolos sem repeticao.",
    }
)


@settings(max_examples=300)
@given(st.text(max_size=300), st.text(max_size=20))
def test_password_problems_are_always_one_of_the_fixed_messages(password: str, username: str) -> None:
    assert set(password_problems(password, username=username)) <= FIXED_MESSAGES


@pytest.mark.parametrize("password", ["senha", "caracteres", "comum", "12", "128", "usuario", "Use"])
def test_passwords_equal_to_words_of_the_messages_do_not_change_the_messages(password: str) -> None:
    assert set(password_problems(password, username="u")) <= FIXED_MESSAGES


def test_a_unique_canary_never_appears_in_any_problem() -> None:
    canary = "zQ9xK7wV3mB"
    for candidate in (canary, canary * 30, f"{canary}-ana.silva", f"ana.silva{canary}"):
        problems = password_problems(candidate, username="ana.silva")
        assert problems
        assert all(canary not in problem and "ana.silva" not in problem for problem in problems)


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
