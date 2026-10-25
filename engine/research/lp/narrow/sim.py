from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from math import log, sqrt
from typing import Literal

from engine.research.lp.liquidity import amounts, liquidity_for_value, position_value
from engine.research.lp.narrow.bars import PathData, Segment
from engine.research.lp.narrow.lvr import lvr_rate
from engine.research.lp.narrow.profile import Profile

RuleKind = Literal["fixed", "close", "pre"]
MAX_SLIPPAGE = 0.05
FLAT_REL = 1e-4


@dataclass(frozen=True, slots=True)
class Rule:
    kind: RuleKind
    theta: float = 1.0
    cooldown_h: float = 0.0

    @property
    def label(self) -> str:
        match self.kind:
            case "fixed":
                return "fixa"
            case "close":
                base = "ao fechar fora"
            case "pre":
                base = f"preemptiva {self.theta * 100:g}%"
        return base + (f" + cooldown {self.cooldown_h:g}h" if self.cooldown_h else "")


@dataclass(frozen=True, slots=True)
class GasUsd:
    open: float
    close: float
    swap: float


@dataclass(frozen=True, slots=True)
class Params:
    capital_usd: float
    half_width: float
    rule: Rule
    fee: float
    gas: GasUsd
    swap_fee: float
    delay_min: float = 0.0
    competition: float = 0.0


@dataclass(frozen=True, slots=True)
class Result:
    capital_usd: float
    hodl_usd: float
    position_usd: float
    fees_usd: float
    gas_usd: float
    swap_usd: float
    il_usd: float
    exposure_usd: float
    rebalances: int
    time_in_range: float
    lvr_theory_usd: float
    hours: int
    net_series: tuple[float, ...] = field(repr=False, default=())
    hodl_series: tuple[float, ...] = field(repr=False, default=())
    rebalance_bars: tuple[int, ...] = field(repr=False, default=())
    rebalance_min: tuple[float, ...] = field(repr=False, default=())

    @property
    def net_usd(self) -> float:
        return self.position_usd + self.fees_usd - self.gas_usd - self.swap_usd

    @property
    def excess_usd(self) -> float:
        return self.net_usd - self.hodl_usd

    @property
    def excess_pct(self) -> float:
        return self.excess_usd / self.capital_usd

    @property
    def directional_usd(self) -> float:
        """Efeito de ficar com exposicao ao preco diferente do HODL (fecha a decomposicao)."""
        return self.exposure_usd - (self.hodl_usd - self.capital_usd)

    @property
    def per_day(self) -> float:
        return self.rebalances / (self.hours / 24) if self.hours else 0.0


def hodl_value(first_price: float, last_price: float, capital: float) -> float:
    return capital / 2 * last_price / first_price + capital / 2


def overlap_sp(p1: float, p2: float, pa: float, pb: float) -> float:
    lo, hi = sorted((sqrt(p1), sqrt(p2)))
    return max(0.0, min(hi, sqrt(pb)) - max(lo, sqrt(pa)))


