from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from engine.research.lp.data.coins import CoinsClient, parse_prices
from engine.research.lp.data.http import CachedGetClient
from engine.research.lp.data.llama import (
    LlamaClient,
    LlamaPool,
    PoolDay,
    parse_fee,
    parse_history,
    parse_pools,
)
from engine.research.lp.dataset import (
    build_days,
    candidates,
    classify,
    is_contiguous,
    pool_from_json,
    pool_to_json,
    rank_by_start_tvl,
    series_hash,
)
from engine.research.lp.types import PoolSeries
from tests.unit.research_lp.helpers import make_days

POOLS_BODY = {
    "data": [
        {
            "pool": "a",
            "chain": "Ethereum",
            "project": "uniswap-v3",
            "symbol": "WETH-USDC",
            "poolMeta": "0.05%",
            "tvlUsd": 5e7,
            "underlyingTokens": ["0xAA", "0xBB"],
        },
        {
            "pool": "b",
            "chain": "Ethereum",
            "project": "uniswap-v3",
            "symbol": "PEPE-WETH",
            "poolMeta": "0.3%",
            "tvlUsd": 9e7,
            "underlyingTokens": ["0xCC", "0xAA"],
        },
        {
            "pool": "c",
            "chain": "Ethereum",
            "project": "aave-v3",
            "symbol": "USDC",
            "poolMeta": None,
            "tvlUsd": 1e9,
            "underlyingTokens": None,
        },
        {
            "pool": "d",
            "chain": "Ethereum",
            "project": "uniswap-v3",
            "symbol": "USDC-USDT",
            "poolMeta": "0.01%",
            "tvlUsd": 3e7,
            "underlyingTokens": ["0xBB", "0xDD"],
        },
    ]
}


class FakeTransport:
    def __init__(self, responses: list[tuple[int, bytes, dict[str, str]]]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, url: str, headers: dict[str, str]) -> tuple[int, bytes, dict[str, str]]:
        self.calls.append(url)
        return self.responses.pop(0)


def _client(
    tmp_path: Path, transport: FakeTransport, ttl: float = 24.0, now: list[float] | None = None
) -> CachedGetClient:
    clock = now if now is not None else [1000.0]
    return CachedGetClient(
        tmp_path,
        ttl_hours=ttl,
        min_interval_s=0.0,
        transport=transport,
        sleep=lambda s: None,
        clock=lambda: clock[0],
    )


def test_cache_evita_segunda_requisicao_e_registra_hash(tmp_path: Path) -> None:
    body = json.dumps(POOLS_BODY).encode()
    transport = FakeTransport([(200, body, {})])
    client = _client(tmp_path, transport)
    first = client.get_json("https://x.test/a")
    second = client.get_json("https://x.test/a")
    assert len(transport.calls) == 1
    assert first.from_cache is False
    assert second.from_cache is True
    assert first.sha256 == second.sha256


def test_cache_expira_pelo_ttl(tmp_path: Path) -> None:
    transport = FakeTransport([(200, b"{}", {}), (200, b'{"v": 2}', {})])
    now = [0.0]
    client = _client(tmp_path, transport, ttl=1.0, now=now)
    client.get_json("https://x.test/a")
    path = next(tmp_path.glob("*.json"))
    now[0] = path.stat().st_mtime + 7200
    assert client.get_json("https://x.test/a").body == {"v": 2}


def test_429_faz_backoff_e_tenta_de_novo(tmp_path: Path) -> None:
    sleeps: list[float] = []
    transport = FakeTransport([(429, b"{}", {"Retry-After": "7"}), (200, b'{"ok": 1}', {})])
    client = CachedGetClient(tmp_path, min_interval_s=0.0, transport=transport, sleep=sleeps.append)
    assert client.get_json("https://x.test/z").body == {"ok": 1}
    assert max(sleeps) >= 7


def test_erro_401_nao_e_repetido(tmp_path: Path) -> None:
    transport = FakeTransport([(401, b"{}", {})])
    with pytest.raises(RuntimeError, match="401"):
        _client(tmp_path, transport).get_json("https://x.test/paid")
    assert len(transport.calls) == 1


def test_transporte_recusa_http_puro() -> None:
    from engine.research.lp.data.http import urllib_transport

    with pytest.raises(ValueError, match="HTTPS"):
        urllib_transport("http://x.test", {})


def test_llama_parse_e_selecao_objetiva() -> None:
    pools = parse_pools(POOLS_BODY)
    assert [p.pool_id for p in pools] == ["a", "b", "d"]
    assert pools[0].fee == pytest.approx(0.0005)
    assert [p.pool_id for p in candidates(pools, "Ethereum", 10)] == ["d", "a"]
    assert parse_fee("0.3%") == pytest.approx(0.003)
    assert parse_fee(None) is None
    assert parse_fee("abc") is None


