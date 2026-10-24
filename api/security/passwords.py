import asyncio
import math
import re
import unicodedata
from itertools import pairwise

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_LENGTH = 12
MAX_LENGTH = 128
MIN_ENTROPY_BITS = 50.0

# Lista pequena embutida (senhas mais vazadas e padroes de teclado); complementa o limite de entropia.
_COMMON = frozenset(
    [
        "password",
        "passw0rd",
        "p@ssw0rd",
        "p@ssword",
        "senha",
        "senha123",
        "senhasenha",
        "admin",
        "administrator",
        "administrador",
        "root",
        "toor",
        "qwerty",
        "qwertyuiop",
        "qwerty123",
        "asdfgh",
        "asdfghjkl",
        "zxcvbnm",
        "azerty",
        "letmein",
        "welcome",
        "welcome1",
        "login",
        "master",
        "changeme",
        "change-me",
        "trustno1",
        "iloveyou",
        "monkey",
        "dragon",
        "football",
        "baseball",
        "superman",
        "batman",
        "sunshine",
        "princess",
        "abc123",
        "abcdef",
        "abcdefgh",
        "abcd1234",
        "abcdefghijkl",
        "123456",
        "1234567",
        "12345678",
        "123456789",
        "1234567890",
        "123456789012",
        "111111",
        "000000",
        "121212",
        "654321",
        "987654321",
        "123123",
        "112233",
        "1q2w3e",
        "1q2w3e4r",
        "1q2w3e4r5t",
        "1qaz2wsx",
        "q1w2e3r4",
        "corinthians",
        "flamengo",
        "palmeiras",
        "saopaulo",
        "brasil",
        "brasil123",
        "mudar123",
        "mudar@123",
        "minhasenha",
        "cryptoinvest",
        "binance",
        "bitcoin",
        "bitcoin123",
        "ethereum",
        "cripto",
        "criptomoeda",
        "trading",
    ]
)
_SEQUENCES = ("abcdefghijklmnopqrstuvwxyz", "0123456789", "qwertyuiopasdfghjklzxcvbnm")
_LEET = str.maketrans("04135@$!7", "oaeisasit")
_hasher_cache: dict[tuple[int, int, int], PasswordHasher] = {}


def _normalize(password: str) -> str:
    return unicodedata.normalize("NFKC", password)


def _charset_size(password: str) -> int:
    size = 0
    if re.search(r"[a-z]", password):
        size += 26
    if re.search(r"[A-Z]", password):
        size += 26
    if re.search(r"\d", password):
        size += 10
    if re.search(r"[^\w\s]|_", password):
        size += 33
    if re.search(r"\s", password):
        size += 1
    if re.search(r"[^\x00-\x7f]", password):
        size += 64
    return size or 1


def _sequential_ratio(lowered: str) -> float:
    """Fracao de caracteres que continuam uma sequencia crescente/decrescente ou repetem o anterior."""
    if len(lowered) < 2:
        return 1.0
    hits = 0
    for prev, cur in pairwise(lowered):
        if prev == cur:
            hits += 1
            continue
        for seq in _SEQUENCES:
            i, j = seq.find(prev), seq.find(cur)
            if i >= 0 and j >= 0 and abs(i - j) == 1:
                hits += 1
                break
    return hits / (len(lowered) - 1)


def estimate_entropy_bits(password: str) -> float:
    """Estimativa conservadora: caracteres distintos x alfabeto, descontando repeticao e sequencias."""
    text = _normalize(password)
    if not text:
        return 0.0
    distinct = len(set(text))
    effective_length = distinct + (len(text) - distinct) * 0.25
    effective_length *= 1.0 - 0.8 * _sequential_ratio(text.lower())
    return effective_length * math.log2(_charset_size(text))


def password_problems(password: str, *, username: str = "") -> list[str]:
    """Motivos (sem eco da senha) pelos quais a senha e recusada; lista vazia = aceita."""
    text = _normalize(password)
    problems: list[str] = []
    if len(text) < MIN_LENGTH:
        problems.append(f"Use ao menos {MIN_LENGTH} caracteres.")
    if len(text) > MAX_LENGTH:
        problems.append(f"Use no maximo {MAX_LENGTH} caracteres.")
    lowered = text.lower()
    squashed = re.sub(r"[\s_.-]+", "", lowered)
    leet = squashed.translate(_LEET)
    bare = re.sub(r"\d+$", "", squashed)
    if {squashed, leet, bare, re.sub(r"\d+$", "", leet)} & _COMMON:
        problems.append("Essa senha e muito comum. Escolha uma frase mais dificil de adivinhar.")
    elif any(len(word) >= 6 and word in squashed and len(squashed) - len(word) <= 6 for word in _COMMON):
        problems.append("Essa senha e muito parecida com uma senha comum.")
    if username and len(username) >= 3 and username.lower() in lowered:
        problems.append("A senha nao pode conter o nome de usuario.")
    if not problems and estimate_entropy_bits(text) < MIN_ENTROPY_BITS:
        problems.append("A senha e previsivel demais. Misture palavras, numeros e simbolos sem repeticao.")
    return problems


def build_hasher(*, time_cost: int, memory_kib: int, parallelism: int) -> PasswordHasher:
    key = (time_cost, memory_kib, parallelism)
    if key not in _hasher_cache:
        _hasher_cache[key] = PasswordHasher(
            time_cost=time_cost, memory_cost=memory_kib, parallelism=parallelism, type=Type.ID
        )
    return _hasher_cache[key]


class PasswordService:
    """Argon2id em thread separada para nao bloquear o loop de eventos."""

    def __init__(self, hasher: PasswordHasher) -> None:
        self._hasher = hasher
        # Hash de uma senha aleatoria descartada: gasta o mesmo tempo quando o usuario nao existe.
        self._dummy = hasher.hash("dummy-" + "x" * 24)

    async def hash(self, password: str) -> str:
        return await asyncio.to_thread(self._hasher.hash, _normalize(password))

    async def verify(self, stored_hash: str | None, password: str) -> bool:
        target = stored_hash or self._dummy
        try:
            ok = await asyncio.to_thread(self._hasher.verify, target, _normalize(password))
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False
        return ok and stored_hash is not None

    def needs_rehash(self, stored_hash: str) -> bool:
        return self._hasher.check_needs_rehash(stored_hash)
