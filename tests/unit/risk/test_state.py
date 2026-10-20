from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule
from pydantic import ValidationError

from engine.application.risk import AuditAction, RejectionCode, RiskState
from engine.domain.risk import RiskConfig
from engine.domain.trade import Trade
from tests.unit.risk.factories import NOW, SYMBOL, D, trade

CFG = RiskConfig()


def fresh(equity: int = 10_000) -> RiskState:
    return RiskState.initial(D(equity), NOW)


def code(state: RiskState, symbol: str = SYMBOL, now: datetime = NOW) -> RejectionCode | None:
    block = state.entry_block(symbol, now)
    return None if block is None else block.code


def test_clean_state_allows_entries() -> None:
    assert code(fresh()) is None


def test_daily_loss_limit_pauses_until_next_utc_midnight() -> None:
    late = datetime(2026, 3, 10, 23, 59, 59, tzinfo=UTC)
    state = fresh().record_trade(trade("-100", at=late), late, CFG)

    assert code(state, now=late) is RejectionCode.DAILY_LOSS_PAUSE
    assert (
        code(state, now=datetime(2026, 3, 10, 23, 59, 59, 999999, tzinfo=UTC))
        is RejectionCode.DAILY_LOSS_PAUSE
    )
    assert code(state, now=datetime(2026, 3, 11, tzinfo=UTC)) is None


def test_daily_loss_just_below_limit_does_not_pause() -> None:
    state = fresh().record_trade(trade("-99.99"), NOW, CFG)

    assert code(state) is None


def test_daily_loss_accumulates_across_trades_and_symbols() -> None:
    state = (
        fresh().record_trade(trade("-60"), NOW, CFG).record_trade(trade("-40", symbol="ETH/USDT"), NOW, CFG)
    )

    assert code(state, symbol="SOL/USDT") is RejectionCode.DAILY_LOSS_PAUSE


def test_utc_day_rollover_resets_daily_figures_and_base() -> None:
    state = fresh().record_trade(trade("-50"), NOW, CFG)
    tomorrow = NOW + timedelta(days=1)

    rolled = state.roll_day(tomorrow)

    assert rolled.day_pnl == 0
    assert rolled.day_start_equity == D(9_950)
    assert rolled.equity == D(9_950)


def test_non_utc_timezone_is_normalised_to_utc_day() -> None:
    from zoneinfo import ZoneInfo

    local = datetime(2026, 3, 10, 22, 30, tzinfo=ZoneInfo("America/Sao_Paulo"))

    assert RiskState.initial(D(1), local).day == datetime(2026, 3, 11, tzinfo=UTC).date()


def test_daily_target_stops_new_entries_for_the_day_only() -> None:
    state = fresh().record_trade(trade("100"), NOW, CFG)

    assert code(state) is RejectionCode.DAILY_TARGET_REACHED
    assert code(state, now=NOW + timedelta(days=1)) is None
    assert any(entry.action is AuditAction.DAILY_TARGET_REACHED for entry in state.audit)


def test_daily_target_is_configurable_and_just_below_does_not_stop() -> None:
    cfg = RiskConfig(daily_profit_target=D("0.02"))

    assert code(fresh().record_trade(trade("199.99"), NOW, cfg)) is None
    assert code(fresh().record_trade(trade("200"), NOW, cfg)) is RejectionCode.DAILY_TARGET_REACHED


def test_two_consecutive_losses_pause_the_pair_for_24h_exactly() -> None:
    state = fresh().record_trade(trade("-10"), NOW, CFG)
    assert code(state) is None

    state = state.record_trade(trade("-10"), NOW, CFG)

    assert code(state) is RejectionCode.PAIR_PAUSED
    assert code(state, symbol="ETH/USDT") is None
    assert code(state, now=NOW + timedelta(hours=24) - timedelta(microseconds=1)) is RejectionCode.PAIR_PAUSED
    assert code(state, now=NOW + timedelta(hours=24)) is None


def test_win_resets_the_loss_streak_but_tie_does_not() -> None:
    after_win = fresh().record_trade(trade("-10"), NOW, CFG).record_trade(trade("5"), NOW, CFG)
    after_tie = fresh().record_trade(trade("-10"), NOW, CFG).record_trade(trade("0"), NOW, CFG)

    assert code(after_win.record_trade(trade("-10"), NOW, CFG)) is None
    assert code(after_tie.record_trade(trade("-10"), NOW, CFG)) is RejectionCode.PAIR_PAUSED


def test_drawdown_of_exactly_five_percent_halts_and_stays_halted() -> None:
    cfg = RiskConfig(max_daily_loss=D("0.05"))
    state = fresh().record_trade(trade("-499.99"), NOW, cfg)
    assert not state.halted

    state = state.record_trade(trade("-0.01"), NOW, cfg)

    assert state.halted
    assert code(state) is RejectionCode.ROBOT_HALTED
    recovered = state.observe_equity(D(20_000), NOW + timedelta(days=3), cfg)
    assert recovered.halted
    assert code(recovered, now=NOW + timedelta(days=3)) is RejectionCode.ROBOT_HALTED


def test_drawdown_is_measured_from_the_peak() -> None:
    state = fresh().observe_equity(D(12_000), NOW, CFG)

    assert state.peak_equity == D(12_000)
    assert not state.observe_equity(D(11_401), NOW, CFG).halted
    assert state.observe_equity(D(11_400), NOW, CFG).halted