def test_classificacao_de_pares() -> None:
    assert classify("USDC-USDT") == "estavel"
    assert classify("WETH-USDC") == "volatil_maior"
    assert classify("LINK-WETH") == "volatil_media"
    assert classify("PEPE-WETH") is None
    assert classify("USDC") is None


def test_llama_history_e_cliente(tmp_path: Path) -> None:
    body = {
        "data": [
            {"timestamp": "2026-01-01T00:00:00.000Z", "tvlUsd": 100.0, "apyBase": 10.0},
            {"timestamp": "2026-01-02T00:00:00.000Z", "tvlUsd": 110.0, "apyBase": None},
            {"timestamp": "2026-01-03T00:00:00.000Z", "tvlUsd": None, "apyBase": 1.0},
        ]
    }
    assert parse_history(body) == {
        date(2026, 1, 1): PoolDay(100.0, 10.0),
        date(2026, 1, 2): PoolDay(110.0, None),
    }
    transport = FakeTransport([(200, json.dumps(body).encode(), {})])
    assert len(LlamaClient(_client(tmp_path, transport)).history("p")) == 2


def test_coins_parse_e_paginacao(tmp_path: Path) -> None:
    ts = int(datetime(2026, 1, 1, 0, 0, 5, tzinfo=UTC).timestamp())
    body = {"coins": {"ethereum:0xaa": {"prices": [{"timestamp": ts, "price": 2.5}]}}}
    assert parse_prices(body, "ethereum:0xaa") == {date(2026, 1, 1): 2.5}
    transport = FakeTransport([(200, json.dumps(body).encode(), {}) for _ in range(3)])
    got = CoinsClient(_client(tmp_path, transport)).prices(
        "Ethereum", "0xaa", date(2026, 1, 1), date(2027, 6, 1)
    )
    assert len(transport.calls) == 3
    assert got == {date(2026, 1, 1): 2.5}


def test_build_days_volume_implicito_e_preenchimento_de_lacunas() -> None:
    d0 = date(2026, 1, 1)
    n = 20
    base = {d0 + timedelta(i): 2000.0 + i for i in range(n)}
    quote = {d0 + timedelta(i): 1.0 for i in range(n)}
    hist = {d0 + timedelta(i): PoolDay(1e6, 36.5) for i in range(n)}
    del hist[d0 + timedelta(2)]
    for i in range(5, 15):
        del hist[d0 + timedelta(i)]
    days = build_days(base, quote, hist, 0.003, d0, d0 + timedelta(n - 1))
    got = [(d.day - d0).days for d in days]
    assert got == list(range(12)) + list(range(15, 20))
    assert days[0].volume_usd == pytest.approx(1e6 * 0.001 / 0.003)
    assert days[2].volume_usd == days[1].volume_usd
    assert days[1].low == pytest.approx(2000.0)
    assert days[1].high == pytest.approx(2001.0)
    assert not is_contiguous(days)
    assert is_contiguous(days, max_gap=4)


def test_ranking_por_tvl_inicial_ignora_pools_novos_e_pequenos() -> None:
    start = date(2026, 1, 1)

    def pool(i: str) -> LlamaPool:
        return LlamaPool(i, "Ethereum", "WETH-USDC", 0.003, 1.0, ("a", "b"))

    items = [
        (pool("grande"), {start: PoolDay(9e7, 1.0)}),
        (pool("pequeno"), {start: PoolDay(1e5, 1.0)}),
        (pool("novo"), {start + timedelta(30): PoolDay(5e8, 1.0)}),
        (pool("medio"), {start + timedelta(2): PoolDay(2e7, 1.0)}),
    ]
    assert [p.pool_id for p, _ in rank_by_start_tvl(items, start, 1e6)] == ["grande", "medio"]


def test_serializacao_ida_e_volta_e_hash_estavel() -> None:
    days = tuple(make_days([1.0, 1.1, 1.2]))
    pool = PoolSeries("id", "Base", "0xp", "WETH-USDC 0.05%", 0.0005, "volatil_maior", days, 10.0, 12.0)
    doc = pool_to_json(pool)
    again = pool_from_json(json.loads(json.dumps(doc)))
    assert again == pool
    assert doc["sha256"] == series_hash(days)
    assert pool.tvl_ratio == pytest.approx(1.2)


def test_coins_arredonda_deriva_em_torno_da_meia_noite() -> None:
    def ts(d: datetime) -> int:
        return int(d.timestamp())

    rows = [
        {"timestamp": ts(datetime(2026, 1, 1, 23, 59, 55, tzinfo=UTC)), "price": 1.0},
        {"timestamp": ts(datetime(2026, 1, 3, 0, 0, 3, tzinfo=UTC)), "price": 2.0},
    ]
    got = parse_prices({"coins": {"k": {"prices": rows}}}, "k")
    assert got == {date(2026, 1, 2): 1.0, date(2026, 1, 3): 2.0}
