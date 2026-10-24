import hashlib
import hmac
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from api.binance_keys import BinanceKeyVerifier, KeyVerificationError
from api.cli import COMMAND_KEY_FILE, MASTER_KEYS_FILE, SESSION_SECRET_FILE, append_master_key, init_secrets
from api.security.crypto import KeyRing

API_KEY = "AKtestKey0123456789abcdef"
API_SECRET = "SECRETtest0123456789abcdef"


async def _serve(payload: dict[str, Any], status: int, seen: dict[str, Any]) -> TestServer:
    async def handler(request: web.Request) -> web.Response:
        seen["key"] = request.headers.get("X-MBX-APIKEY")
        seen["path"] = request.path
        seen["query"] = dict(request.query)
        seen["raw"] = request.query_string
        return web.json_response(payload, status=status)

    app = web.Application()
    app.router.add_get("/sapi/v1/account/apiRestrictions", handler)
    server = TestServer(app)
    await server.start_server()
    return server


async def test_verifier_parses_permissions_and_signs_request() -> None:
    seen: dict[str, Any] = {}
    server = await _serve(
        {
            "ipRestrict": True,
            "enableReading": True,
            "enableWithdrawals": False,
            "enableSpotAndMarginTrading": True,
            "enableInternalTransfer": False,
            "enableMargin": False,
            "enableFutures": False,
        },
        200,
        seen,
    )
    try:
        permissions = await BinanceKeyVerifier(str(server.make_url(""))).verify(API_KEY, API_SECRET)
    finally:
        await server.close()
    assert permissions.can_read and permissions.can_trade and not permissions.can_withdraw
    assert permissions.ip_restricted is True
    assert seen["key"] == API_KEY and seen["path"] == "/sapi/v1/account/apiRestrictions"
    unsigned = "&".join(f"{k}={v}" for k, v in seen["query"].items() if k != "signature")
    assert (
        seen["query"]["signature"]
        == hmac.new(API_SECRET.encode(), unsigned.encode(), hashlib.sha256).hexdigest()
    )
    assert API_SECRET not in seen["raw"]


async def test_verifier_detects_withdraw_permission() -> None:
    server = await _serve(
        {
            "enableReading": True,
            "enableSpotAndMarginTrading": True,
            "enableWithdrawals": True,
            "ipRestrict": False,
        },
        200,
        {},
    )
    try:
        permissions = await BinanceKeyVerifier(str(server.make_url(""))).verify(API_KEY, API_SECRET)
    finally:
        await server.close()
    assert permissions.can_withdraw is True and permissions.ip_restricted is False


async def test_verifier_error_messages_never_contain_secrets() -> None:
    server = await _serve({"code": -2015, "msg": f"Invalid key {API_KEY} {API_SECRET}"}, 401, {})
    try:
        with pytest.raises(KeyVerificationError) as refused:
            await BinanceKeyVerifier(str(server.make_url(""))).verify(API_KEY, API_SECRET)
    finally:
        await server.close()
    assert API_KEY not in str(refused.value) and API_SECRET not in str(refused.value)
    assert "-2015" in str(refused.value) and refused.value.upstream is False

    with pytest.raises(KeyVerificationError) as unreachable:
        await BinanceKeyVerifier("http://127.0.0.1:9", timeout_seconds=2).verify(API_KEY, API_SECRET)
    assert unreachable.value.upstream is True
    assert API_KEY not in str(unreachable.value) and API_SECRET not in str(unreachable.value)
    assert unreachable.value.__cause__ is None and unreachable.value.__suppress_context__


def test_init_secrets_creates_once_and_never_overwrites(tmp_path: Path) -> None:
    assert {SESSION_SECRET_FILE, MASTER_KEYS_FILE, COMMAND_KEY_FILE} <= set(init_secrets(tmp_path))
    assert len((tmp_path / COMMAND_KEY_FILE).read_text()) >= 32
    session_secret = (tmp_path / SESSION_SECRET_FILE).read_text()
    assert len(session_secret) >= 32
    assert init_secrets(tmp_path) == []
    assert (tmp_path / SESSION_SECRET_FILE).read_text() == session_secret
    assert KeyRing.parse((tmp_path / MASTER_KEYS_FILE).read_text()).active_version == 1


def test_new_master_key_appends_next_version(tmp_path: Path) -> None:
    init_secrets(tmp_path)
    first = KeyRing.parse((tmp_path / MASTER_KEYS_FILE).read_text())
    assert append_master_key(tmp_path) == 2
    ring = KeyRing.parse((tmp_path / MASTER_KEYS_FILE).read_text())
    assert ring.active_version == 2 and ring.keys[1] == first.keys[1]
