"""Fontes oficiais publicas (sem chave) do calendario de eventos de volatilidade.

Regra unica: qualquer duvida (estrutura mudou, data ambigua, contagem estranha) levanta
`CalendarSourceError` e a fonte inteira e descartada. Nada aqui estende cobertura "por palpite".

Horarios padrao (tambem em config/README.md), convertidos com `zoneinfo` (horario de verao correto):
- FOMC: decisao as 14:00 America/New_York; janela [decisao, decisao + 90 min].
- BLS (payroll/CPI): 08:30 America/New_York; janela [divulgacao, divulgacao + 60 min].
- Deribit: vencimento mensal/trimestral as 08:00 UTC; janela [vencimento - 1 h, vencimento + 1 h].
O gate ainda acrescenta suas proprias margens antes/depois de cada janela.
"""

import asyncio
import calendar as pycal
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import aiohttp

from engine.application.risk import EventWindow

ET = ZoneInfo("America/New_York")
USER_AGENT = (
    "CryptoInvestCalendarUpdater/1.0 (paper trading; contato: wellington.lacerda@abilitytecnologia.com.br)"
)
MAX_BYTES = 8 * 1024 * 1024
TIMEOUT_SECONDS = 20.0
POLITE_DELAY_SECONDS = 2.0

FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
EMPSIT_URL = "https://www.bls.gov/schedule/news_release/empsit.htm"
CPI_URL = "https://www.bls.gov/schedule/news_release/cpi.htm"
DERIBIT_URL = (
    "https://www.deribit.com/api/v2/public/get_instruments?currency={currency}&kind=option&expired=false"
)
ALLOWED_HOSTS = frozenset({"www.federalreserve.gov", "www.bls.gov", "www.deribit.com"})

FOMC_DECISION = time(14, 0)
FOMC_AFTER = timedelta(minutes=90)
BLS_RELEASE = time(8, 30)
BLS_AFTER = timedelta(hours=1)
DERIBIT_EXPIRY = time(8, 0)
DERIBIT_MARGIN = timedelta(hours=1)
FOMC_MEETINGS_PER_YEAR = 8
MIN_YEAR, MAX_YEAR = 1990, 2100
MAX_CLOCK_DRIFT = timedelta(minutes=5)
BLS_MAX_FIRST_RELEASE_GAP = timedelta(days=45)

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_PT = ("Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez")


class CalendarSourceError(Exception):
    """Fonte indisponivel, alterada ou ambigua: nao confirma nada."""


@dataclass(frozen=True, slots=True)
class SourceResult:
    name: str
    covered_until: datetime
    windows: tuple[EventWindow, ...]


class Fetcher(Protocol):
    async def get(self, url: str) -> str: ...


class HttpsFetcher:
    """GET somente HTTPS, hosts fixos, sem redirecionamento, com timeout e teto de tamanho."""

    def __init__(self, delay_seconds: float = POLITE_DELAY_SECONDS) -> None:
        self._delay = delay_seconds
        self._first = True
        self._server_clock: list[tuple[datetime, datetime]] = []

    def clock_drift(self) -> timedelta | None:
        """Maior |relogio do host - header Date do servidor| observado; None sem nenhuma resposta datada."""
        return max((abs(local - server) for local, server in self._server_clock), default=None)

    def _note_server_date(self, header: str | None) -> None:
        if not header:
            return
        try:
            server = parsedate_to_datetime(header).astimezone(UTC)
        except (TypeError, ValueError):
            return
        self._server_clock.append((datetime.now(UTC), server))

    def server_now(self) -> datetime | None:
        return min(
            (server + (datetime.now(UTC) - local) for local, server in self._server_clock), default=None
        )

    async def get(self, url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS:
            raise CalendarSourceError(f"url fora da lista permitida: {parts.hostname}")
        if not self._first:
            await asyncio.sleep(self._delay)
        self._first = False
        timeout = aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)
        headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/json"}
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout, headers=headers) as session,
                session.get(url, allow_redirects=False) as response,
            ):
                if response.status != 200:
                    raise CalendarSourceError(f"{parts.hostname}: HTTP {response.status}")
                self._note_server_date(response.headers.get("Date"))
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        raise CalendarSourceError(f"{parts.hostname}: resposta acima de {MAX_BYTES} bytes")
                return body.decode("utf-8")
        except (aiohttp.ClientError, TimeoutError, UnicodeDecodeError) as error:
            raise CalendarSourceError(f"{parts.hostname}: {type(error).__name__}") from error


def _utc(day: date, at: time, zone: ZoneInfo | None) -> datetime:
    local = datetime.combine(day, at, tzinfo=zone or UTC)
    return local.astimezone(UTC)


def _end_of_day(day: date) -> datetime:
    return datetime.combine(day, time(23, 59, 59), tzinfo=UTC)


def _month_index(token: str) -> int:
    key = token.strip().lower()[:3]
    if key not in _MONTHS:
        raise CalendarSourceError(f"mes desconhecido: {token!r}")
    return _MONTHS.index(key) + 1


