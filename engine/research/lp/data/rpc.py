from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engine.research.lp.data.http import USER_AGENT

ALLOWED_METHODS = frozenset({"eth_call", "eth_getLogs", "eth_blockNumber"})
BATCH_SIZE = 10
RETRY_CODES = (-32005, -32007, 429)

RpcTransport = Callable[[str, bytes], tuple[int, bytes]]

PUBLIC_RPC: dict[str, str] = {
    "arbitrum": "https://arb1.arbitrum.io/rpc",
    "base": "https://mainnet.base.org",
    "optimism": "https://mainnet.optimism.io",
    "polygon_pos": "https://polygon-bor-rpc.publicnode.com",
    "unichain": "https://mainnet.unichain.org",
}

LOG_CHUNK_BLOCKS: dict[str, int] = {
    "arbitrum": 90_000,
    "base": 9_000,
    "optimism": 20_000,
    "polygon_pos": 10_000,
    "unichain": 5_000,
}


def urllib_rpc_transport(url: str, payload: bytes) -> tuple[int, bytes]:
    if not url.startswith("https://"):
        raise ValueError("apenas HTTPS")
    req = urllib.request.Request(  # noqa: S310
        url, data=payload, headers={"Content-Type": "application/json", "User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:  # noqa: S310
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@dataclass(frozen=True, slots=True)
class RpcLogEntry:
    method: str
    sha256: str
    from_cache: bool


class RpcError(RuntimeError):
    pass


class PublicRpc:
    """JSON-RPC SOMENTE LEITURA: so eth_call, eth_getLogs e eth_blockNumber; sem chave; intervalo minimo."""

    def __init__(
        self,
        url: str,
        cache_dir: Path | None = None,
        min_interval_s: float = 0.35,
        transport: RpcTransport = urllib_rpc_transport,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.url = url
        self._dir = cache_dir
        self._interval = min_interval_s
        self._transport = transport
        self._sleep = sleep
        self._clock = clock
        self._last = 0.0
        self.log: list[RpcLogEntry] = []

    def request(self, method: str, params: Sequence[Any]) -> Any:
        return self._send([self._body(method, params, 1)])[0]

    def batch_calls(self, calls: Sequence[tuple[str, Sequence[Any]]]) -> list[Any]:
        out: list[Any] = []
        for i in range(0, len(calls), BATCH_SIZE):
            chunk = calls[i : i + BATCH_SIZE]
            bodies = [self._body(m, p, k) for k, (m, p) in enumerate(chunk)]
            try:
                out += self._send(bodies)
            except RpcError:
                out += [self._send([{**b, "id": 1}])[0] for b in bodies]
        return out

    @staticmethod
    def _body(method: str, params: Sequence[Any], ident: int) -> dict[str, Any]:
        if method not in ALLOWED_METHODS:
            raise PermissionError(f"metodo RPC nao permitido (somente leitura): {method}")
        return {"jsonrpc": "2.0", "id": ident, "method": method, "params": list(params)}

    def _send(self, bodies: list[dict[str, Any]]) -> list[Any]:
        payload = json.dumps(bodies if len(bodies) > 1 else bodies[0], separators=(",", ":"))
        digest = hashlib.sha256((self.url + payload).encode()).hexdigest()
        cacheable = self._dir is not None and '"latest"' not in payload and "eth_blockNumber" not in payload
        path = self._dir / f"{digest}.json" if self._dir and cacheable else None
        if path is not None and path.exists():
            self.log.append(RpcLogEntry(bodies[0]["method"], digest, True))
            return list(json.loads(path.read_text(encoding="utf-8")))
        results = self._post(payload, len(bodies))
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(results), encoding="utf-8")
        self.log.append(RpcLogEntry(bodies[0]["method"], digest, False))
        return results

    def _post(self, payload: str, count: int) -> list[Any]:
        for attempt in range(6):
            wait = self._interval * (1 + count / 5) - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
            self._last = self._clock()
            status, raw = self._transport(self.url, payload.encode())
            if status in (429, 500, 502, 503, 504):
                self._sleep(3.0 * 2**attempt)
                continue
            if status != 200:
                raise RpcError(f"HTTP {status} em {self.url}")
            doc = json.loads(raw)
            items = doc if isinstance(doc, list) else [doc]
            items.sort(key=lambda d: d.get("id", 0))
            if len(items) != count:
                raise RpcError("resposta com quantidade inesperada de itens")
            for item in items:
                if "error" in item:
                    code = item["error"].get("code")
                    if code in RETRY_CODES:
                        self._sleep(3.0 * 2**attempt)
                        break
                    raise RpcError(str(item["error"]))
            else:
                return [item["result"] for item in items]
        raise RpcError(f"sem sucesso apos tentativas: {self.url}")
