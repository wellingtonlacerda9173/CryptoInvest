from engine.application.risk import (
    AccountSnapshot,
    DivergenceKind,
    ProtectiveStop,
    RiskState,
    pause_on_divergence,
    reconcile,
)
from engine.domain.market import Balance
from tests.unit.risk.factories import NOW, SYMBOL, D, position


def stop_of(quantity: str = "1", price: str = "95") -> ProtectiveStop:
    return ProtectiveStop(symbol=SYMBOL, quantity=D(quantity), stop_price=D(price))


def snapshot(btc: str = "1", protected: bool = True, **extra: str) -> AccountSnapshot:
    balances = {
        "BTC": Balance(asset="BTC", free=D(btc), locked=D(0)),
        "USDT": Balance(asset="USDT", free=D(500), locked=D(0)),
    }
    balances |= {a: Balance(asset=a, free=D(v), locked=D(0)) for a, v in extra.items()}
    return AccountSnapshot(
        balances=balances,
        stops=(stop_of(),) if protected else (),
        prices={"ETH": D(3000), "BTC": D(60000)},
    )


def test_consistent_state_has_no_divergence() -> None:
    assert reconcile([position()], snapshot()) == ()


def test_locked_balance_counts() -> None:
    snap = AccountSnapshot(
        balances={"BTC": Balance(asset="BTC", free=D("0.4"), locked=D("0.6"))}, stops=(stop_of(),)
    )

    assert reconcile([position()], snap) == ()


def test_missing_balance() -> None:
    [found] = reconcile([position()], snapshot(btc="0.5"))

    assert found.kind is DivergenceKind.MISSING_ON_EXCHANGE
    assert (found.subject, found.expected, found.actual) == ("BTC", D(1), D("0.5"))


def test_untracked_balance_of_unknown_asset() -> None:
    [found] = reconcile([position()], snapshot(ETH="2"))

    assert found.kind is DivergenceKind.UNTRACKED_BALANCE
    assert found.subject == "ETH"


def test_extra_balance_on_tracked_asset() -> None:
    [found] = reconcile([position()], snapshot(btc="1.5"))

    assert found.kind is DivergenceKind.UNTRACKED_BALANCE


def test_tolerance_absorbs_small_differences() -> None:
    assert reconcile([position()], snapshot(btc="0.9991")) == ()


def test_dust_below_quote_threshold_is_ignored_but_unpriced_balance_is_not() -> None:
    dust = reconcile([], snapshot(ETH="0.0001"))
    unpriced = reconcile([], snapshot(DOGE="1"))

    assert [d.subject for d in dust if d.subject == "ETH"] == []
    assert [d.subject for d in unpriced if d.subject == "DOGE"] == ["DOGE"]


def test_dust_threshold_is_configurable() -> None:
    found = reconcile([], snapshot(ETH="0.0001"), dust_quote_value=D("0.1"))

    assert [d.subject for d in found if d.subject == "ETH"] == ["ETH"]


def test_quote_assets_are_ignored() -> None:
    balances = {a: Balance(asset=a, free=D(3), locked=D(0)) for a in ("USDT", "BNB")}

    assert reconcile([], AccountSnapshot(balances=balances)) == ()


def test_unprotected_position_is_a_divergence() -> None:
    [found] = reconcile([position()], snapshot(protected=False))

    assert found.kind is DivergenceKind.UNPROTECTED_POSITION


def test_stop_must_cover_the_position_quantity() -> None:
    snap = snapshot().model_copy(update={"stops": (stop_of(quantity="0.01"),)})

    [found] = reconcile([position()], snap)

    assert found.kind is DivergenceKind.PARTIALLY_PROTECTED
    assert (found.expected, found.actual) == (D(1), D("0.01"))


def test_stops_split_across_orders_add_up() -> None:
    snap = snapshot().model_copy(update={"stops": (stop_of("0.6"), stop_of("0.4"))})

    assert reconcile([position()], snap) == ()


def test_stop_below_the_expected_level_is_a_divergence() -> None:
    pos = position().model_copy(update={"stop": D(100), "breakeven_reached": True})
    snap = snapshot().model_copy(update={"stops": (stop_of(price="95"),)})

    [found] = reconcile([pos], snap)

    assert found.kind is DivergenceKind.STOP_BELOW_EXPECTED
    assert (found.expected, found.actual) == (D(100), D(95))


def test_stop_at_or_above_expected_level_is_fine() -> None:
    snap = snapshot().model_copy(update={"stops": (stop_of(price="96"),)})

    assert reconcile([position()], snap) == ()


def test_divergence_pauses_with_reason() -> None:
    divergences = reconcile([position()], snapshot(btc="0.5"))
    state = pause_on_divergence(RiskState.initial(D(1000), NOW), divergences, NOW)

    assert state.paused
    assert state.pause_reason is not None
    assert "BTC" in state.pause_reason


def test_no_divergence_leaves_state_untouched() -> None:
    state = RiskState.initial(D(1000), NOW)

    assert pause_on_divergence(state, (), NOW) is state
