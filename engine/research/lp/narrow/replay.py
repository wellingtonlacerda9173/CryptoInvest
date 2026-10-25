from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from engine.research.lp.data.rpc import PublicRpc, RpcError
from engine.research.lp.liquidity import liquidity_for_value
from engine.research.lp.narrow import abi
from engine.research.lp.narrow.sim import overlap_sp
from engine.research.lp.narrow.snapshot import PoolSnapshot, human_liquidity, sqrt_usd

Q96 = 2**96
LOG_CHUNK = 90_000


@dataclass(frozen=True, slots=True)
class SwapEvent:
    block: int
    log_index: int
    amount0: int
    amount1: int
    sqrt_price_x96: int
    liquidity: int
    tick: int
    ts: int = 0


def decode_swap(log: dict[str, Any]) -> SwapEvent:
    w = abi.words(log["data"])
    return SwapEvent(
        block=int(log["blockNumber"], 16),
        log_index=int(log["logIndex"], 16),
        amount0=abi.to_signed(w[0]),
        amount1=abi.to_signed(w[1]),
        sqrt_price_x96=w[2],
        liquidity=w[3],
        tick=abi.to_signed(w[4]),
        ts=int(log.get("blockTimestamp", "0x0"), 16),
    )


def fetch_swaps(
    rpc: PublicRpc, pool: str, from_block: int, to_block: int, chunk: int = LOG_CHUNK
) -> list[SwapEvent]:
    events: list[SwapEvent] = []
    start = from_block
    while start <= to_block:
        end = min(start + chunk - 1, to_block)
        logs = rpc.request(
            "eth_getLogs",
            [
                {
                    "address": pool,
                    "topics": [abi.SWAP_TOPIC],
                    "fromBlock": hex(start),
                    "toBlock": hex(end),
                }
            ],
        )
        events += [decode_swap(entry) for entry in logs]
        start = end + 1
    return sorted(events, key=lambda e: (e.block, e.log_index))


def swaps_in_window(
    rpc: PublicRpc, pool: str, start_ts: int, end_ts: int, chunk: int, probe_blocks: int = 20_000
) -> list[SwapEvent]:
    """Swaps com `blockTimestamp` em [start_ts, end_ts), sem ler estado antigo (RPCs publicos nao o guardam).

    Estima a taxa segundos/bloco com os logs recentes, busca com folga e estende ate cobrir a janela.
    """
    head = int(rpc.request("eth_blockNumber", []), 16)
    recent = fetch_swaps(rpc, pool, head - probe_blocks, head, chunk)
    if recent and recent[-1].ts == 0:
        raise RpcError("RPC devolve blockTimestamp=0 e nao guarda estado antigo: janela nao datavel")
    if len(recent) < 2 or recent[-1].ts == recent[0].ts:
        raise RpcError("poucos eventos recentes para estimar o tempo de bloco")
    rate = (recent[-1].ts - recent[0].ts) / (recent[-1].block - recent[0].block)
    ref_block, ref_ts = recent[-1].block, recent[-1].ts
    est_lo = ref_block - int((ref_ts - start_ts) / rate)
    est_hi = ref_block - int((ref_ts - end_ts) / rate)
    margin = max(int((ref_block - est_lo) * 0.03), probe_blocks // 4)
    lo, hi = est_lo - margin, min(est_hi + margin, head)
    for _ in range(4):
        events = fetch_swaps(rpc, pool, lo, hi, chunk)
        if events and events[0].ts <= start_ts and events[-1].ts >= end_ts:
            return [e for e in events if start_ts <= e.ts < end_ts]
        if not events or events[0].ts > start_ts:
            lo -= margin
        if not events or events[-1].ts < end_ts:
            hi = min(hi + margin, head)
    raise RpcError("janela nao coberta pelos logs")


@dataclass(frozen=True, slots=True)
class ReplayResult:
    swaps: int
    volume_usd: float
    fees_usd: dict[float, float]
    start_price: float


def replay_fees(
    events: Sequence[SwapEvent],
    snap: PoolSnapshot,
    stable_is_token0: bool,
    fee: float,
    half_widths: Sequence[float],
    capital_usd: float,
) -> ReplayResult:
    """Taxas que posicoes FIXAS (centradas no primeiro preco) teriam recebido, evento a evento.

    Para cada Swap, o trecho de preco entre o evento anterior e este tem liquidez suposta constante
    (a ativa depois do swap): participacao = L_me/(L_ativa + L_me) sobre a fracao do trecho dentro da faixa.
    Exato quando o swap nao cruza ticks inicializados; aproximacao caso contrario.
    """
    d0, d1 = snap.decimals0, snap.decimals1
    sps = [sqrt_usd(e.sqrt_price_x96 / Q96, d0, d1, stable_is_token0) for e in events]
    p0 = sps[0] ** 2
    bands = {w: (p0 * (1 - w), p0 * (1 + w)) for w in half_widths}
    mine = {w: liquidity_for_value(capital_usd, p0, a, b) for w, (a, b) in bands.items()}
    fees = dict.fromkeys(half_widths, 0.0)
    volume = 0.0
    for k in range(1, len(events)):
        e = events[k]
        stable_amt = (e.amount0 / 10**d0) if stable_is_token0 else (e.amount1 / 10**d1)
        vol_amt = (e.amount1 / 10**d1) if stable_is_token0 else (e.amount0 / 10**d0)
        usd_in = stable_amt if stable_amt > 0 else vol_amt * sps[k] ** 2
        volume += usd_in
        lo, hi = sorted((sps[k - 1], sps[k]))
        liq = human_liquidity(e.liquidity, snap)
        for w, (a, b) in bands.items():
            frac = overlap_sp(lo**2, hi**2, a, b) / (hi - lo) if hi > lo else float(a <= lo**2 <= b)
            if frac > 0:
                fees[w] += fee * usd_in * frac * mine[w] / (liq + mine[w])
    return ReplayResult(len(events) - 1, volume, fees, p0)