def test_only_manual_reactivation_unhalts_and_is_audited() -> None:
    cfg = RiskConfig(max_daily_loss=D("0.05"))
    halted = fresh().record_trade(trade("-500"), NOW, cfg)

    with pytest.raises(ValueError, match="operator"):
        halted.reactivate(operator=" ", reason="ok", now=NOW)
    back = halted.reactivate(operator="wellington", reason="revisado", now=NOW)

    assert not back.halted
    assert back.peak_equity == back.equity == D(9_500)
    last = back.audit[-1]
    assert (last.action, last.operator, last.detail) == (AuditAction.REACTIVATED, "wellington", "revisado")
    with pytest.raises(ValueError, match="not halted"):
        back.reactivate(operator="x", reason="y", now=NOW)


def test_pause_blocks_until_explicit_resume() -> None:
    paused = fresh().pause("divergencia", NOW)

    assert code(paused, now=NOW + timedelta(days=9)) is RejectionCode.PAUSED
    resumed = paused.resume(operator="op", reason="conferido", now=NOW)
    assert code(resumed) is None
    assert [e.action for e in resumed.audit] == [AuditAction.PAUSED, AuditAction.RESUMED]


def test_ledger_is_append_only_and_trades_immutable() -> None:
    first, second = trade("-10"), trade("5")
    state = fresh().record_trade(first, NOW, CFG).record_trade(second, NOW, CFG)

    assert state.ledger == (first, second)
    with pytest.raises(ValidationError):
        first.exit_price = D(1)  # type: ignore[misc]
    with pytest.raises(ValidationError):
        state.ledger = ()  # type: ignore[misc]


def test_state_roundtrips_through_json() -> None:
    state = fresh().record_trade(trade("-10"), NOW, CFG).record_trade(trade("-10"), NOW, CFG)
    state = state.pause("x", NOW)

    assert RiskState.model_validate_json(state.model_dump_json()) == state


class RiskMachine(RuleBasedStateMachine):
    """Sequencias aleatorias de eventos: as travas nunca se soltam sozinhas e o historico so cresce."""

    @initialize()
    def start(self) -> None:
        self.state = fresh()
        self.now = NOW
        self.previous: RiskState = self.state
        self.reactivated = False

    def _advance(self, new: RiskState) -> None:
        self.previous, self.state = self.state, new

    @rule(pnl=st.integers(-600, 600), symbol=st.sampled_from([SYMBOL, "ETH/USDT"]))
    def close_trade(self, pnl: int, symbol: str) -> None:
        self._advance(self.state.record_trade(trade(str(pnl), symbol=symbol, at=self.now), self.now, CFG))

    @rule(minutes=st.integers(0, 3000))
    def tick(self, minutes: int) -> None:
        self.now += timedelta(minutes=minutes)
        self._advance(self.state.roll_day(self.now))

    @rule(equity=st.integers(1_000, 30_000))
    def observe(self, equity: int) -> None:
        self._advance(self.state.observe_equity(D(equity), self.now, CFG))

    @rule()
    def pause(self) -> None:
        self._advance(self.state.pause("div", self.now))

    @precondition(lambda self: self.state.halted and self.state.equity > 0)
    @rule()
    def reactivate(self) -> None:
        self._advance(self.state.reactivate(operator="op", reason="ok", now=self.now))
        self.reactivated = True

    @invariant()
    def ledger_and_audit_only_grow(self) -> None:
        assert self.state.ledger[: len(self.previous.ledger)] == self.previous.ledger
        assert self.state.audit[: len(self.previous.audit)] == self.previous.audit

    @invariant()
    def halt_only_cleared_by_reactivation(self) -> None:
        if self.previous.halted and not self.state.halted:
            assert self.state.audit[-1].action is AuditAction.REACTIVATED
        if self.state.halted:
            assert code(self.state, now=self.now) is RejectionCode.ROBOT_HALTED

    @invariant()
    def peak_dominates_equity(self) -> None:
        assert self.state.peak_equity >= self.state.equity
        if not self.state.halted:
            assert self.state.drawdown < CFG.max_drawdown

    @invariant()
    def streaks_stay_below_limit(self) -> None:
        assert all(n < CFG.consecutive_loss_limit for n in self.state.loss_streaks.values())

    @invariant()
    def daily_loss_blocks_same_day(self) -> None:
        state = self.state.roll_day(self.now)
        if state.day_pnl <= -CFG.max_daily_loss * state.day_start_equity:
            assert state.entry_block("SOL/USDT", self.now) is not None

    @invariant()
    def serializable(self) -> None:
        assert RiskState.model_validate_json(self.state.model_dump_json()) == self.state

    @precondition(lambda self: self.state.halted and self.state.equity <= 0)
    @rule()
    def reactivate_without_capital_is_refused(self) -> None:
        with pytest.raises(ValueError, match="zero equity"):
            self.state.reactivate(operator="op", reason="ok", now=self.now)

    @invariant()
    def zero_equity_always_halts(self) -> None:
        if self.state.equity <= 0:
            assert self.state.halted


TestRiskMachine = RiskMachine.TestCase
TestRiskMachine.settings = settings(max_examples=60, stateful_step_count=30, deadline=None)


def test_trade_fixture_pnl_matches_expectation() -> None:
    assert isinstance(trade("-10"), Trade)
    assert trade("-10").pnl == D(-10)


def test_loss_larger_than_equity_zeroes_the_account_and_halts_instead_of_crashing() -> None:
    state = fresh(500).record_trade(trade("-600"), NOW, CFG)

    assert state.equity == 0 and state.halted and state.halt_reason == "capital zerado"
    assert code(state, now=NOW) is RejectionCode.ROBOT_HALTED


def test_reactivate_is_refused_while_the_account_has_no_capital() -> None:
    state = fresh(500).record_trade(trade("-600"), NOW, CFG)

    with pytest.raises(ValueError, match="zero equity"):
        state.reactivate(operator="op", reason="ok", now=NOW)
