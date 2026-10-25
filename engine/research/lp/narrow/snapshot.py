from __future__ import annotations

from dataclasses import dataclass
from math import log
from typing import Any

from engine.research.lp.data.rpc import PublicRpc
from engine.research.lp.narrow import abi
from engine.research.lp.narrow.profile import Profile

TICK_BASE = 1.0001
Q96 = 2**96


@dataclass(frozen=True, slots=True)
class PoolSnapshot:
    block: int
    sqrt_price_x96: int
    tick: int
    liquidity: int
    tick_spacing: int
    token0: str
    token1: str
    decimals0: int
    decimals1: int
    ticks: tuple[tuple[int, int], ...]
    tick_lo: int
    tick_hi: int

    def to_json(self) -> dict[str, Any]:
        return {
            "block": self.block,
            "sqrt_price_x96": str(self.sqrt_price_x96),
            "tick": self.tick,
            "liquidity": str(self.liquidity),
            "tick_spacing": self.tick_spacing,
            "token0": self.token0,
            "token1": self.token1,
            "decimals0": self.decimals0,
            "decimals1": self.decimals1,
            "ticks": [[t, str(n)] for t, n in self.ticks],
            "tick_lo": self.tick_lo,
            "tick_hi": self.tick_hi,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> PoolSnapshot:
        return cls(
            block=int(d["block"]),
            sqrt_price_x96=int(d["sqrt_price_x96"]),
            tick=int(d["tick"]),
            liquidity=int(d["liquidity"]),
            tick_spacing=int(d["tick_spacing"]),
            token0=d["token0"],
            token1=d["token1"],
            decimals0=int(d["decimals0"]),
            decimals1=int(d["decimals1"]),
            ticks=tuple((int(t), int(n)) for t, n in d["ticks"]),
            tick_lo=int(d["tick_lo"]),
            tick_hi=int(d["tick_hi"]),
        )


def _call(pool: str, selector: str, arg: str = "") -> tuple[str, list[Any]]:
    return "eth_call", [{"to": pool, "data": selector + arg}, "BLOCK"]


def _at(calls: list[tuple[str, list[Any]]], block_hex: str) -> list[tuple[str, list[Any]]]:
    return [(m, [p[0], block_hex]) for m, p in calls]


def fetch_snapshot(
    rpc: PublicRpc, pool: str, token_a: str, token_b: str, half_range: float = 0.12
) -> PoolSnapshot:
    """Estado atual (somente leitura): slot0, liquidity, mapa de ticks via tickBitmap + ticks(int24)."""
    block = int(rpc.request("eth_blockNumber", []), 16) - 2
    bh = hex(block)
    base = _at(
        [
            _call(pool, abi.SEL_SLOT0),
            _call(pool, abi.SEL_LIQUIDITY),
            _call(pool, abi.SEL_TICK_SPACING),
            _call(pool, abi.SEL_TOKEN0),
            _call(pool, abi.SEL_TOKEN1),
        ],
        bh,
    )
    slot0, liq, spacing, t0, t1 = rpc.batch_calls(base)
    w = abi.words(slot0)
    sqrt_price, tick = w[0], abi.to_signed(w[1])
    tick_spacing = abi.words(spacing)[0]
    token0, token1 = abi.address_of(abi.words(t0)[0]), abi.address_of(abi.words(t1)[0])
    dec = rpc.batch_calls(_at([_call(token0, abi.SEL_DECIMALS), _call(token1, abi.SEL_DECIMALS)], bh))
    span = int(log(1 + half_range) / log(TICK_BASE)) + tick_spacing
    tick_lo, tick_hi = tick - span, tick + span
    word_lo, word_hi = (tick_lo // tick_spacing) >> 8, (tick_hi // tick_spacing) >> 8
    bitmaps = rpc.batch_calls(
        _at(
            [_call(pool, abi.SEL_TICK_BITMAP, abi.encode_int(wp)) for wp in range(word_lo, word_hi + 1)],
            bh,
        )
    )
    init_ticks: list[int] = []
    for wp, raw in zip(range(word_lo, word_hi + 1), bitmaps, strict=True):
        bits = abi.words(raw)[0]
        init_ticks += [
            t
            for bit in range(256)
            if bits >> bit & 1 and tick_lo <= (t := ((wp << 8) + bit) * tick_spacing) <= tick_hi
        ]
    infos = rpc.batch_calls(_at([_call(pool, abi.SEL_TICKS, abi.encode_int(t)) for t in init_ticks], bh))
    nets = [abi.to_signed(abi.words(i)[1]) for i in infos]
    return PoolSnapshot(
        block=block,
        sqrt_price_x96=sqrt_price,
        tick=tick,
        liquidity=abi.words(liq)[0],
        tick_spacing=tick_spacing,
        token0=token0,
        token1=token1,
        decimals0=abi.words(dec[0])[0],
        decimals1=abi.words(dec[1])[0],
        ticks=tuple(zip(init_ticks, nets, strict=True)),
        tick_lo=tick_lo,
        tick_hi=tick_hi,
    )


def sqrt_usd(sqrt_raw: float, decimals0: int, decimals1: int, stable_is_token0: bool) -> float:
    """sqrt(USD por 1 token volatil) a partir de sqrt(preco bruto token1/token0)."""
    if stable_is_token0:
        return 10 ** ((decimals1 - decimals0) / 2) / sqrt_raw
    return sqrt_raw * 10 ** ((decimals0 - decimals1) / 2)


def snapshot_price_usd(snap: PoolSnapshot, stable_is_token0: bool) -> float:
    return sqrt_usd(snap.sqrt_price_x96 / Q96, snap.decimals0, snap.decimals1, stable_is_token0) ** 2


def human_liquidity(raw: float, snap: PoolSnapshot) -> float:
    """L bruto -> L 'humano': USD de quote = L_h x delta sqrt(preco USD)."""
    return raw * 10 ** (-(snap.decimals0 + snap.decimals1) / 2)


def build_profile(snap: PoolSnapshot, stable_is_token0: bool, mult: float = 1.0) -> Profile:
    sp_now = sqrt_usd(snap.sqrt_price_x96 / Q96, snap.decimals0, snap.decimals1, stable_is_token0)
    ticks = sorted(snap.ticks)
    if not ticks:
        dens = [human_liquidity(snap.liquidity, snap)] * 2
        return Profile([1.0], dens, sp_now, mult)
    j = max((i for i, (t, _) in enumerate(ticks) if t <= snap.tick), default=-1)
    after = [0] * len(ticks)
    if j >= 0:
        after[j] = snap.liquidity
        for i in range(j + 1, len(ticks)):
            after[i] = after[i - 1] + ticks[i][1]
        for i in range(j, 0, -1):
            after[i - 1] = after[i] - ticks[i][1]
        before = after[0] - ticks[0][1]
    else:
        before = snap.liquidity
        after[0] = before + ticks[0][1]
        for i in range(1, len(ticks)):
            after[i] = after[i - 1] + ticks[i][1]
    dens = [max(0.0, human_liquidity(v, snap)) for v in [before, *after]]
    edges = [
        sqrt_usd(TICK_BASE ** (t / 2), snap.decimals0, snap.decimals1, stable_is_token0) / sp_now
        for t, _ in ticks
    ]
    if stable_is_token0:
        edges, dens = edges[::-1], dens[::-1]
    return Profile(edges, dens, sp_now, mult)
