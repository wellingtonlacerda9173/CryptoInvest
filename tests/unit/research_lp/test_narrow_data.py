from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from engine.research.lp.data.gecko import GeckoClient, PoolInfo, parse_fee, parse_pool
from engine.research.lp.data.http import CachedGetClient
from engine.research.lp.data.rpc import PublicRpc, RpcError
from engine.research.lp.liquidity import liquidity_for_value
from engine.research.lp.narrow import abi
from engine.research.lp.narrow.profile import Profile, effective_depth
from engine.research.lp.narrow.replay import SwapEvent, decode_swap, replay_fees, swaps_in_window
from engine.research.lp.narrow.selection import classify_pair, ranked_candidates
from engine.research.lp.narrow.snapshot import (
    PoolSnapshot,
    build_profile,
    fetch_snapshot,
    human_liquidity,
)

Q96 = 2**96
NOW = datetime(2026, 10, 25, tzinfo=UTC)


def pool_item(addr: str, name: str, reserve: float, created: str = "2025-01-01T00:00:00Z") -> dict[str, Any]:
    return {
        "id": f"arbitrum_{addr}",
        "attributes": {
            "name": name,
            "address": addr,
            "reserve_in_usd": str(reserve),
            "volume_usd": {"h24": "10"},
            "pool_created_at": created,
        },
        "relationships": {
            "base_token": {"data": {"id": "arbitrum_0xbase"}},
            "quote_token": {"data": {"id": "arbitrum_0xquote"}},
        },
    }


def test_parse_pool_e_taxa() -> None:
    assert parse_fee("USDC / WBTC 0.05%") == pytest.approx(0.0005)
    info = parse_pool("arbitrum", pool_item("0xAB", "USDC / WBTC 0.05%", 5e6))
    assert info is not None and info.symbols == ("USDC", "WBTC") and info.address == "0xab"
    assert parse_pool("arbitrum", pool_item("0x1", "SEM TAXA", 1)) is None


def _info(name: str, reserve: float, created: str = "2025-01-01T00:00:00Z") -> PoolInfo:
    parsed = parse_pool("arbitrum", pool_item(f"0x{abs(hash(name)):x}", name, reserve, created))
    assert parsed is not None
    return parsed


def test_selecao_por_liquidez_ignora_novos_nao_estaveis_e_lsts() -> None:
    pools = [
        _info("USDC / WBTC 0.05%", 8e6),
        _info("WBTC / USDT 0.05%", 3e6),
        _info("WETH / USDC 0.05%", 9e6),
        _info("wstETH / USDC 0.05%", 20e6),
        _info("WBTC / WETH 0.05%", 50e6),
        _info("ARB / USDC 0.05%", 2e6),
        _info("WBTC / USDC 0.3%", 99e6, created="2026-09-01T00:00:00Z"),
    ]
    ranked = ranked_candidates(pools, NOW)
    assert [c[0].name for c in ranked["btc"]] == ["USDC / WBTC 0.05%", "WBTC / USDT 0.05%"]
    assert [c[0].name for c in ranked["eth"]] == ["WETH / USDC 0.05%"]
    assert [c[2] for c in ranked["media"]] == ["ARB"]
    cls = classify_pair(_info("USDC / WBTC 0.05%", 1))
    assert cls is not None and cls[1] == "quote"


class FakeHttp:
    def __init__(self, pages: dict[int | None, list[list[float]] | int]) -> None:
        self.pages = pages
        self.urls: list[str] = []

    def __call__(self, url: str, headers: dict[str, str]) -> tuple[int, bytes, dict[str, str]]:
        self.urls.append(url)
        before = int(url.split("before_timestamp=")[1]) if "before_timestamp" in url else None
        page = self.pages[before]
        if isinstance(page, int):
            return page, b"{}", {}
        body = {"data": {"attributes": {"ohlcv_list": page}}}
        return 200, json.dumps(body).encode(), {}


def candle(ts: int) -> list[float]:
    return [float(ts), 1.0, 1.1, 0.9, 1.0, 100.0]


def test_gecko_pagina_do_mais_novo_para_o_mais_antigo_e_aceita_limite_180d(tmp_path: Path) -> None:
    newest = [candle(t) for t in range(5000, 1000, -1000)]
    fake = FakeHttp({None: newest, 2000: [candle(1000), candle(0)], 0: 401})
    http = CachedGetClient(tmp_path, transport=fake, sleep=lambda _s: None, min_interval_s=0)
    rows = GeckoClient(http).ohlcv_hour("arbitrum", "0xpool", "quote", 10_000)
    assert [int(r[0]) for r in rows] == [0, 1000, 2000, 3000, 4000, 5000]
    assert "currency=usd" in fake.urls[0] and "token=quote" in fake.urls[0]
    assert all(url.startswith("https://api.geckoterminal.com/") for url in fake.urls)