def _plain(fragment: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()


def parse_fomc(html: str, now: datetime) -> SourceResult:
    sections = list(re.finditer(r'<h4><a id="\d+">(\d{4}) FOMC Meetings</a></h4>', html))
    if not sections:
        raise CalendarSourceError("FOMC: nenhuma secao de ano encontrada")
    decisions: dict[int, list[date]] = {}
    for position, section in enumerate(sections):
        year = int(section.group(1))
        if not MIN_YEAR <= year <= MAX_YEAR:
            raise CalendarSourceError(f"FOMC: ano fora do intervalo plausivel {year}")
        end = sections[position + 1].start() if position + 1 < len(sections) else len(html)
        block = html[section.end() : end]
        rows = re.findall(
            r"fomc-meeting__month[^>]*>\s*<strong>(.*?)</strong>.*?fomc-meeting__date[^>]*>(.*?)</div>",
            block,
            re.S,
        )
        if len(rows) != block.count("fomc-meeting__month"):
            raise CalendarSourceError(f"FOMC {year}: linha de reuniao sem data legivel")
        days = [_fomc_decision(year, _plain(month), _plain(raw)) for month, raw in rows]
        decisions[year] = [d for d in days if d is not None]
    covered_years: list[int] = []
    year = now.year
    while len(decisions.get(year, ())) == FOMC_MEETINGS_PER_YEAR:
        scheduled = decisions[year]
        if scheduled != sorted(set(scheduled)):
            raise CalendarSourceError(f"FOMC {year}: datas fora de ordem ou repetidas")
        covered_years.append(year)
        year += 1
    if not covered_years:
        raise CalendarSourceError(f"FOMC {now.year}: calendario do ano ausente ou incompleto")
    windows = []
    for covered in covered_years:
        for day in decisions[covered]:
            start = _utc(day, FOMC_DECISION, ET)
            if start + FOMC_AFTER > now:
                windows.append(
                    EventWindow(
                        start=start,
                        end=start + FOMC_AFTER,
                        label=f"FOMC decisao de juros ({_PT[day.month - 1]})",
                    )
                )
    return SourceResult("fomc", _end_of_day(date(covered_years[-1], 12, 31)), tuple(windows))


def _fomc_decision(year: int, month_text: str, date_text: str) -> date | None:
    """Dia da decisao (ultimo dia da reuniao). Voto por notacao: None. Outro formato: erro."""
    if "notation vote" in date_text:
        return None
    match = re.fullmatch(r"(\d{1,2})-(\d{1,2})\*?", date_text)
    months = [_month_index(m) for m in month_text.split("/")]
    if match is None or len(months) not in (1, 2):
        raise CalendarSourceError(f"FOMC {year}: formato de data inesperado {month_text!r} {date_text!r}")
    first_day, last_day = int(match.group(1)), int(match.group(2))
    if len(months) == 1:
        if last_day <= first_day:
            raise CalendarSourceError(f"FOMC {year}: intervalo invalido {date_text!r}")
        month, decision_year = months[0], year
    else:
        if last_day >= first_day or months[1] != months[0] % 12 + 1:
            raise CalendarSourceError(f"FOMC {year}: intervalo entre meses invalido {date_text!r}")
        month = months[1]
        decision_year = year + 1 if months[1] < months[0] else year
    try:
        return date(decision_year, month, last_day)
    except ValueError as error:
        raise CalendarSourceError(f"FOMC {year}: data inexistente {date_text!r}") from error


def parse_bls(html: str, name: str, label: str, now: datetime) -> SourceResult:
    start = html.find('<table class="release-list"')
    end = html.find("</table>", start)
    if start < 0 or end < 0:
        raise CalendarSourceError(f"BLS {name}: tabela de agenda ausente")
    table = html[start:end]
    headers = [_plain(h) for h in re.findall(r"<th>(.*?)</th>", table, re.S)]
    if headers != ["Reference Month", "Release Date", "Release Time"]:
        raise CalendarSourceError(f"BLS {name}: cabecalho inesperado {headers}")
    cells = [
        tuple(_plain(c) for c in row)
        for row in re.findall(r"<td>(.*?)</td>\s*<td>(.*?)</td>\s*<td>(.*?)</td>", table, re.S)
    ]
    if len(cells) != table.count("<td>") // 3 or not cells:
        raise CalendarSourceError(f"BLS {name}: linhas ilegiveis")
    releases: list[date] = []
    previous_reference: tuple[int, int] | None = None
    for reference, released, at in cells:
        ref = re.fullmatch(r"([A-Za-z]+) (\d{4})", reference)
        rel = re.fullmatch(r"([A-Za-z]{3,5})\.? (\d{1,2}), (\d{4})", released)
        if ref is None or rel is None or at != "08:30 AM":
            raise CalendarSourceError(f"BLS {name}: linha ambigua {reference!r} {released!r} {at!r}")
        ref_key = (int(ref.group(2)), _month_index(ref.group(1)))
        try:
            day = date(int(rel.group(3)), _month_index(rel.group(1)), int(rel.group(2)))
        except ValueError as error:
            raise CalendarSourceError(f"BLS {name}: data inexistente {released!r}") from error
        if not (MIN_YEAR <= ref_key[0] <= MAX_YEAR and MIN_YEAR <= day.year <= MAX_YEAR):
            raise CalendarSourceError(f"BLS {name}: ano fora do intervalo plausivel {reference!r}")
        ref_end = date(ref_key[0], ref_key[1], pycal.monthrange(*ref_key)[1])
        if not ref_end < day <= ref_end + timedelta(days=60):
            raise CalendarSourceError(
                f"BLS {name}: divulgacao incoerente com o mes de referencia {reference!r}"
            )
        if previous_reference is None and ref_key > (now.year, now.month):
            raise CalendarSourceError(f"BLS {name}: agenda truncada (divulgacao anterior ausente)")
        if previous_reference is not None:
            expected = (previous_reference[0] + (previous_reference[1] == 12), previous_reference[1] % 12 + 1)
            if ref_key != expected or day <= releases[-1]:
                raise CalendarSourceError(f"BLS {name}: meses nao consecutivos ou datas fora de ordem")
        previous_reference = ref_key
        releases.append(day)
    if _utc(releases[0], BLS_RELEASE, ET) - now > BLS_MAX_FIRST_RELEASE_GAP:
        raise CalendarSourceError(
            f"BLS {name}: primeira divulgacao listada distante demais (agenda incompleta?)"
        )
    windows = tuple(
        EventWindow(start=moment, end=moment + BLS_AFTER, label=f"EUA {label} ({_PT[day.month - 1]})")
        for day in releases
        if (moment := _utc(day, BLS_RELEASE, ET)) + BLS_AFTER > now
    )
    return SourceResult(name, _end_of_day(releases[-1]), windows)


def _last_friday(year: int, month: int) -> date:
    last = date(year, month, pycal.monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - 4) % 7)


