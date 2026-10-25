from datetime import UTC, datetime, timedelta

import aiohttp
import pytest

from api.services.ticker import RawTick, TickerService, parse_tick


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Fetcher:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail = False
        self.price = 100.0

    async def __call__(self, symbol: str) -> RawTick:
        self.calls.append(symbol)
        if self.fail:
            raise aiohttp.ClientConnectionError("fora do ar")
        return RawTick(self.price, 1.5)


def build() -> tuple[TickerService, Fetcher, Clock]:
    clock, fetcher = Clock(), Fetcher()
    return TickerService(fetcher, clock), fetcher, clock


async def test_cache_limits_upstream_calls_and_dedups_symbols() -> None:
    service, fetcher, clock = build()
    await service.quotes(["BTC/USDT", "BTC/USDT", "ETH/USDT"])
    await service.quotes(["BTC/USDT"])
    assert fetcher.calls == ["BTC/USDT", "ETH/USDT"]
    clock.advance(2.5)
    fetcher.price = 101.0
    (quote,) = await service.quotes(["BTC/USDT"], allowed=frozenset({"BTC/USDT"}))
    assert quote.price == 101.0 and quote.age_seconds == 0 and not quote.stale


async def test_failure_without_previous_quote_returns_nothing_and_backs_off() -> None:
    service, fetcher, clock = build()
    fetcher.fail = True
    assert await service.quotes(["BTC/USDT"]) == []
    assert await service.quotes(["BTC/USDT"]) == []
    assert len(fetcher.calls) == 1
    clock.advance(2.1)
    await service.quotes(["BTC/USDT"])
    assert len(fetcher.calls) == 2
    clock.advance(3.9)
    await service.quotes(["BTC/USDT"])
    assert len(fetcher.calls) == 2
    clock.advance(0.2)
    await service.quotes(["BTC/USDT"])
    assert len(fetcher.calls) == 3


async def test_old_quote_is_flagged_stale_then_dropped() -> None:
    service, fetcher, clock = build()
    await service.quotes(["BTC/USDT"])
    fetcher.fail = True
    clock.advance(10)
    (quote,) = await service.quotes(["BTC/USDT"])
    assert not quote.stale
    clock.advance(10)
    (quote,) = await service.quotes(["BTC/USDT"])
    assert quote.stale and quote.age_seconds == 20
    clock.advance(120)
    assert await service.quotes(["BTC/USDT"]) == []


async def test_recovery_resets_backoff() -> None:
    service, fetcher, clock = build()
    fetcher.fail = True
    await service.quotes(["ETH/USDT"])
    fetcher.fail = False
    clock.advance(3)
    (quote,) = await service.quotes(["ETH/USDT"])
    assert quote.change_24h_pct == 1.5
    assert not quote.stale


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"lastPrice": "x", "openPrice": "1"},
        {"lastPrice": "0", "openPrice": "1"},
        {"lastPrice": "nan", "openPrice": "1"},
    ],
)
def test_parse_tick_rejects_garbage(payload: dict[str, str]) -> None:
    with pytest.raises((KeyError, ValueError)):
        parse_tick(payload)


def test_parse_tick_computes_change() -> None:
    tick = parse_tick({"lastPrice": "110", "openPrice": "100"})
    assert tick.price == 110 and round(tick.change_24h_pct, 6) == 10


async def test_junk_symbols_do_not_lock_out_real_pairs() -> None:
    service, fetcher, _ = build()
    fetcher.fail = True
    await service.quotes([f"JUNK{i}/USDT" for i in range(32)])
    fetcher.fail = False
    (quote,) = await service.quotes(["BTC/USDT"], allowed=frozenset({"BTC/USDT"}))
    assert quote.symbol == "BTC/USDT"
    assert len(service._entries) <= 32 and len(service._locks) <= 32


async def test_fresh_quotes_are_not_evicted_and_old_ones_are() -> None:
    service, _, clock = build()
    mine = frozenset([f"P{i}/USDT" for i in range(32)] + ["NEW/USDT"])
    await service.quotes(sorted(mine - {"NEW/USDT"}), allowed=mine)
    assert await service.quotes(["NEW/USDT"], allowed=mine) == []
    clock.advance(121)
    assert [q.symbol for q in await service.quotes(["NEW/USDT"], allowed=mine)] == ["NEW/USDT"]
    assert len(service._entries) == 32


async def test_unexpected_error_is_swallowed() -> None:
    async def boom(symbol: str) -> RawTick:
        raise RuntimeError("segredo")

    service = TickerService(boom, Clock())
    assert await service.quotes(["BTC/USDT"]) == []


async def test_allowlist_blocks_unknown_pairs_before_any_upstream_call() -> None:
    service, fetcher, _ = build()
    quotes = await service.quotes(["BTC/USDT", "FAKE/USDT"], allowed=frozenset({"BTC/USDT"}))
    assert [q.symbol for q in quotes] == ["BTC/USDT"] and fetcher.calls == ["BTC/USDT"]


async def test_rejected_symbol_is_cached_for_a_minute_and_frees_its_slot() -> None:
    calls: list[str] = []

    async def reject(symbol: str) -> RawTick:
        calls.append(symbol)
        raise ValueError("invalid symbol")

    clock = Clock()
    service = TickerService(reject, clock)
    for _ in range(5):
        assert await service.quotes(["XXX/USDT"]) == []
    assert calls == ["XXX/USDT"] and not service._entries
    clock.advance(61)
    await service.quotes(["XXX/USDT"])
    assert len(calls) == 2


async def test_unvouched_new_symbols_have_a_per_minute_probe_ceiling() -> None:
    service, fetcher, clock = build()
    await service.quotes([f"S{i}/USDT" for i in range(40)])
    assert len(fetcher.calls) == 16
    clock.advance(61)
    await service.quotes(["LATE/USDT"])
    assert len(fetcher.calls) == 17
