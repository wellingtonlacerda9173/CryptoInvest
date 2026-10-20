from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from engine.application.risk import ManagedPosition, RiskState
from engine.domain.decision import DecisionAction
from engine.domain.journal import DecisionEntry, FillRecord, OrderRecord, RunMode, RunRecord
from engine.domain.order import OrderStatus, OrderType
from engine.domain.risk import RiskConfig
from engine.domain.trade import ExitReason, Trade

D = Decimal
SYMBOL = "BTC/USDT"
T0 = datetime(2026, 3, 1, 12, tzinfo=UTC)


def new_run(mode: RunMode = RunMode.PAPER) -> RunRecord:
    return RunRecord(
        run_id=f"t-{uuid4().hex[:16]}", mode=mode, started_at=T0, config_hash="abc123", code_version="e1b76b8"
    )


def position(**overrides: object) -> ManagedPosition:
    values: dict[str, object] = {
        "symbol": SYMBOL,
        "entry_price": D(100),
        "initial_quantity": D(2),
        "quantity": D(2),
        "initial_stop": D(95),
        "stop": D(95),
        "take_profit": D(110),
        "highest_price": D(100),
        "entry_fee": D("0.2"),
        "opened_at": T0,
    }
    return ManagedPosition.model_validate(values | overrides)


def order(
    client_order_id: str, *, side: str = "buy", status: OrderStatus = OrderStatus.NEW, at: datetime = T0
) -> OrderRecord:
    return OrderRecord(
        client_order_id=client_order_id,
        symbol=SYMBOL,
        side=side,  # type: ignore[arg-type]
        type=OrderType.LIMIT,
        quantity=D(2),
        price=D(100),
        stop_loss=D(95) if side == "buy" else None,
        take_profit=D(110) if side == "buy" else None,
        status=status,
        created_at=T0,
        updated_at=at,
    )


def fill(
    fill_id: str, client_order_id: str, *, side: str = "buy", price: str = "100", at: datetime = T0
) -> FillRecord:
    return FillRecord(
        fill_id=fill_id,
        client_order_id=client_order_id,
        symbol=SYMBOL,
        side=side,  # type: ignore[arg-type]
        quantity=D(2),
        price=D(price),
        fee=D("0.2"),
        fee_asset="USDT",
        filled_at=at,
    )


def trade(
    trade_id: str, *, exit_price: str = "90", fee: str = "0.2", closed: datetime | None = None
) -> Trade:
    closed_at = closed or T0 + timedelta(hours=3)
    return Trade(
        trade_id=trade_id,
        symbol=SYMBOL,
        quantity=D(2),
        entry_price=D(100),
        exit_price=D(exit_price),
        entry_fee=D("0.2"),
        exit_fee=D(fee),
        opened_at=T0,
        closed_at=closed_at,
        exit_reason=ExitReason.STOP_LOSS if D(exit_price) < 100 else ExitReason.TAKE_PROFIT,
    )


def decision(
    code: str = "entered", *, traded: bool = True, signal_id: str | None = None, at: datetime = T0
) -> DecisionEntry:
    return DecisionEntry(
        decided_at=at,
        symbol=SYMBOL,
        action=DecisionAction.ENTER if traded else DecisionAction.SKIP,
        traded=traded,
        code=code,
        reason="barra elefante com rompimento" if traded else "spread acima do limite",
        strategy="elephant",
        confidence=0.72,
        signal_id=signal_id,
        context={"regime": "tendencia", "spread": D("0.0004"), "at": at, "levels": [D(95), D(110)]},
    )


def busy_risk_state() -> RiskState:
    """Estado com ledger, auditoria, sequencia de perdas e pausas: exercita toda a serializacao."""
    config = RiskConfig()
    state = RiskState.initial(D("10000.123456789012345678"), T0)
    for i, price in enumerate(("90", "91")):
        state = state.record_trade(
            trade(f"loss-{i}", exit_price=price, closed=T0 + timedelta(hours=i + 1)),
            T0 + timedelta(hours=i + 1),
            config,
        )
    return state.pause("divergencia de saldo", T0 + timedelta(hours=5))
