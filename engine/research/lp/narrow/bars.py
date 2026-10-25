from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from math import log

HOUR = 3600
MIN_LEG_WEIGHT = 1e-4


@dataclass(frozen=True, slots=True)
class Bar:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume_usd: float


@dataclass(frozen=True, slots=True)
class Segment:
    bar: int
    t1: float
    t2: float
    p1: float
    p2: float
    closes_bar: bool


@dataclass(frozen=True, slots=True)
class PathData:
    """Barras horarias contiguas e a trajetoria de preco linear em log, em minutos desde o inicio."""

    bars: tuple[Bar, ...]
    segments: tuple[Segment, ...]
    bar_segments: tuple[tuple[int, int], ...]

    @property
    def sigma2_hour(self) -> float:
        rets = [log(b.close / a.close) for a, b in pairwise(self.bars)]
        if len(rets) < 2:
            return 0.0
        mean = sum(rets) / len(rets)
        return sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)


def regularize(
    rows: Sequence[Sequence[float]], start_ts: int | None = None, end_ts: int | None = None
) -> list[Bar]:
    """[ts, o, h, l, c, v] em qualquer ordem -> grade horaria contigua.

    A abertura de cada barra e o fechamento da anterior (continuidade); horas sem negocio repetem o
    ultimo preco com volume zero.
    """
    by_ts = {int(r[0]) // HOUR * HOUR: r for r in rows if r[4] > 0}
    if not by_ts:
        return []
    earliest = min(by_ts)
    first = earliest if start_ts is None else start_ts
    last = max(by_ts) if end_ts is None else end_ts
    bars: list[Bar] = []
    prev = float(by_ts[earliest][1])
    for ts in range(first, last + HOUR, HOUR):
        row = by_ts.get(ts)
        if row is None:
            bars.append(Bar(ts, prev, prev, prev, prev, 0.0))
            continue
        close = float(row[4])
        high = max(float(row[2]), prev, close)
        low = min(float(row[3]), prev, close)
        bars.append(Bar(ts, prev, high, low, close, float(row[5])))
        prev = close
    return bars


def build_path(bars: Sequence[Bar]) -> PathData:
    """Ordem intrabarra assumida: abertura, minima, maxima, fechamento se subiu; invertida se caiu."""
    segments: list[Segment] = []
    spans: list[tuple[int, int]] = []
    for i, bar in enumerate(bars):
        mid = (bar.low, bar.high) if bar.close >= bar.open else (bar.high, bar.low)
        pts = [bar.open, mid[0], mid[1], bar.close]
        weights = [abs(log(b / a)) + MIN_LEG_WEIGHT for a, b in pairwise(pts)]
        total = sum(weights)
        t = i * 60.0
        start = len(segments)
        for k, (a, b) in enumerate(pairwise(pts)):
            dt = 60.0 * weights[k] / total
            segments.append(Segment(i, t, t + dt, a, b, k == 2))
            t += dt
        spans.append((start, len(segments)))
    return PathData(tuple(bars), tuple(segments), tuple(spans))
