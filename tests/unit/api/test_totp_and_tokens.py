import uuid
from datetime import UTC, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from api.security import totp
from api.security.throttle import FailureThrottle, lockout_seconds
from api.security.tokens import (
    csrf_token,
    hash_recovery_code,
    hash_token,
    new_recovery_codes,
    new_session_token,
    normalize_recovery_code,
)

NOW = datetime(2026, 10, 20, 12, 0, 10, tzinfo=UTC)


def test_current_code_is_accepted_and_window_is_plus_minus_one() -> None:
    secret = totp.new_secret()
    for delta, expected in ((-30, True), (0, True), (30, True), (-60, False), (60, False), (120, False)):
        code = totp.current_code(secret, NOW + timedelta(seconds=delta))
        assert (totp.match_counter(secret, code, NOW, last_counter=None) is not None) is expected


def test_replay_of_same_or_older_counter_is_rejected() -> None:
    secret = totp.new_secret()
    code = totp.current_code(secret, NOW)
    counter = totp.match_counter(secret, code, NOW, last_counter=None)
    assert counter == totp.counter_at(NOW)
    assert totp.match_counter(secret, code, NOW, last_counter=counter) is None
    older = totp.current_code(secret, NOW - timedelta(seconds=30))
    assert totp.match_counter(secret, older, NOW, last_counter=counter) is None
    newer = totp.current_code(secret, NOW + timedelta(seconds=30))
    assert totp.match_counter(secret, newer, NOW, last_counter=counter) == counter + 1


def test_malformed_codes_are_rejected() -> None:
    secret = totp.new_secret()
    for bad in ("", "12345", "1234567", "abcdef", "١٢٣٤٥٦", "12 345"):
        assert totp.match_counter(secret, bad, NOW, last_counter=None) is None


@given(st.text(max_size=12))
def test_random_text_never_matches_unless_valid(candidate: str) -> None:
    secret = "JBSWY3DPEHPK3PXP"
    valid = {totp.current_code(secret, NOW + timedelta(seconds=d)) for d in (-30, 0, 30)}
    matched = totp.match_counter(secret, candidate, NOW, last_counter=None)
    assert (matched is not None) == (candidate.strip().replace(" ", "") in valid)


def test_provisioning_uri_has_issuer() -> None:
    uri = totp.provisioning_uri("JBSWY3DPEHPK3PXP", username="ana", issuer="CryptoInvest")
    assert uri.startswith("otpauth://totp/CryptoInvest:ana?") and "issuer=CryptoInvest" in uri


def test_csrf_token_is_bound_to_session_and_secret() -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    assert csrf_token("a" * 40, first) == csrf_token("a" * 40, first)
    assert csrf_token("a" * 40, first) != csrf_token("a" * 40, second)
    assert csrf_token("a" * 40, first) != csrf_token("b" * 40, first)


def test_session_tokens_are_unique_and_hashed() -> None:
    tokens = {new_session_token() for _ in range(200)}
    assert len(tokens) == 200
    token = next(iter(tokens))
    assert hash_token(token) != token and len(hash_token(token)) == 64


def test_recovery_codes() -> None:
    codes = new_recovery_codes()
    assert len(codes) == len(set(codes)) == 10
    assert all(len(c) == 19 and c.count("-") == 3 for c in codes)
    assert hash_recovery_code("s" * 40, codes[0]) == hash_recovery_code(
        "s" * 40, codes[0].lower().replace("-", " ")
    )
    assert hash_recovery_code("s" * 40, codes[0]) != hash_recovery_code("t" * 40, codes[0])
    assert normalize_recovery_code(" abcd-efgh ") == "ABCDEFGH"


def test_lockout_schedule_is_progressive_and_capped() -> None:
    assert [lockout_seconds(n) for n in range(0, 5)] == [0, 0, 0, 0, 0]
    assert [lockout_seconds(n) for n in (5, 6, 7, 8)] == [30, 60, 120, 240]
    assert lockout_seconds(50) == 3600
    assert lockout_seconds(20, threshold=20) == 30


@given(st.integers(min_value=0, max_value=500))
def test_lockout_is_monotonic(failures: int) -> None:
    assert lockout_seconds(failures + 1) >= lockout_seconds(failures)


class _Tick:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


def test_throttle_locks_after_threshold_and_releases() -> None:
    tick = _Tick()
    throttle = FailureThrottle(threshold=3, clock=tick)
    for _ in range(2):
        assert throttle.register_failure("u:ana") == 0
    assert throttle.retry_after("u:ana") == 0
    assert throttle.register_failure("u:ana") == 30
    assert 29 <= throttle.retry_after("u:ana") <= 31
    assert throttle.retry_after("u:outro") == 0
    tick.value += 31
    assert throttle.retry_after("u:ana") == 0
    assert throttle.register_failure("u:ana") == 60
    throttle.register_success("u:ana")
    assert throttle.retry_after("u:ana") == 0


def test_throttle_window_forgets_old_failures_and_bounds_memory() -> None:
    tick = _Tick()
    throttle = FailureThrottle(threshold=3, window_seconds=60, max_keys=10, clock=tick)
    throttle.register_failure("k")
    throttle.register_failure("k")
    tick.value += 120
    assert throttle.register_failure("k") == 0
    for i in range(50):
        throttle.register_failure(f"key{i}")
    assert len(throttle._failures) <= 10