def parse_deribit(payloads: dict[str, str], now: datetime) -> SourceResult:
    """Vencimentos mensais (ultima sexta, 08:00 UTC) listados para BTC E ETH; cobertura = meses contiguos."""
    listed: list[set[datetime]] = []
    for currency, text in sorted(payloads.items()):
        try:
            result = json.loads(text)["result"]
            stamps = {
                datetime.fromtimestamp(item["expiration_timestamp"] / 1000, UTC)
                for item in result
                if item["kind"] == "option" and item["base_currency"] == currency
            }
        except (ValueError, KeyError, TypeError, OverflowError, OSError) as error:
            raise CalendarSourceError(f"Deribit {currency}: resposta inesperada") from error
        if any(not MIN_YEAR <= stamp.year <= MAX_YEAR for stamp in stamps):
            raise CalendarSourceError(f"Deribit {currency}: vencimento fora do intervalo plausivel")
        if not stamps:
            raise CalendarSourceError(f"Deribit {currency}: nenhum instrumento listado")
        listed.append(stamps)
    if len(listed) < 2:
        raise CalendarSourceError("Deribit: BTC e ETH sao ambos obrigatorios")
    confirmed = set.intersection(*listed)
    windows: list[EventWindow] = []
    covered: date | None = None
    year, month = now.year, now.month
    for _ in range(36):
        expiry = _utc(_last_friday(year, month), DERIBIT_EXPIRY, None)
        if expiry >= now:
            if expiry not in confirmed:
                break
            kind = "trimestral" if month % 3 == 0 else "mensal"
            windows.append(
                EventWindow(
                    start=expiry - DERIBIT_MARGIN,
                    end=expiry + DERIBIT_MARGIN,
                    label=f"Deribit vencimento {kind} BTC/ETH ({_PT[month - 1]})",
                )
            )
            covered = date(year, month, pycal.monthrange(year, month)[1])
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    if covered is None:
        raise CalendarSourceError("Deribit: vencimento do mes corrente nao confirmado")
    return SourceResult("deribit", _end_of_day(covered), tuple(windows))


async def collect_sources(fetcher: Fetcher, now: datetime) -> tuple[list[SourceResult], dict[str, str]]:
    """Roda todas as fontes: (ok, erros por fonte). Erro em qualquer uma => o chamador nao muda nada."""
    ok: list[SourceResult] = []
    errors: dict[str, str] = {}

    async def run(name: str, job: Callable[[], Awaitable[SourceResult]]) -> None:
        try:
            ok.append(await job())
        except CalendarSourceError as error:
            errors[name] = str(error)
        except Exception as error:
            errors[name] = f"erro inesperado: {type(error).__name__}"

    async def fomc() -> SourceResult:
        return parse_fomc(await fetcher.get(FOMC_URL), now)

    async def empsit() -> SourceResult:
        return parse_bls(await fetcher.get(EMPSIT_URL), "empsit", "payroll", now)

    async def cpi() -> SourceResult:
        return parse_bls(await fetcher.get(CPI_URL), "cpi", "CPI", now)

    async def deribit() -> SourceResult:
        payloads = {c: await fetcher.get(DERIBIT_URL.format(currency=c)) for c in ("BTC", "ETH")}
        return parse_deribit(payloads, now)

    for name, job in (("fomc", fomc), ("empsit", empsit), ("cpi", cpi), ("deribit", deribit)):
        await run(name, job)
    return ok, errors
