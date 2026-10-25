from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from engine.research.lp.data.coins import CoinsClient
from engine.research.lp.data.http import CachedGetClient
from engine.research.lp.data.llama import LlamaClient

pytestmark = pytest.mark.integration

USDC_WETH_005 = "665dc8bc-c79d-4800-97f7-304bf368e547"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"


def test_apis_publicas_sem_chave_respondem(tmp_path: Path) -> None:
    http = CachedGetClient(tmp_path)
    try:
        history = LlamaClient(http).history(USDC_WETH_005)
        end = datetime.now(UTC).date() - timedelta(days=1)
        prices = CoinsClient(http).prices("Ethereum", WETH, end - timedelta(days=30), end)
    except (OSError, RuntimeError) as exc:
        pytest.skip(f"rede/API indisponivel: {exc}")
    assert len(history) > 300
    assert any(d.apy_base is not None for d in history.values())
    assert len(prices) >= 25
    assert all(p > 0 for p in prices.values())