class _Sim:
    def __init__(self, path: PathData, profile: Profile, params: Params, sigma2_hour: float) -> None:
        self.path, self.profile, self.p = path, profile, params
        self.sigma2_min = sigma2_hour / 60.0
        self.fees = self.gas = self.swap = self.il = self.exposure = self.lvr = 0.0
        self.in_range_min = 0.0
        self.rebalances = 0
        self.rebalance_bars: list[int] = []
        self.rebalance_min: list[float] = []
        self.cooldown_end = 0.0
        self.pending: float | None = None
        self.bar_vol = 0.0
        self.bar_total = 0.0
        self.bar_other = 0.0
        self.bar_segs: tuple[Segment, ...] = ()
        first = path.bars[0].open
        self._open_band(first, params.capital_usd)
        self.gas += params.gas.open
        x, _ = amounts(self.liq, first, self.pa, self.pb)
        self._swap_cost(params.capital_usd / 2 / first - x, first)

    def _open_band(self, price: float, value: float) -> None:
        w = self.p.half_width
        self.center, self.pa, self.pb = price, price * (1 - w), price * (1 + w)
        self.liq = liquidity_for_value(value, price, self.pa, self.pb)
        x, y = amounts(self.liq, price, self.pa, self.pb)
        self.cycle_start = (x, y, value)

    def _swap_cost(self, base_delta: float, price: float) -> None:
        notional = abs(base_delta) * price
        depth = self.profile.active_depth_usd()
        slip = min(notional / depth, MAX_SLIPPAGE) if depth > 0 else MAX_SLIPPAGE
        self.swap += notional * (self.p.swap_fee + slip)

    @staticmethod
    def _price_at(seg: Segment, t: float) -> float:
        if seg.t2 <= seg.t1 or seg.p1 == seg.p2:
            return seg.p1
        return float(seg.p1 * (seg.p2 / seg.p1) ** ((t - seg.t1) / (seg.t2 - seg.t1)))

    def _retotal(self) -> None:
        """Denominador da reparticao do volume da barra com a posicao ATUAL (refeito a cada reajuste)."""
        mine = sum(overlap_sp(s.p1, s.p2, self.pa, self.pb) for s in self.bar_segs)
        self.bar_total = self.bar_other + self.liq * mine

    def _accrue(self, seg: Segment, ta: float, tb: float) -> None:
        pa_, pb_ = self._price_at(seg, ta), self._price_at(seg, tb)
        ov = overlap_sp(pa_, pb_, self.pa, self.pb)
        if self.bar_total > 0 and ov > 0:
            share = self.liq * ov / self.bar_total
            self.fees += self.bar_vol * self.p.fee * (1 - self.p.competition) * share
        lo, hi = sorted((pa_, pb_))
        if hi > lo:
            inside = max(0.0, min(hi, self.pb) - max(lo, self.pa)) / (hi - lo)
        else:
            inside = 1.0 if self.pa <= lo <= self.pb else 0.0
        dt = tb - ta
        self.in_range_min += dt * inside
        self.lvr += lvr_rate(self.sigma2_min, self.liq, sqrt(pa_ * pb_)) * dt * inside

    def _flat_fee(self, price: float) -> None:
        """Barra sem movimento: o volume negocia num intervalo minimo (+-1 pb) em torno do preco."""
        lo, hi = price * (1 - FLAT_REL), price * (1 + FLAT_REL)
        ov = overlap_sp(lo, hi, self.pa, self.pb)
        if ov > 0:
            mine = self.liq * ov
            share = mine / (self.profile.measure(lo, hi, price) + mine)
            self.fees += self.bar_vol * self.p.fee * (1 - self.p.competition) * share

    def _trigger_time(self, seg: Segment, tcur: float) -> float | None:
        rule = self.p.rule
        if rule.kind != "pre":
            return None
        ta = max(tcur, self.cooldown_end)
        if ta > seg.t2:
            return None
        w = self.p.half_width * rule.theta
        up, down = self.center * (1 + w), self.center * (1 - w)
        p_a = self._price_at(seg, ta)
        if p_a >= up or p_a <= down:
            return ta
        p2 = seg.p2
        for level in (up, down):
            if p_a < level <= p2 or p2 <= level < p_a:
                return ta + log(level / p_a) / log(p2 / p_a) * (seg.t2 - ta)
        return None

    def _execute(self, price: float, bar: int, t: float) -> None:
        value = position_value(self.liq, price, self.pa, self.pb)
        x0, y0, v0 = self.cycle_start
        hold_end = x0 * price + y0
        self.il += value - hold_end
        self.exposure += hold_end - v0
        old_x = amounts(self.liq, price, self.pa, self.pb)[0]
        self._open_band(price, value)
        new_x = amounts(self.liq, price, self.pa, self.pb)[0]
        self.gas += self.p.gas.close + self.p.gas.swap + self.p.gas.open
        self._swap_cost(old_x - new_x, price)
        self.rebalances += 1
        self.rebalance_bars.append(bar)
        self.rebalance_min.append(t)
        self.cooldown_end = t + self.p.rule.cooldown_h * 60.0
        self.pending = None
        if self.bar_total > 0:
            self._retotal()

    def run(self) -> Result:
        path, p = self.path, self.p
        bars = path.bars
        net_series: list[float] = []
        hodl_series: list[float] = []
        first = bars[0].open
        for i, bar in enumerate(bars):
            lo_i, hi_i = path.bar_segments[i]
            segs = path.segments[lo_i:hi_i]
            self.bar_vol = bar.volume_usd
            self.bar_segs = segs
            self.bar_other = 0.0
            if sum(abs(sqrt(s.p2) - sqrt(s.p1)) for s in segs) < FLAT_REL * sqrt(bar.open):
                self.bar_total = 0.0
                self._flat_fee(bar.open)
            else:
                self.bar_other = sum(self.profile.measure(s.p1, s.p2, bar.open) for s in segs)
                self._retotal()
            for seg in segs:
                self._walk(seg, i)
            net_series.append(
                position_value(self.liq, bar.close, self.pa, self.pb) + self.fees - self.gas - self.swap
            )
            hodl_series.append(hodl_value(first, bar.close, p.capital_usd))
        last = bars[-1].close
        end_value = position_value(self.liq, last, self.pa, self.pb)
        x0, y0, v0 = self.cycle_start
        hold_end = x0 * last + y0
        self.il += end_value - hold_end
        self.exposure += hold_end - v0
        self.gas += p.gas.close
        return Result(
            capital_usd=p.capital_usd,
            hodl_usd=hodl_value(first, last, p.capital_usd),
            position_usd=end_value,
            fees_usd=self.fees,
            gas_usd=self.gas,
            swap_usd=self.swap,
            il_usd=self.il,
            exposure_usd=self.exposure,
            rebalances=self.rebalances,
            time_in_range=self.in_range_min / (len(bars) * 60.0),
            lvr_theory_usd=self.lvr,
            hours=len(bars),
            net_series=tuple(net_series),
            hodl_series=tuple(hodl_series),
            rebalance_bars=tuple(self.rebalance_bars),
            rebalance_min=tuple(self.rebalance_min),
        )

    def _walk(self, seg: Segment, bar: int) -> None:
        tcur = seg.t1
        while True:
            event: tuple[float, bool] | None = None
            if self.pending is not None:
                if self.pending <= seg.t2:
                    event = (max(self.pending, tcur), False)
            elif (tt := self._trigger_time(seg, tcur)) is not None:
                event = (tt, True)
            te = event[0] if event else seg.t2
            self._accrue(seg, tcur, te)
            tcur = te
            if event is None:
                break
            if event[1] and self.p.delay_min > 0:
                self.pending = te + self.p.delay_min
            else:
                self._execute(self._price_at(seg, te), bar, te)
            if te >= seg.t2:
                break
        if self.p.rule.kind == "close" and seg.closes_bar and self.pending is None:
            outside = not self.pa < seg.p2 < self.pb
            if seg.t2 >= self.cooldown_end and outside:
                if self.p.delay_min > 0:
                    self.pending = seg.t2 + self.p.delay_min
                else:
                    self._execute(seg.p2, bar, seg.t2)


def simulate_path(
    path: PathData, profile: Profile, params: Params, sigma2_hour: float | None = None
) -> Result:
    sigma2 = path.sigma2_hour if sigma2_hour is None else sigma2_hour
    return _Sim(path, profile, params, sigma2).run()


def block_excess(res: Result, start: int, end: int) -> float:
    """Excesso sobre HODL (fracao do capital) no bloco [start, end), com a mesma estrategia continua."""
    base_net = res.net_series[start - 1] if start > 0 else res.capital_usd
    base_hodl = res.hodl_series[start - 1] if start > 0 else res.capital_usd
    d_net = res.net_series[end - 1] - base_net
    d_hodl = res.hodl_series[end - 1] - base_hodl
    return (d_net - d_hodl) / res.capital_usd


def blocks(n_bars: int, block_hours: int) -> Sequence[tuple[int, int]]:
    return [(s, s + block_hours) for s in range(0, n_bars - block_hours + 1, block_hours)]