class FakeNode:
    """No Uniswap v3 falso: slot0, liquidity, tickSpacing, tokens, bitmap e ticks."""

    def __init__(self, tick: int, liquidity: int, ticks: dict[int, int], spacing: int = 10) -> None:
        self.tick, self.liquidity, self.ticks, self.spacing = tick, liquidity, ticks, spacing
        self.methods: list[str] = []
        self.token_a, self.token_b = "0x" + "11" * 20, "0x" + "22" * 20

    def _word(self, v: int) -> str:
        return f"{v & abi.MASK256:064x}"

    def _one(self, req: dict[str, Any]) -> dict[str, Any]:
        self.methods.append(req["method"])
        if req["method"] == "eth_blockNumber":
            return {"id": req["id"], "result": hex(1000)}
        data = req["params"][0]["data"]
        to = req["params"][0]["to"]
        sel, arg = data[:10], data[10:]
        if sel == abi.SEL_SLOT0:
            sqrt_p = int(1.0001 ** (self.tick / 2) * Q96)
            out = self._word(sqrt_p) + self._word(self.tick) + self._word(0) * 5
        elif sel == abi.SEL_LIQUIDITY:
            out = self._word(self.liquidity)
        elif sel == abi.SEL_TICK_SPACING:
            out = self._word(self.spacing)
        elif sel == abi.SEL_TOKEN0:
            out = self._word(int(self.token_a, 16))
        elif sel == abi.SEL_TOKEN1:
            out = self._word(int(self.token_b, 16))
        elif sel == abi.SEL_DECIMALS:
            out = self._word(8 if to == self.token_a else 6)
        elif sel == abi.SEL_TICK_BITMAP:
            wp = abi.to_signed(int(arg, 16))
            bits = 0
            for t in self.ticks:
                comp = t // self.spacing
                if comp >> 8 == wp:
                    bits |= 1 << (comp & 255)
            out = self._word(bits)
        elif sel == abi.SEL_TICKS:
            t = abi.to_signed(int(arg, 16))
            out = self._word(abs(self.ticks[t])) + self._word(self.ticks[t]) + self._word(0) * 6
        else:
            raise AssertionError(sel)
        return {"id": req["id"], "result": "0x" + out}

    def __call__(self, url: str, payload: bytes) -> tuple[int, bytes]:
        body = json.loads(payload)
        if isinstance(body, list):
            return 200, json.dumps([self._one(r) for r in reversed(body)]).encode()
        return 200, json.dumps(self._one(body)).encode()


def make_rpc(node: FakeNode, tmp_path: Path | None = None) -> PublicRpc:
    return PublicRpc("https://rpc.invalid", tmp_path, 0, node, lambda _s: None)


def test_rpc_so_permite_leitura() -> None:
    node = FakeNode(0, 1, {})
    rpc = make_rpc(node)
    for method in ("eth_sendRawTransaction", "eth_sendTransaction", "personal_sign", "eth_signTransaction"):
        with pytest.raises(PermissionError):
            rpc.request(method, [])
    assert node.methods == []
    assert int(rpc.request("eth_blockNumber", []), 16) == 1000


def test_rpc_repete_em_429_e_falha_em_erro_jsonrpc() -> None:
    calls = {"n": 0}

    def transport(url: str, payload: bytes) -> tuple[int, bytes]:
        calls["n"] += 1
        if calls["n"] == 1:
            return 429, b"{}"
        return 200, json.dumps({"id": 1, "result": "0x1"}).encode()

    rpc = PublicRpc("https://rpc.invalid", None, 0, transport, lambda _s: None)
    assert rpc.request("eth_blockNumber", []) == "0x1"
    assert calls["n"] == 2

    def bad(url: str, payload: bytes) -> tuple[int, bytes]:
        return 200, json.dumps({"id": 1, "error": {"code": -32000, "message": "x"}}).encode()

    with pytest.raises(RpcError):
        PublicRpc("https://rpc.invalid", None, 0, bad, lambda _s: None).request("eth_blockNumber", [])


LIQ_RAW = 10**14


def pool_node(tick: int = 66_900) -> FakeNode:
    return FakeNode(tick, LIQ_RAW, {tick - 200: LIQ_RAW, tick + 300: -LIQ_RAW})


def test_snapshot_le_ticks_do_bitmap_e_monta_perfil_para_estavel_token1() -> None:
    node = pool_node()
    snap = fetch_snapshot(make_rpc(node), "0xpool", node.token_a, node.token_b)
    assert snap.tick == 66_900 and snap.liquidity == LIQ_RAW
    assert dict(snap.ticks) == {66_700: LIQ_RAW, 67_200: -LIQ_RAW}
    assert (snap.decimals0, snap.decimals1) == (8, 6)
    prof = build_profile(snap, stable_is_token0=False)
    lh = human_liquidity(LIQ_RAW, snap)
    sp = prof.sqrt_p_snap
    inside = prof.measure(sp**2 * 0.99, sp**2 * 1.01, sp**2)
    assert inside == pytest.approx(lh * sp * (1.01**0.5 - 0.99**0.5), rel=1e-6)
    assert prof.measure(sp**2 * 1.2, sp**2 * 1.3, sp**2) == pytest.approx(0, abs=1e-9)
    assert set(node.methods) <= {"eth_blockNumber", "eth_call"}


