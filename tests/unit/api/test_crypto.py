import base64

import pytest
from hypothesis import given
from hypothesis import strategies as st

from api.security.crypto import DecryptionError, KeyRing, KeyringError, generate_key_line
from tests.api_support import MASTER_KEY_V1, MASTER_KEY_V2


def ring(*versions: tuple[int, str]) -> KeyRing:
    return KeyRing.parse("\n".join(f"{v}:{k}" for v, k in versions))


def test_round_trip_and_unique_nonce() -> None:
    keys = ring((1, MASTER_KEY_V1))
    first = keys.encrypt("segredo-da-binance", aad="cred:1")
    second = keys.encrypt("segredo-da-binance", aad="cred:1")
    assert first != second
    assert "segredo-da-binance" not in first
    assert keys.decrypt(first, aad="cred:1") == "segredo-da-binance"
    assert first.startswith("enc:v1:")


def test_tampered_ciphertext_is_rejected() -> None:
    keys = ring((1, MASTER_KEY_V1))
    envelope = keys.encrypt("valor", aad="a")
    head, _, blob = envelope.rpartition(":")
    raw = bytearray(base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4)))
    raw[-1] ^= 0x01
    forged = f"{head}:{base64.urlsafe_b64encode(bytes(raw)).decode().rstrip('=')}"
    with pytest.raises(DecryptionError):
        keys.decrypt(forged, aad="a")


def test_wrong_aad_is_rejected() -> None:
    keys = ring((1, MASTER_KEY_V1))
    envelope = keys.encrypt("valor", aad="credential:1:api_key")
    with pytest.raises(DecryptionError):
        keys.decrypt(envelope, aad="credential:2:api_key")


@pytest.mark.parametrize("garbage", ["", "enc:v1:", "enc:v1:abc", "plain", "enc:v9:AAAA", "enc:v1:!!!!"])
def test_garbage_envelopes(garbage: str) -> None:
    with pytest.raises(DecryptionError):
        ring((1, MASTER_KEY_V1)).decrypt(garbage, aad="a")


def test_key_rotation_keeps_old_versions_readable() -> None:
    old = ring((1, MASTER_KEY_V1))
    envelope = old.encrypt("antigo", aad="a")
    both = ring((1, MASTER_KEY_V1), (2, MASTER_KEY_V2))
    assert both.active_version == 2
    assert both.needs_rotation(envelope)
    assert both.decrypt(envelope, aad="a") == "antigo"
    renewed = both.encrypt(both.decrypt(envelope, aad="a"), aad="a")
    assert renewed.startswith("enc:v2:")
    assert not both.needs_rotation(renewed)
    only_new = ring((2, MASTER_KEY_V2))
    assert only_new.decrypt(renewed, aad="a") == "antigo"
    with pytest.raises(DecryptionError):
        only_new.decrypt(envelope, aad="a")


def test_wrong_master_key_fails() -> None:
    envelope = ring((1, MASTER_KEY_V1)).encrypt("x", aad="a")
    with pytest.raises(DecryptionError):
        ring((1, MASTER_KEY_V2)).decrypt(envelope, aad="a")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "1",
        "x:AAAA",
        "0:" + MASTER_KEY_V1,
        "1:not-base64!!",
        "1:AAAA",
        f"1:{MASTER_KEY_V1}\n1:{MASTER_KEY_V2}",
    ],
)
def test_invalid_keyring_files(text: str) -> None:
    with pytest.raises(KeyringError):
        KeyRing.parse(text)


def test_keyring_ignores_comments_and_never_prints_keys() -> None:
    keys = KeyRing.parse(f"# dev\n\n1:{MASTER_KEY_V1}\n")
    assert MASTER_KEY_V1 not in repr(keys)


def test_generated_key_line_is_valid() -> None:
    assert KeyRing.parse(generate_key_line(3)).active_version == 3


@given(st.text(max_size=300), st.text(max_size=80))
def test_round_trip_property(plaintext: str, aad: str) -> None:
    keys = ring((1, MASTER_KEY_V1))
    assert keys.decrypt(keys.encrypt(plaintext, aad=aad), aad=aad) == plaintext
