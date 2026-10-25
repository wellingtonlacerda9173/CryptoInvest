from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from engine.research.lp.data.http import CachedGetClient

BASE = "https://coins.llama.fi/chart"
COINS_CHAIN = {
    "Ethereum": "ethereum",
    "Arbitrum": "arbitrum",
    "Base": "base",
    "OP Mainnet": "optimism",
    "Polygon": "polygon",
}
CHUNK_DAYS = 240


def parse_prices(body: dict[str, Any], key: str) -> dict[date, float]:
    rows = body.get("coins", {}).get(key, {}).get("prices", [])
    # Os pontos "diarios" derivam em torno da meia-noite (23:59:5x / 00:00:0x): arredonda ao dia mais proximo.
    return {
        (datetime.fromtimestamp(r["timestamp"], UTC) + timedelta(hours=12)).date(): float(r["price"])
        for r in rows
    }


class CoinsClient:
    """Precos diarios em USD (DefiLlama coins, sem chave). Limite da API: 500 pontos por chamada."""

    def __init__(self, http: CachedGetClient) -> None:
        self._http = http

    def prices(self, chain: str, token: str, start: date, end: date) -> dict[date, float]:
        key = f"{COINS_CHAIN[chain]}:{token}"
        out: dict[date, float] = {}
        cursor = start
        while cursor <= end:
            ts = int(datetime(cursor.year, cursor.month, cursor.day, tzinfo=UTC).timestamp())
            url = f"{BASE}/{key}?start={ts}&span={CHUNK_DAYS}&period=1d"
            out.update(parse_prices(self._http.get_json(url).body, key))
            cursor += timedelta(days=CHUNK_DAYS)
        return out
