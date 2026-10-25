# ruff: noqa: E501
"""Fuzz de injecao: o alvo/stop executa em CADA ponto (requisicao) entre o fill da entrada e o proximo poll, com
saldo ALHEIO de BTC na conta. Nenhuma venda pode tocar o saldo alheio (vendido <= comprado, alheio intacto).
Somente a fake local. Exemplos: LIVE_FUZZ_EXAMPLES (padrao 150); saldo alheio: LIVE_FUZZ_ALIEN (padrao 5)."""

import asyncio
import contextlib
import os
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from engine.domain.gateway import UnknownOrderError
from engine.domain.order import ExitOrder, OrderType
from engine.infrastructure.live.errors import LiveError
from tests.fakes.fake_binance import FakeBinance, Req
from tests.unit.live.support import NOW, SYMBOL, approved_entry, build_rig

D = Decimal
ALIEN = D(os.environ.get("LIVE_FUZZ_ALIEN", "5"))
EPS = D("0.0005")
TRIGGERS = (112, 94)

ops_strategy = st.one_of(
    st.tuples(st.just("poll"), st.just(0)),
    st.tuples(st.just("exit"), st.integers(1, 12)),
    st.tuples(st.just("raise"), st.integers(95, 108)),
    st.tuples(st.just("drop"), st.just(0)),
)


def totals(fake: FakeBinance) -> tuple[Decimal, Decimal]:
    bought = sum((D(t["qty"]) for t in fake.trades if t["isBuyer"]), D(0))
    sold = sum((D(t["qty"]) for t in fake.trades if not t["isBuyer"]), D(0))
    return bought, sold


async def scenario(
    at: int, after: bool, trigger: int, ops: list[tuple[str, int]], *, base_fee: bool = False
) -> str | None:
    fake = FakeBinance()
    await fake.start()
    fake.balances["BTC"] = [ALIEN, D(0)]
    fake.commission_in_base = base_fee
    rig = build_rig(fake, request_timeout_seconds=0.12, read_retries=1, protection_activation_grace_seconds=0)
    try:
        await rig.gateway.place_order(approved_entry())
        fake.set_price("BTCUSDT", D(99))
        count = [0]

        def hook(_: Req) -> None:
            if count[0] == at:
                fake.set_price("BTCUSDT", D(trigger))
            count[0] += 1

        if after:
            fake.after_dispatch = hook
        else:
            fake.before_dispatch = hook
        for index, (name, arg) in enumerate(ops):
            with contextlib.suppress(LiveError, ValueError, UnknownOrderError):
                await run_op(rig.gateway, name, arg, fake)
            bought, sold = totals(fake)
            if sold > bought + EPS:
                return f"op #{index} {name}{arg}: vendeu {sold} de {bought} comprado (saldo alheio tocado)"
            if fake.free("BTC") + fake.locked("BTC") < ALIEN - EPS:
                return f"op #{index} {name}{arg}: saldo alheio diminuiu"
        fake.before_dispatch = fake.after_dispatch = None
        for _ in range(3):
            try:
                await rig.gateway.poll()
            except LiveError:
                continue
        bought, sold = totals(fake)
        if sold > bought + EPS or fake.free("BTC") + fake.locked("BTC") < ALIEN - EPS:
            return "saldo alheio tocado apos convergencia"
        return None
    finally:
        await rig.client.aclose()
        await fake.stop()


async def run_op(gateway: Any, name: str, arg: int, fake: FakeBinance) -> None:
    match name:
        case "poll":
            await gateway.poll()
        case "exit":
            await gateway.place_order(
                ExitOrder(
                    client_order_id=f"x-{len(fake.log)}",
                    symbol=SYMBOL,
                    type=OrderType.MARKET,
                    quantity=D(arg),
                    created_at=NOW,
                )
            )
        case "raise":
            for entry_id in gateway.open_entry_ids():
                await gateway.raise_stop(entry_id, D(arg))
        case "drop":
            for entry_id in gateway.open_entry_ids():
                await gateway.drop_target(entry_id)


@pytest.mark.parametrize("trigger", TRIGGERS)
@pytest.mark.parametrize("after", [False, True])
@pytest.mark.parametrize("at", range(0, 40))
async def test_target_or_stop_at_every_request_between_fill_and_poll_never_sells_foreign_balance(
    at: int, after: bool, trigger: int
) -> None:
    problem = await scenario(at, after, trigger, [("exit", 5), ("poll", 0), ("exit", 5), ("poll", 0)])

    assert problem is None, problem


@settings(
    max_examples=int(os.environ.get("LIVE_FUZZ_EXAMPLES", "150")),
    deadline=None,
    suppress_health_check=list(HealthCheck),
)
@given(
    at=st.integers(0, 60),
    after=st.booleans(),
    trigger=st.sampled_from(TRIGGERS),
    base_fee=st.booleans(),
    ops=st.lists(ops_strategy, min_size=1, max_size=8),
)
def test_random_sequences_with_injected_execution_never_sell_foreign_balance(
    at: int, after: bool, trigger: int, base_fee: bool, ops: list[tuple[str, int]]
) -> None:
    problem = asyncio.run(scenario(at, after, trigger, ops, base_fee=base_fee))

    assert problem is None, problem
