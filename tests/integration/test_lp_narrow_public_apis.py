from __future__ import annotations

from pathlib import Path

import pytest

from engine.research.lp.data.gecko import GeckoClient
from engine.research.lp.data.http import CachedGetClient
from engine.research.lp.data.rpc import PUBLIC_RPC, PublicRpc, RpcError
from engine.research.lp.narrow.snapshot import build_profile, fetch_snapshot

pytestmark = pytest.mark.integration

ARB_USDC_WBTC = "0x0e4831319a50228b9e450861297ab92dee15b44f"
ARB_USDC = "0xaf88d065e77c8cc2239327c5edb3a432268e5831"
ARB_WBTC = "0x2f2a2543b76a4166549f7aab2e75bef0aefc5b0f"


def test_geckoterminal_ohlcv_horario_com_volume_sem_chave(tmp_path: Path) -> None:
    http = CachedGetClient(tmp_path)
    try:
        rows = GeckoClient(http).ohlcv_hour("arbitrum", ARB_USDC_WBTC, "quote", 200)
    except (OSError, RuntimeError) as exc:
        pytest.skip(f"rede/API indisponivel: {exc}")
    assert len(rows) >= 100
    assert all(r[4] > 1_000 for r in rows)
    assert sum(r[5] for r in rows) > 0


def test_rpc_publico_snapshot_de_ticks_somente_leitura(tmp_path: Path) -> None:
    rpc = PublicRpc(PUBLIC_RPC["arbitrum"], tmp_path)
    try:
        snap = fetch_snapshot(rpc, ARB_USDC_WBTC, ARB_USDC, ARB_WBTC)
    except (OSError, RpcError) as exc:
        pytest.skip(f"RPC publico indisponivel: {exc}")
    assert snap.liquidity > 0 and len(snap.ticks) > 5
    prof = build_profile(snap, stable_is_token0=snap.token0.lower() == ARB_USDC)
    assert prof.active_depth_usd() > 10_000
