from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from engine.research.lp.data.http import CachedGetClient

BASE_URL = "https://api.geckoterminal.com/api/v2"
HEADERS = {"Accept": "application/json;version=20230302"}
PAGE_LIMIT = 1000

NETWORK_DEXES: dict[str, str] = {
    "arbitrum": "uniswap_v3_arbitrum",
    "base": "uniswap-v3-base",
    "optimism": "uniswap_v3_optimism",
    "polygon_pos": "uniswap_v3_polygon_pos",
    "unichain": "uniswap-v3-unichain",
}
NETWORK_LABEL = {
    "arbitrum": "Arbitrum",
    "base": "Base",
    "optimism": "OP Mainnet",
    "polygon_pos": "Polygon",
    "unichain": "Unichain",
}
_FEE = re.compile(r"(\d+(?:\.\d+)?)%\s*$")


@dataclass(frozen=True, slots=True)
class PoolInfo:
    network: str
    address: str
    name: str
    fee: float
    reserve_usd: float
    volume24h_usd: float
    created_at: str
    base_address: str
    quote_address: str

    @property
    def symbols(self) -> tuple[str, str]:
        left, _, right = self.name.rpartition(" / ")
        return left.strip(), _FEE.sub("", right).strip()


def parse_fee(name: str) -> float | None:
    m = _FEE.search(name)
    return float(m.group(1)) / 100 if m else None


def parse_pool(network: str, item: dict[str, Any]) -> PoolInfo | None:
    attrs = item["attributes"]
    fee = parse_fee(attrs["name"])
    if fee is None or " / " not in attrs["name"]:
        return None
    rel = item["relationships"]
    return PoolInfo(
        network=network,
        address=attrs["address"].lower(),
        name=attrs["name"],
        fee=fee,
        reserve_usd=float(attrs.get("reserve_in_usd") or 0),
        volume24h_usd=float((attrs.get("volume_usd") or {}).get("h24") or 0),
        created_at=attrs.get("pool_created_at") or "",
        base_address=rel["base_token"]["data"]["id"].split("_", 1)[1].lower(),
        quote_address=rel["quote_token"]["data"]["id"].split("_", 1)[1].lower(),
    )


class GeckoClient:
    """GeckoTerminal API publica (sem chave): pools por rede e OHLCV horario com volume em USD."""

    def __init__(self, http: CachedGetClient) -> None:
        self._http = http

    def top_pools(self, network: str, pages: int = 3) -> list[PoolInfo]:
        dex = NETWORK_DEXES[network]
        out: list[PoolInfo] = []
        for page in range(1, pages + 1):
            url = f"{BASE_URL}/networks/{network}/dexes/{dex}/pools?page={page}&sort=h24_volume_usd_desc"
            data = self._http.get_json(url, HEADERS).body["data"]
            out += [p for item in data if (p := parse_pool(network, item)) is not None]
            if len(data) < 20:
                break
        return out

    def ohlcv_hour(self, network: str, pool: str, volatile_side: str, hours: int) -> list[list[float]]:
        """Candles [ts, o, h, l, c, volume_usd] do token volatil em USD, mais antigo primeiro."""
        rows: dict[int, list[float]] = {}
        before: int | None = None
        while len(rows) < hours:
            url = (
                f"{BASE_URL}/networks/{network}/pools/{pool}/ohlcv/hour"
                f"?aggregate=1&limit={PAGE_LIMIT}&currency=usd&token={volatile_side}"
            )
            if before is not None:
                url += f"&before_timestamp={before}"
            try:
                page = self._http.get_json(url, HEADERS).body["data"]["attributes"]["ohlcv_list"]
            except RuntimeError:
                if rows:
                    break
                raise
            if not page:
                break
            rows.update({int(r[0]): [float(v) for v in r] for r in page})
            oldest = min(int(r[0]) for r in page)
            if before is not None and oldest >= before:
                break
            before = oldest
        return [rows[k] for k in sorted(rows)][-hours:]
