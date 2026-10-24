import unicodedata

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from api.security.passwords import (
    MIN_LENGTH,
    PasswordService,
    build_hasher,
    estimate_entropy_bits,
    password_problems,
)


@pytest.mark.parametrize(
    "password",
    [
        "short1!",
        "password1234",
        "Password123456",
        "qwertyuiop12",
        "aaaaaaaaaaaaaaaa",
        "123456789012",
        "abcdefghijkl",
        "senhasenha123",
        "P@ssw0rd2024",
        "ana.silva-2026",
    ],
)
def test_weak_passwords_are_rejected(password: str) -> None:
    assert password_problems(password, username="ana.silva")


@pytest.mark.parametrize(
    "password",
    ["cavalo azul voa sobre 7 montanhas!", "correct horse battery staple", "Tr0ub4dor&3-xylophone"],
)
def test_strong_passwords_are_accepted(password: str) -> None:
    assert password_problems(password, username="ana") == []


def test_length_boundary() -> None:
    assert any("12" in p for p in password_problems("x7!Qm#2pL9z"))
    assert not any("12" in p for p in password_problems("x7!Qm#2pL9zK"))


def test_password_with_username_is_rejected() -> None:
    assert any("usuario" in p for p in password_problems("zz-ana.silva-Qx9!Lm", username="ana.silva"))


def test_entropy_penalizes_repetition_and_sequences() -> None:
    assert estimate_entropy_bits("abcdefghijkl") < estimate_entropy_bits("xk7!pQ2#mZ9w")
    assert estimate_entropy_bits("aaaaaaaaaaaa") < 20


@given(st.text(max_size=200))
def test_problems_never_echo_the_password(password: str) -> None:
    for problem in password_problems(password, username="u"):
        assert password not in problem or len(password) < 3


@given(st.text(max_size=MIN_LENGTH - 1))
def test_too_short_is_never_accepted(password: str) -> None:
    assume(len(unicodedata.normalize("NFKC", password)) < MIN_LENGTH)
    assert password_problems(password)


async def test_hash_verify_and_dummy() -> None:
    service = PasswordService(build_hasher(time_cost=1, memory_kib=8, parallelism=1))
    hashed = await service.hash("cavalo azul voa sobre 7 montanhas!")
    assert hashed.startswith("$argon2id$")
    assert "cavalo" not in hashed
    assert await service.verify(hashed, "cavalo azul voa sobre 7 montanhas!")
    assert not await service.verify(hashed, "outra senha qualquer 123")
    assert not await service.verify(None, "cavalo azul voa sobre 7 montanhas!")
    assert not await service.verify("lixo", "x")


async def test_unicode_equivalent_forms_verify() -> None:
    service = PasswordService(build_hasher(time_cost=1, memory_kib=8, parallelism=1))
    hashed = await service.hash("senha-ação-forte-2026!")
    assert await service.verify(hashed, "senha-ação-forte-2026!")
    assert not await service.verify(hashed, "senha-acao-forte-2026!")
