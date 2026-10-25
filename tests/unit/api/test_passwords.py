import unicodedata

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from api.security.passwords import (
    MAX_LENGTH,
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


# Texto FIXO de toda recusa possivel: qualquer interpolacao da entrada produziria uma mensagem fora deste
# conjunto. A checagem antiga ("senha not in problem") dava falso positivo quando a senha gerada coincidia
# com uma palavra da propria mensagem (ex.: 'senha', 'caracteres').
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
