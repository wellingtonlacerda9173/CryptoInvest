from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

USER_AGENT = "cryptoinvest-lp-research/0.1 (pesquisa; somente leitura)"


@dataclass(frozen=True, slots=True)
class Fetched:
    url: str
    body: Any
    fetched_at: str
    sha256: str
    from_cache: bool


Transport = Callable[[str, dict[str, str]], tuple[int, bytes, dict[str, str]]]


def urllib_transport(url: str, headers: dict[str, str]) -> tuple[int, bytes, dict[str, str]]:
    if not url.startswith("https://"):
        raise ValueError("apenas HTTPS")
    req = urllib.request.Request(url, headers=headers, method="GET")  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


class CachedGetClient:
    """GET com cache em disco (TTL), intervalo minimo entre requisicoes e backoff em 429/5xx."""

    def __init__(
        self,
        cache_dir: Path,
        ttl_hours: float = 24.0,
        min_interval_s: float = 2.2,
        transport: Transport = urllib_transport,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._dir = cache_dir
        self._ttl = ttl_hours * 3600
        self._interval = min_interval_s
        self._transport = transport
        self._sleep = sleep
        self._clock = clock
        self._last = 0.0
        self.log: list[Fetched] = []

    def get_json(self, url: str, headers: dict[str, str] | None = None) -> Fetched:
        key = hashlib.sha256(url.encode()).hexdigest()
        path = self._dir / f"{key}.json"
        cached = self._read_cache(path)
        if cached is not None:
            self.log.append(cached)
            return cached
        raw = self._download(url, {"User-Agent": USER_AGENT, **(headers or {})})
        fetched = Fetched(
            url=url,
            body=json.loads(raw),
            fetched_at=datetime.now(UTC).isoformat(timespec="seconds"),
            sha256=hashlib.sha256(raw).hexdigest(),
            from_cache=False,
        )
        self._dir.mkdir(parents=True, exist_ok=True)
        doc = {"url": url, "fetched_at": fetched.fetched_at, "sha256": fetched.sha256, "body": fetched.body}
        path.write_text(json.dumps(doc), encoding="utf-8")
        self.log.append(fetched)
        return fetched

    def _read_cache(self, path: Path) -> Fetched | None:
        if not path.exists() or self._clock() - path.stat().st_mtime > self._ttl:
            return None
        doc = json.loads(path.read_text(encoding="utf-8"))
        return Fetched(doc["url"], doc["body"], doc["fetched_at"], doc["sha256"], True)

    def _download(self, url: str, headers: dict[str, str]) -> bytes:
        for attempt in range(6):
            wait = self._interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
            self._last = self._clock()
            status, body, resp_headers = self._transport(url, headers)
            if status == 200:
                return body
            if status in (429, 500, 502, 503, 504):
                retry = float(resp_headers.get("Retry-After", 0) or 0)
                self._sleep(max(retry, 5.0 * 2**attempt))
                continue
            raise RuntimeError(f"HTTP {status} em {url}")
        raise RuntimeError(f"Sem sucesso apos tentativas: {url}")
