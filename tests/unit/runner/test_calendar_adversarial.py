import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from api.services.ticker import RawTick, TickerService
from engine.application.risk import EventCalendar, EventWindow
from engine.runner.calendar_sources import CalendarSourceError, collect_sources, parse_bls
from engine.runner.calendar_store import regression_problem
from tests.unit.runner.test_calendar_update import NOW, FakeFetcher, text


def test_bls_ano_zero_deve_virar_erro_de_fonte_e_nao_excecao_solta() -> None:
    # Arrange
    page = text("cpi.html").replace("October 2026", "October 0000", 1)

    # Act / Assert
    with pytest.raises(CalendarSourceError):
        parse_bls(page, "cpi", "CPI", NOW)


def test_coleta_nao_propaga_excecao_inesperada_de_parser() -> None:
    # Arrange
    page = text("cpi.html").replace("October 2026", "October 0000", 1)
    fetcher = FakeFetcher(cpi=page)

    # Act
    ok, errors = asyncio.run(collect_sources(fetcher, NOW))

    # Assert
    assert "cpi" in errors
    assert ok is not None


def test_bls_primeira_linha_removida_e_recusada_mesmo_com_lacuna_pequena() -> None:
    # Arrange: remove a 1a linha (Nov 10); com now 10 dias depois a lacuna cai para <45 dias, mas o mes de
    # referencia da 1a linha restante (novembro) ja e posterior ao mes de now.
    page = text("cpi.html")
    first = page.index('<tr class="release-list-even-row">')
    second = page.index('<tr class="release-list-odd-row">')
    truncated = page[:first] + page[second:]
    now = datetime(2026, 10, 30, 20, 0, tzinfo=UTC)

    # Act / Assert
    with pytest.raises(CalendarSourceError):
        parse_bls(truncated, "cpi", "CPI", now)


def test_loaded_at_futuro_do_arquivo_antigo_nao_trava_atualizacoes_futuras() -> None:
    # Arrange
    future = datetime.now(UTC) + timedelta(days=30)
    old = EventCalendar(
        loaded_at=future,
        valid_until=future + timedelta(days=60),
        windows=(EventWindow(start=future, end=future + timedelta(hours=1)),),
    )
    new = old.model_copy(update={"loaded_at": datetime.now(UTC)})

    # Act
    problem = regression_problem(old, new)

    # Assert
    assert problem is None


def test_ticker_pares_inventados_nao_tem_teto_de_chamadas_upstream() -> None:
    # Arrange
    calls: list[str] = []

    async def fetch(symbol: str) -> RawTick:
        calls.append(symbol)
        raise ValueError("invalid symbol")

    service = TickerService(fetch, lambda: datetime(2026, 1, 1, tzinfo=UTC))

    # Act
    for i in range(200):
        asyncio.run(service.quotes([f"JUNK{i}/USDT"]))

    # Assert
    assert len(calls) <= 32, f"{len(calls)} chamadas upstream com pares inventados"
