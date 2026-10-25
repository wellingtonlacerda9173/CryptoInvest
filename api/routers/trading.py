"""Leituras de trading: qualquer usuário autenticado (leitor e admin). Nenhuma rota aqui escreve."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Response

from api.deps import ContainerDep, UserDep
from api.errors import PROBLEM_RESPONSES
from api.schemas_trading import (
    AccountSummaryOut,
    BotStatusOut,
    CandlesOut,
    DecisionPage,
    EquitySeriesOut,
    GlossaryOut,
    LiveGateOut,
    PerformanceOut,
    PositionOut,
    RiskLimitOut,
    TickerOut,
    TickerQuoteOut,
    TradePage,
)
from api.services.livegate import LiveGateService, gate_out
from api.services.status import StatusService, current_run_id
from api.services.trading import TradeFilter, TradingReads
from engine.domain.types import Timeframe

# byte NUL em texto de busca derruba o asyncpg (500): recusado na borda com 422
NO_NUL = r"^[^\x00]*$"

router = APIRouter(tags=["trading"], responses=PROBLEM_RESPONSES)

ModeQuery = Annotated[
    Literal["paper", "live"], Query(description="Totais de modos diferentes nunca se misturam.")
]
SymbolQuery = Annotated[
    str | None, Query(pattern=r"^[A-Z0-9]{2,12}/[A-Z0-9]{2,12}$", description="Ex.: BTC/USDT")
]
RunQuery = Annotated[str | None, Query(max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")]


def _utc(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


@router.get(
    "/status",
    operation_id="getBotStatus",
    response_model=BotStatusOut,
    summary="Estado do robô, pausas, travas de risco e elegibilidade ao Live",
)
async def get_bot_status(_: UserDep, container: ContainerDep) -> BotStatusOut:
    return await StatusService(container).build()


@router.get(
    "/risk/limits",
    operation_id="getRiskLimits",
    response_model=list[RiskLimitOut],
    summary="Travas de risco: valor atual de limite, com a palavra de estado",
)
async def get_risk_limits(_: UserDep, container: ContainerDep) -> list[RiskLimitOut]:
    return (await StatusService(container).build()).limits


@router.get(
    "/account/summary",
    operation_id="getAccountSummary",
    response_model=AccountSummaryOut,
    summary="Saldo e resultado do dia, da semana e total",
)
async def get_account_summary(_: UserDep, container: ContainerDep) -> AccountSummaryOut:
    return await TradingReads(container).account()


@router.get(
    "/equity",
    operation_id="getEquitySeries",
    response_model=EquitySeriesOut,
    summary="Curva de capital (série reduzida a no máximo `points` pontos)",
)
async def get_equity_series(
    _: UserDep,
    container: ContainerDep,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    points: Annotated[int, Query(ge=10, le=1000)] = 300,
) -> EquitySeriesOut:
    return await TradingReads(container).equity_series(days=days, points=points)


@router.get(
    "/positions",
    operation_id="listPositions",
    response_model=list[PositionOut],
    summary="Posições abertas, com stop, alvo e proteção",
)
async def list_positions(_: UserDep, container: ContainerDep) -> list[PositionOut]:
    return await TradingReads(container).positions()


@router.get(
    "/trades",
    operation_id="listTrades",
    response_model=TradePage,
    summary="Operações encerradas (filtros, busca e paginação por cursor)",
)
async def list_trades(
    _: UserDep,
    container: ContainerDep,
    mode: ModeQuery = "paper",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[int | None, Query(ge=1, description="`next_cursor` da página anterior")] = None,
    run_id: RunQuery = None,
    symbol: SymbolQuery = None,
    strategy: Annotated[str | None, Query(max_length=100, pattern=NO_NUL)] = None,
    exit_reason: Annotated[
        Literal["stop_loss", "take_profit", "profit_protection", "signal", "manual"] | None, Query()
    ] = None,
    result: Annotated[Literal["win", "loss", "breakeven"] | None, Query()] = None,
    since: Annotated[datetime | None, Query(description="Fechadas a partir de (UTC)")] = None,
    until: Annotated[datetime | None, Query(description="Fechadas antes de (UTC)")] = None,
    q: Annotated[
        str | None,
        Query(max_length=60, pattern=NO_NUL, description="Busca por par, estratégia, id ou motivo"),
    ] = None,
) -> TradePage:
    return await TradingReads(container).trades(
        TradeFilter(mode, run_id, symbol, strategy, exit_reason, result, _utc(since), _utc(until), q),
        limit=limit,
        cursor=cursor,
    )


@router.get(
    "/trades/export",
    operation_id="exportTradesCsv",
    response_class=Response,
    responses={200: {"content": {"text/csv": {"schema": {"type": "string"}}}, "description": "CSV (UTF-8)"}},
    summary="Exporta as operações do filtro em CSV (o modo vai no nome e no cabeçalho)",
)
async def export_trades_csv(
    _: UserDep,
    container: ContainerDep,
    mode: ModeQuery = "paper",
    run_id: RunQuery = None,
    symbol: SymbolQuery = None,
    strategy: Annotated[str | None, Query(max_length=100, pattern=NO_NUL)] = None,
    exit_reason: Annotated[
        Literal["stop_loss", "take_profit", "profit_protection", "signal", "manual"] | None, Query()
    ] = None,
    result: Annotated[Literal["win", "loss", "breakeven"] | None, Query()] = None,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=60, pattern=NO_NUL)] = None,
) -> Response:
    body, filename = await TradingReads(container).trades_csv(
        TradeFilter(mode, run_id, symbol, strategy, exit_reason, result, _utc(since), _utc(until), q)
    )
    return Response(
        content=body.encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/decisions",
    operation_id="listDecisions",
    response_model=DecisionPage,
    summary="Feed de decisões, com o motivo em português simples",
)
async def list_decisions(
    _: UserDep,
    container: ContainerDep,
    mode: ModeQuery = "paper",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[int | None, Query(ge=1)] = None,
    run_id: RunQuery = None,
    traded: Annotated[
        bool | None, Query(description="Verdadeiro: só as que operaram; falso: as que não")
    ] = None,
    symbol: SymbolQuery = None,
    code: Annotated[str | None, Query(max_length=50, pattern=r"^[a-z0-9_]+$")] = None,
    since: Annotated[datetime | None, Query()] = None,
) -> DecisionPage:
    return await TradingReads(container).decisions(
        mode=mode,
        limit=limit,
        cursor=cursor,
        run_id=run_id,
        traded=traded,
        symbol=symbol,
        code=code,
        since=_utc(since),
    )


@router.get(
    "/performance",
    operation_id="getPerformance",
    response_model=PerformanceOut,
    summary="Desempenho do run: acerto, profit factor, payoff, drawdown, Sharpe e quebras",
)
async def get_performance(
    _: UserDep,
    container: ContainerDep,
    days: Annotated[
        int | None, Query(ge=1, le=3650, description="Últimos N dias; sem valor, o run inteiro")
    ] = None,
    run_id: RunQuery = None,
) -> PerformanceOut:
    return await TradingReads(container).performance(days=days, run_id=run_id)


@router.get(
    "/live-gate",
    operation_id="getLiveGate",
    response_model=LiveGateOut,
    summary="Trava do Live (somente leitura): critérios, valor atual e meta",
)
async def get_live_gate(_: UserDep, container: ContainerDep, run_id: RunQuery = None) -> LiveGateOut:
    evaluation = await LiveGateService(container).evaluate(run_id or await current_run_id(container))
    return gate_out(evaluation)


@router.get(
    "/market/candles",
    operation_id="listCandles",
    response_model=CandlesOut,
    summary="Velas do par (Timescale) com marcadores de barras elefante, entradas, saídas, stops e alvos",
)
async def list_candles(
    _: UserDep,
    container: ContainerDep,
    symbol: Annotated[str, Query(pattern=r"^[A-Z0-9]{2,12}/[A-Z0-9]{2,12}$", examples=["BTC/USDT"])],
    timeframe: Timeframe = Timeframe.H1,
    limit: Annotated[int, Query(ge=10, le=1000)] = 300,
    quiet: bool = False,
) -> CandlesOut:
    return await TradingReads(container).candles(symbol=symbol, timeframe=timeframe, limit=limit, quiet=quiet)


_PAIR = r"[A-Z0-9]{2,12}/[A-Z0-9]{2,12}"


@router.get(
    "/market/ticker",
    operation_id="getTicker",
    response_model=TickerOut,
    summary="Cotacao ao vivo (so visual) com cache curto; se a corretora falhar, a lista vem vazia",
)
async def get_ticker(
    _: UserDep,
    container: ContainerDep,
    symbols: Annotated[str, Query(pattern=rf"^{_PAIR}(,{_PAIR}){{0,7}}$", examples=["BTC/USDT,ETH/USDT"])],
) -> TickerOut:
    if container.ticker is None:
        return TickerOut(quotes=[])
    allowed = await TradingReads(container).ticker_symbols()
    quotes = await container.ticker.quotes(symbols.split(","), allowed=allowed)
    return TickerOut(quotes=[TickerQuoteOut.model_validate(q, from_attributes=True) for q in quotes])


@router.get(
    "/glossary",
    operation_id="getGlossary",
    response_model=GlossaryOut,
    summary="Glossário, estados do robô e motivos de decisão em português simples",
)
async def get_glossary(_: UserDep) -> GlossaryOut:
    return TradingReads.glossary()
