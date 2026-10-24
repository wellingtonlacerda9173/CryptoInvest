"""QA final: matriz adversarial de X-Forwarded-For / IPv4 mapeado / IPv6 rotativo."""

import types
from typing import Any

import pytest
from starlette.requests import Request

from api.deps import client_ip
from api.security.throttle import throttle_ip
from tests.api_support import make_settings


def _req(peer: str | None, lines: list[str]) -> Request:
    scope = {
        "type": "http", "method": "POST", "path": "/",
        "headers": [(b"x-forwarded-for", line.encode()) for line in lines],
        "client": (peer, 5000) if peer else None,
    }  # fmt: skip
    return Request(scope)


def _c(**kw: Any) -> Any:
    return types.SimpleNamespace(
        settings=make_settings(trust_forwarded_for=True, trusted_proxies=["10.0.0.0/8", "fd00::/8"], **kw)
    )


@pytest.mark.parametrize(
    ("peer", "lines", "expected"),
    [
        ("10.0.0.5", ["6.6.6.6", "7.7.7.7", "198.51.100.4"], "198.51.100.4"),
        ("10.0.0.5", ["6.6.6.6, 10.1.1.1", "198.51.100.4, 10.2.2.2"], "198.51.100.4"),
        ("10.0.0.5", ["", "  ", "198.51.100.4"], "198.51.100.4"),
        ("10.0.0.5", ["198.51.100.4,,"], "198.51.100.4"),
        ("10.0.0.5", ["6.6.6.6, ::ffff:198.51.100.4"], "198.51.100.4"),
        ("10.0.0.5", ["198.51.100.4, ::ffff:10.1.1.1"], "198.51.100.4"),
        ("::ffff:10.0.0.5", ["6.6.6.6, 198.51.100.4"], "198.51.100.4"),
        ("fd00::5", ["6.6.6.6, 2001:db8::7"], "2001:db8::7"),
        (
            "10.0.0.5",
            ["6.6.6.6, 198.51.100.4:8080"],
            "10.0.0.5",
        ),  # salto invalido: cai no IP do proxy (fail-safe)
        ("10.0.0.5", ["6.6.6.6;198.51.100.4"], "10.0.0.5"),
        ("10.0.0.5", ["10.1.1.1, 10.2.2.2"], "10.0.0.5"),  # so proxies
        ("203.0.113.9", ["10.0.0.5, 6.6.6.6"], "203.0.113.9"),  # par nao confiavel
        ("::ffff:203.0.113.9", ["6.6.6.6"], "203.0.113.9"),
        ("10.0.0.5", ["unknown, 198.51.100.4"], "198.51.100.4"),
        ("10.0.0.5", ["198.51.100.4, unknown"], "10.0.0.5"),
    ],
)
def test_client_ip_matrix(peer: str, lines: list[str], expected: str) -> None:
    assert client_ip(_req(peer, lines), _c()) == expected


def test_a_rotating_ipv6_64_is_one_throttle_key_and_mapped_ipv4_equals_ipv4() -> None:
    keys = {throttle_ip(f"2001:db8:1:2:{i:x}:{i * 7:x}:1:2") for i in range(2000)}
    assert keys == {"2001:db8:1:2::/64"}
    assert throttle_ip("::ffff:198.51.100.4") == throttle_ip("198.51.100.4") == "198.51.100.4"
    assert throttle_ip("2001:db8:1:3::1") != throttle_ip("2001:db8:1:2::1")