def test_perfil_com_estavel_token0_inverte_o_eixo_de_preco() -> None:
    node = pool_node()
    snap = fetch_snapshot(make_rpc(node), "0xpool", node.token_a, node.token_b)
    prof = build_profile(snap, stable_is_token0=True)
    lh = human_liquidity(LIQ_RAW, snap)
    sp = prof.sqrt_p_snap
    assert prof.measure(sp**2 * 0.99, sp**2 * 1.01, sp**2) == pytest.approx(
        lh * sp * (1.01**0.5 - 0.99**0.5), rel=1e-6
    )
    assert prof.active_depth_usd() == pytest.approx(lh * sp)


def test_profundidade_efetiva_em_faixa_total_equivale_a_tvl_v2() -> None:
    tvl, price = 1e7, 100.0
    prof = Profile.v2_equivalent(tvl, price)
    mean_l, usd = effective_depth(prof, 0.01)
    assert mean_l == pytest.approx(tvl / (2 * price**0.5))
    assert 0 < usd < tvl


def log_entry(
    amount0: int, amount1: int, tick: int, liquidity: int, price: float, idx: int
) -> dict[str, Any]:
    sqrt_p = int(price**0.5 * Q96)
    data = "0x" + "".join(f"{v & abi.MASK256:064x}" for v in (amount0, amount1, sqrt_p, liquidity, tick))
    return {"data": data, "blockNumber": hex(10 + idx), "logIndex": hex(idx)}


def test_decodifica_swap_e_calcula_taxa_capturada_de_posicao_fixa() -> None:
    node = FakeNode(46_000, 10**22, {})
    snap = PoolSnapshot(1, 0, 46_000, 10**22, 10, node.token_a, node.token_b, 18, 18, (), 0, 0)
    first = decode_swap(log_entry(0, 0, 46_000, 10**22, 100.0, 0))
    second = decode_swap(log_entry(-(10**19), 1000 * 10**18, 46_010, 10**22, 100.1, 1))
    assert second.amount1 == 1000 * 10**18 and second.amount0 == -(10**19) and second.tick == 46_010
    res = replay_fees([first, second], snap, False, 0.0005, [0.01, 0.0005], 1000.0)
    lh = human_liquidity(10**22, snap)
    mine = liquidity_for_value(1000.0, 100.0, 99.0, 101.0)
    assert res.volume_usd == pytest.approx(1000.0)
    assert res.fees_usd[0.01] == pytest.approx(0.0005 * 1000 * mine / (lh + mine))
    frac = (100.05**0.5 - 10) / (100.1**0.5 - 10)
    mine_narrow = liquidity_for_value(1000.0, 100.0, 99.95, 100.05)
    assert res.fees_usd[0.0005] == pytest.approx(
        0.0005 * 1000 * frac * mine_narrow / (lh + mine_narrow), rel=1e-6
    )
    assert isinstance(first, SwapEvent)


class FakeLogsNode:
    """Cadeia falsa: 4 blocos/s a partir de t0, um Swap a cada 50 blocos; limita o intervalo do getLogs."""

    T0, HEAD, MAX_RANGE = 1_000_000, 400_000, 10_000

    def __init__(self) -> None:
        self.ranges: list[int] = []

    def __call__(self, url: str, payload: bytes) -> tuple[int, bytes]:
        req = json.loads(payload)
        if req["method"] == "eth_blockNumber":
            return 200, json.dumps({"id": req["id"], "result": hex(self.HEAD)}).encode()
        flt = req["params"][0]
        lo, hi = int(flt["fromBlock"], 16), int(flt["toBlock"], 16)
        self.ranges.append(hi - lo + 1)
        if hi - lo + 1 > self.MAX_RANGE:
            err = {"code": -32602, "message": "range"}
            return 200, json.dumps({"id": req["id"], "error": err}).encode()
        logs = []
        for blk in range(max(lo, 0) // 50 * 50, hi + 1, 50):
            if blk < lo:
                continue
            entry = log_entry(0, 0, 0, 10**22, 100.0, 0)
            entry.update(blockNumber=hex(blk), logIndex="0x0", blockTimestamp=hex(self.T0 + blk // 4))
            logs.append(entry)
        return 200, json.dumps({"id": req["id"], "result": logs}).encode()


def test_swaps_na_janela_usam_blockTimestamp_e_respeitam_limite_de_intervalo() -> None:
    node = FakeLogsNode()
    rpc = PublicRpc("https://rpc.invalid", None, 0, node, lambda _s: None)
    start, end = FakeLogsNode.T0 + 20_000, FakeLogsNode.T0 + 40_000
    events = swaps_in_window(rpc, "0xpool", start, end, 9_000, probe_blocks=8_000)
    assert events and all(start <= e.ts < end for e in events)
    assert events[0].ts - start < 50 and end - events[-1].ts < 50
    assert max(node.ranges) <= 9_000
    assert len(events) == pytest.approx(20_000 * 4 / 50, abs=2)
