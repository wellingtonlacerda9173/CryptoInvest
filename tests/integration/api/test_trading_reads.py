"""Leituras de trading com Postgres/Timescale reais: status, saldo, equity, posicoes, operacoes, decisoes,
desempenho, trava do Live e velas. Leitor e admin leem; anonimo nunca; nenhuma resposta traz segredo."""

import csv
import io
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import URL

from tests.integration.api.conftest import Harness, Session
from tests.integration.api.runner_support import (
    OPEN_RUN_ID,
    RUN_ID,
    fake_link,
    module_db_url,
    module_sessions,
    rh,
    rh_admin,
    rh_reader,
    seed_once,
    status_payload,
)
from tests.redis_fakes import FakeLink

pytestmark = pytest.mark.integration

FORBIDDEN = (
    "password",
    "passwd",
    "secret",
    "api_key",
    "token_hash",
    "totp",
    "recovery",
    "hmac",
    "authorization",
)


@pytest.fixture(scope="module")
def seeded(module_db_url: URL) -> dict[str, Any]:
    return seed_once(module_db_url)()


def keys_of(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in keys_of(v)}
    if isinstance(value, list):
        return {k for v in value for k in keys_of(v)}
    return set()


def alive(rh: Harness, link: FakeLink, **overrides: Any) -> None:
    link.latest["heartbeat"] = status_payload(now=rh.clock(), **overrides)


# --------------------------------------------------------------------------- acesso


async def test_anonymous_gets_401_on_every_read(rh: Harness, seeded: dict[str, Any]) -> None:
    client = rh.client()
    for path in ("/status", "/risk/limits", "/account/summary", "/equity", "/positions", "/trades", "/decisions",
                 "/performance", "/live-gate", "/glossary", "/trades/export"):  # fmt: skip
        assert (await client.get(path)).status_code == 401, path
    assert (await client.get("/market/candles", params={"symbol": "BTC/USDT"})).status_code == 401
    await client.aclose()


async def test_reader_and_admin_read_the_same_data(
    rh: Harness, rh_admin: Session, rh_reader: Session, fake_link: FakeLink, seeded: dict[str, Any]
) -> None:
    alive(rh, fake_link)
    for path in (
        "/status",
        "/account/summary",
        "/positions",
        "/trades",
        "/decisions",
        "/performance",
        "/live-gate",
    ):
        a, r = await rh_admin.get(path), await rh_reader.get(path)
        assert a.status_code == r.status_code == 200, path


# --------------------------------------------------------------------------- status


async def test_status_with_a_live_runner(
    rh: Harness, rh_reader: Session, fake_link: FakeLink, seeded: dict[str, Any]
) -> None:
    alive(rh, fake_link, pause_reasons=[], paused=False)
    body = (await rh_reader.get("/status")).json()
    assert body["run_id"] == RUN_ID and body["mode"] == "paper" and body["simulated"] is True
    assert body["state"] == "RUNNING" and body["state_label"] == "Operando" and body["connection"] == "live"
    assert body["heartbeat_age_seconds"] < 5 and body["open_positions"] == ["BTC/USDT"]
    assert body["equity"] == pytest.approx(10184.32) and body["day_pnl"] == pytest.approx(-30.56)
    assert body["day_pnl_pct"] == pytest.approx(-0.2992, abs=1e-3) and body["drawdown_pct"] == pytest.approx(
        1.13
    )
    assert body["stops_blind"] is False and body["outbox_pending"] == 0 and body["pending_commands"] == 0
    limits = {limit["key"]: limit for limit in body["limits"]}
    assert (
        limits["daily_loss"]["value"] == pytest.approx(0.2992, abs=1e-3)
        and limits["daily_loss"]["limit"] == 1.0
    )
    assert limits["daily_loss"]["summary"].startswith("Perda diária: 0,3% de 1,0%")
    assert limits["loss_streak"]["name"] == "Perdas seguidas (ETH/USDT)"
    assert limits["total_exposure"]["value"] == pytest.approx(24.0)
    assert (
        body["live"]["live_available"] is False
        and body["live"]["eligible"] is True
        and body["live"]["approved"] is False
    )
    assert (await rh_reader.get("/risk/limits")).json() == body["limits"]


async def test_status_describes_every_state_in_plain_words_without_leaking_errors(
    rh: Harness, rh_reader: Session, fake_link: FakeLink, seeded: dict[str, Any]
) -> None:
    cases = {
        "PAUSED": {"paused": True, "pause_reasons": [{"reason": "manutencao", "operator": "ana", "at": rh.clock().isoformat()}]},
        "HALTED": {"halted": True, "halt_reason": "drawdown 0.05"},
        "FAILED_PERSISTENCE": {"persistence_error": "asyncpg.exceptions.ConnectionDoesNotExistError: host=10.0.0.5", "outbox_pending": 12},
        "STARTING": {},
    }  # fmt: skip
    for state, extra in cases.items():
        alive(rh, fake_link, state=state, **extra)
        body = (await rh_reader.get("/status")).json()
        assert body["state"] == state and body["state_label"] and body["state_detail"]
        raw = json.dumps(body)
        assert "asyncpg" not in raw and "10.0.0.5" not in raw
    assert body["state"] == "STARTING"
    alive(rh, fake_link, state="PAUSED", **cases["PAUSED"])
    body = (await rh_reader.get("/status")).json()
    assert (
        body["pause_reasons"][0]["reason"] == "manutencao" and body["pause_reasons"][0]["operator"] == "ana"
    )
    alive(rh, fake_link, state="FAILED_PERSISTENCE", **cases["FAILED_PERSISTENCE"])
    body = (await rh_reader.get("/status")).json()
    assert body["outbox_pending"] == 12 and "banco de dados" in body["persistence_error"]
    alive(rh, fake_link, stops_blind=True, state="PAUSED")
    assert (await rh_reader.get("/status")).json()["stops_blind"] is True


async def test_status_without_signal_is_offline_stale_or_unknown_never_presented_as_current(
    rh: Harness, rh_reader: Session, fake_link: FakeLink, seeded: dict[str, Any]
) -> None:
    body = (await rh_reader.get("/status")).json()
    assert body["connection"] == "offline" and body["state"] == "OFFLINE" and body["heartbeat_at"] is None
    assert body["equity"] == pytest.approx(10184.32) and body["state_label"] == "Sem sinal do robô"

    old = (rh.clock() - timedelta(minutes=5)).isoformat()
    alive(rh, fake_link, at=old, heartbeat_at=old)
    body = (await rh_reader.get("/status")).json()
    assert (
        body["connection"] == "stale"
        and body["state"] == "RUNNING"
        and body["state_label"] == "Sem sinal do robô"
    )
    assert body["heartbeat_age_seconds"] > 200

    fake_link.latest.clear()
    fake_link.reachable = False
    assert (await rh_reader.get("/status")).json()["connection"] == "unknown"


async def test_the_most_recent_of_state_and_heartbeat_wins(
    rh: Harness, rh_reader: Session, fake_link: FakeLink, seeded: dict[str, Any]
) -> None:
    old = (rh.clock() - timedelta(seconds=40)).isoformat()
    fake_link.latest["heartbeat"] = status_payload(now=rh.clock(), at=old, heartbeat_at=old, state="RUNNING")
    fake_link.latest["state"] = status_payload(now=rh.clock(), state="PAUSED", paused=True)
    assert (await rh_reader.get("/status")).json()["state"] == "PAUSED"


# --------------------------------------------------------------------------- saldo, equity, posicoes


async def test_account_summary_has_day_week_and_total_result(
    rh: Harness, rh_reader: Session, fake_link: FakeLink, seeded: dict[str, Any]
) -> None:
    alive(rh, fake_link, equity="10300.00", realized_equity="10300.00")
    body = (await rh_reader.get("/account/summary")).json()
    assert body["simulated"] is True and body["currency"] == "USDT" and body["initial_capital"] == 10000
    assert (
        body["equity"] == 10300 and body["total_pnl"] == 300 and body["total_pnl_pct"] == pytest.approx(3.0)
    )
    assert body["week_pnl"] is not None and body["day_pnl"] is not None and body["as_of"]


async def test_equity_series_is_reduced_ordered_and_windowed(
    rh: Harness, rh_reader: Session, seeded: dict[str, Any]
) -> None:
    body = (await rh_reader.get("/equity", params={"days": 30, "points": 50})).json()
    points = body["points"]
    assert body["initial_capital"] == 10000 and 20 <= len(points) <= 60
    assert [p["at"] for p in points] == sorted(p["at"] for p in points)
    short = (await rh_reader.get("/equity", params={"days": 2, "points": 500})).json()["points"]
    assert 0 < len(short) < len(points) * 5
    assert (await rh_reader.get("/equity", params={"points": 5})).status_code == 422


async def test_open_positions_show_stop_target_and_protection(
    rh: Harness, rh_reader: Session, fake_link: FakeLink, seeded: dict[str, Any]
) -> None:
    alive(rh, fake_link)
    [position] = (await rh_reader.get("/positions")).json()
    assert position["symbol"] == "BTC/USDT" and position["stop"] == 97 and position["take_profit"] == 110
    assert (
        position["risk_amount"] == 5
        and position["protected"] is True
        and position["protection_note"] == "Stop ativo"
    )
    assert position["last_price"] is not None and position["unrealized_r"] is not None
    alive(rh, fake_link, stops_blind=True)
    [blind] = (await rh_reader.get("/positions")).json()
    assert blind["protected"] is False and "Sem dados de mercado" in blind["protection_note"]
    fake_link.latest.clear()
    [dead] = (await rh_reader.get("/positions")).json()
    assert dead["protected"] is False and "sem sinal de vida" in dead["protection_note"]


# --------------------------------------------------------------------------- operacoes


async def test_trades_are_paginated_by_cursor_without_gaps_or_repeats(
    rh: Harness, rh_reader: Session, seeded: dict[str, Any]
) -> None:
    seen: list[int] = []
    cursor = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 4, **({"cursor": cursor} if cursor else {})}
        page = (await rh_reader.get("/trades", params=params)).json()
        seen += [t["id"] for t in page["items"]]
        pages += 1
        assert page["summary"] == {
            "count": 9,
            "wins": 5,
            "losses": 3,
            "pnl": pytest.approx(209.5),
            "fees": pytest.approx(1.8),
        }
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert pages == 3 and len(seen) == len(set(seen)) == 9 and seen == sorted(seen, reverse=True)


async def test_trade_filters_search_and_mode_separation(
    rh: Harness, rh_reader: Session, seeded: dict[str, Any]
) -> None:
    async def fetch(**params: Any) -> dict[str, Any]:
        response = await rh_reader.get("/trades", params={"limit": 50, **params})
        assert response.status_code == 200, response.text
        return response.json()  # type: ignore[no-any-return]

    assert {t["symbol"] for t in (await fetch(symbol="ETH/USDT"))["items"]} == {"ETH/USDT"}
    assert (await fetch(symbol="ETH/USDT"))["summary"]["count"] == 4
    losses = await fetch(result="loss")
    assert losses["summary"]["count"] == 3 and all(t["pnl"] < 0 for t in losses["items"])
    assert (await fetch(result="breakeven"))["summary"]["count"] == 1
    assert (await fetch(strategy="ema"))["summary"]["count"] == 3
    assert (await fetch(exit_reason="stop_loss"))["summary"]["count"] == 3
    assert (await fetch(q="protection"))["summary"]["count"] == 1
    assert (await fetch(q="take"))["summary"]["count"] == 4
    assert (await fetch(q="%"))["summary"]["count"] == 0
    now = datetime.now(UTC)
    recent = await fetch(since=(now - timedelta(days=2.5)).isoformat())
    assert recent["summary"]["count"] == 3
    window = await fetch(
        since=(now - timedelta(days=6.5)).isoformat(), until=(now - timedelta(days=1.5)).isoformat()
    )
    assert window["summary"]["count"] == 4
    live = await fetch(mode="live")
    assert live["items"] == [] and live["summary"]["count"] == 0
    first = (await fetch())["items"][0]
    assert (
        first["mode"] == "paper"
        and first["exit_reason_text"]
        and first["result"] in {"win", "loss", "breakeven"}
    )
    for bad in (
        {"exit_reason": "explodiu"},
        {"result": "talvez"},
        {"symbol": "btc"},
        {"limit": 0},
        {"mode": "backtest"},
    ):
        assert (await rh_reader.get("/trades", params=bad)).status_code == 422


async def test_csv_export_is_labelled_by_mode_and_neutralises_formulas(
    rh: Harness, rh_reader: Session, seeded: dict[str, Any]
) -> None:
    response = await rh_reader.get("/trades/export", params={"mode": "paper"})
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/csv")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="operacoes-paper-') and disposition.endswith('.csv"')
    text = response.content.decode("utf-8-sig")
    lines = text.splitlines()
    assert lines[0] == "# CryptoInvest - operações encerradas - modo PAPER (simulado)"
    rows = list(csv.reader(io.StringIO("\n".join(lines[2:]))))
    assert rows[0][0] == "modo" and len(rows) == 10 and {r[0] for r in rows[1:]} == {"PAPER"}
    strategies = {r[4] for r in rows[1:]}
    assert "'=1+1" in strategies and "=1+1" not in strategies
    filtered = (await rh_reader.get("/trades/export", params={"result": "loss"})).content.decode("utf-8-sig")
    assert len([ln for ln in filtered.splitlines() if ln.startswith("PAPER,")]) == 3
    assert "modo LIVE" in (await rh_reader.get("/trades/export", params={"mode": "live"})).content.decode(
        "utf-8-sig"
    )


# --------------------------------------------------------------------------- decisoes


async def test_decisions_feed_translates_the_reason_and_filters(
    rh: Harness, rh_reader: Session, seeded: dict[str, Any]
) -> None:
    page = (await rh_reader.get("/decisions", params={"limit": 3})).json()
    assert page["next_cursor"] and [d["id"] for d in page["items"]] == sorted(
        (d["id"] for d in page["items"]), reverse=True
    )
    summary = page["summary"]
    assert (
        summary["entries"] == 1
        and summary["refusals"] == 3
        and summary["waits"] == 3
        and summary["signals"] == 4
    )
    nxt = (await rh_reader.get("/decisions", params={"limit": 50, "cursor": page["next_cursor"]})).json()
    assert nxt["summary"] is None and len(page["items"]) + len(nxt["items"]) == 7

    everything = {
        d["code"]: d for d in (await rh_reader.get("/decisions", params={"limit": 50})).json()["items"]
    }
    entered = everything["approved"]
    assert (entered["outcome"], entered["outcome_kind"], entered["traded"]) == ("Entrou", "entered", True)
    refused = everything["event_window"]
    assert refused["outcome"] == "Não entrou" and refused["outcome_kind"] == "refused"
    assert refused["reason_text"] == "Perto de uma notícia importante do mercado." and refused[
        "detail"
    ].startswith("event_window")
    assert refused["signal"]["entry"] == "100" and refused["strategy"] == "ema"
    assert (
        everything["no_signal"]["outcome"] == "Esperou"
        and "Ficar parado em USDT" in everything["no_signal"]["reason_text"]
    )
    assert everything["reward_risk"]["reason_text"].endswith("(mínimo 1 para 2).")

    only_traded = (await rh_reader.get("/decisions", params={"traded": True})).json()["items"]
    assert [d["code"] for d in only_traded] == ["approved"]
    not_traded = (await rh_reader.get("/decisions", params={"traded": False, "limit": 50})).json()["items"]
    assert len(not_traded) == 6 and all(not d["traded"] for d in not_traded)
    by_code = (await rh_reader.get("/decisions", params={"code": "spread_too_wide"})).json()["items"]
    assert [d["code"] for d in by_code] == ["spread_too_wide"]
    eth = (await rh_reader.get("/decisions", params={"symbol": "ETH/USDT", "limit": 50})).json()["items"]
    assert {d["symbol"] for d in eth} == {"ETH/USDT"}
    assert (await rh_reader.get("/decisions", params={"code": "DROP TABLE"})).status_code == 422


# --------------------------------------------------------------------------- desempenho


async def test_performance_metrics_and_breakdowns(
    rh: Harness, rh_reader: Session, fake_link: FakeLink, seeded: dict[str, Any]
) -> None:
    body = (await rh_reader.get("/performance")).json()
    assert body["run_id"] == RUN_ID and body["mode"] == "paper" and body["simulated"] is True
    assert (body["trades"], body["wins"], body["losses"]) == (9, 5, 3)
    assert body["win_rate"] == pytest.approx(5 / 9, abs=1e-3)
    assert body["profit_factor"] == pytest.approx(359.5 / 150, abs=1e-3)
    assert body["payoff"] == pytest.approx((359.5 / 5) / 50, abs=1e-3)
    assert body["expectancy_r"] == pytest.approx(3.8 / 9, abs=1e-3)
    assert body["total_pnl"] == pytest.approx(209.5) and body["total_return_pct"] == pytest.approx(2.095)
    assert body["max_drawdown_pct"] is not None and body["sharpe"] is not None
    assert 0 <= body["idle_pct"] <= 100
    strategies = {g["key"]: g for g in body["by_strategy"]}
    assert (
        strategies["elephant"]["trades"] == 5
        and strategies["ema"]["trades"] == 3
        and strategies["=1+1"]["trades"] == 1
    )
    assert strategies["ema"]["win_rate"] == pytest.approx(1 / 3, abs=1e-3)
    pairs = {g["key"]: g for g in body["by_pair"]}
    assert pairs["BTC/USDT"]["trades"] == 5 and pairs["ETH/USDT"]["trades"] == 4
    assert body["months"] >= 1 and sum(g["trades"] for g in body["by_month"]) == 9
    assert body["signals_refused"] == 3 and {a["code"] for a in body["avoided"]} == {
        "event_window",
        "reward_risk",
        "spread_too_wide",
    }
    assert "poucas para concluir" in body["sample_warning"] and any("USDT" in n for n in body["notes"])
    recent = (await rh_reader.get("/performance", params={"days": 3})).json()
    assert 0 < recent["trades"] < 9


async def test_performance_for_a_run_without_trades_is_an_honest_empty_state(
    rh: Harness, rh_reader: Session, seeded: dict[str, Any]
) -> None:
    body = (await rh_reader.get("/performance", params={"run_id": OPEN_RUN_ID})).json()
    assert body["trades"] == 0 and body["win_rate"] is None and body["profit_factor"] is None
    assert any("calendário" in n for n in body["notes"])


# --------------------------------------------------------------------------- trava do Live


async def test_live_gate_shows_value_against_target_and_never_allows_activation(
    rh: Harness, rh_reader: Session, seeded: dict[str, Any]
) -> None:
    gate = (await rh_reader.get("/live-gate")).json()
    assert gate["run_id"] == RUN_ID and gate["approved"] is False and gate["eligible_for_live"] is True
    assert gate["live_available"] is False and gate["unavailable_reason"] == "Live ainda não disponível"
    assert (
        gate["phrase_required"] == "OPERAR COM DINHEIRO REAL"
        and gate["first_activation_reduced_risk_days"] == 7
    )
    rows = {c["code"]: c for c in gate["criteria"]}
    assert list(rows) == [
        "operating_days",
        "max_drawdown",
        "profit_factor",
        "min_trades",
        "min_losses",
        "daily_loss",
        "eligible_for_live",
    ]
    assert rows["operating_days"]["status"] == "in_progress" and rows["operating_days"]["target"] == 60
    assert rows["min_trades"]["value"] == 9 and rows["min_trades"]["missing_text"] == "Faltam 21"
    assert rows["min_losses"]["value"] == 3 and rows["min_losses"]["status"] == "in_progress"
    assert rows["eligible_for_live"]["status"] == "met" and gate["verdict"].startswith("Bloqueado")


async def test_a_run_that_used_the_open_calendar_is_forever_ineligible(
    rh: Harness, rh_reader: Session, seeded: dict[str, Any]
) -> None:
    gate = (await rh_reader.get("/live-gate", params={"run_id": OPEN_RUN_ID})).json()
    assert gate["eligible_for_live"] is False and gate["approved"] is False
    assert "nunca vai poder liberar dinheiro real" in gate["ineligible_reason"]
    assert gate["verdict"].startswith("Inelegível")
    rows = {c["code"]: c for c in gate["criteria"]}
    assert (
        rows["eligible_for_live"]["status"] == "failed"
        and rows["eligible_for_live"]["value_text"] == "Inelegível"
    )


# --------------------------------------------------------------------------- mercado


async def test_candles_come_with_elephant_entry_exit_and_level_markers(
    rh: Harness, rh_reader: Session, seeded: dict[str, Any]
) -> None:
    body = (
        await rh_reader.get("/market/candles", params={"symbol": "BTC/USDT", "timeframe": "1h", "limit": 100})
    ).json()
    assert len(body["candles"]) == 100 and body["simulated"] is True and body["symbol"] == "BTC/USDT"
    times = [c["t"] for c in body["candles"]]
    assert times == sorted(times) and set(body["candles"][0]) == {"t", "o", "h", "l", "c", "v"}
    kinds = {m["kind"] for m in body["markers"]}
    assert "elephant" in kinds and {"entry", "target"} <= kinds
    assert {lv["kind"]: lv["price"] for lv in body["levels"]} == {"stop": 97, "entry": 100, "target": 110}
    assert (
        body["last_price"] == body["candles"][-1]["c"]
        and body["change_24h_pct"] is not None
        and body["volume_24h"] > 0
    )
    assert (body["last_decision"] and "Não entrou" in body["last_decision"]) or "Esperou" in body[
        "last_decision"
    ]


async def test_candles_for_a_pair_without_history_are_empty_and_bad_input_is_rejected(
    rh: Harness, rh_reader: Session, seeded: dict[str, Any]
) -> None:
    empty = (await rh_reader.get("/market/candles", params={"symbol": "SOL/USDT"})).json()
    assert empty["candles"] == [] and empty["markers"] == [] and empty["last_price"] is None
    for bad in (
        {"symbol": "btc"},
        {"symbol": "BTC/USDT", "timeframe": "2h"},
        {"symbol": "BTC/USDT", "limit": 5},
        {},
    ):
        assert (await rh_reader.get("/market/candles", params=bad)).status_code == 422


# --------------------------------------------------------------------------- glossario e segredos


async def test_glossary_has_terms_states_and_every_decision_reason(
    rh: Harness, rh_reader: Session, seeded: dict[str, Any]
) -> None:
    body = (await rh_reader.get("/glossary")).json()
    assert {t["term"] for t in body["terms"]} >= {
        "Paper",
        "Drawdown",
        "Stop",
        "Profit factor",
        "Barra elefante",
    }
    assert {s["state"] for s in body["states"]} >= {"RUNNING", "HALTED", "FAILED_PERSISTENCE"}
    assert {r["code"] for r in body["reasons"]} >= {
        "event_window",
        "reward_risk",
        "daily_loss_pause",
        "approved",
    }


async def test_no_read_response_carries_secrets(
    rh: Harness, rh_admin: Session, fake_link: FakeLink, seeded: dict[str, Any]
) -> None:
    alive(rh, fake_link)
    paths = ["/status", "/risk/limits", "/account/summary", "/equity", "/positions", "/trades", "/decisions",
             "/performance", "/live-gate", "/glossary", "/control/commands", "/settings", "/backtests", "/onboarding"]  # fmt: skip
    for path in paths:
        response = await rh_admin.get(path)
        assert response.status_code == 200, (path, response.text)
        names = {k.lower() for k in keys_of(response.json())}
        assert not [k for k in names if any(word in k for word in FORBIDDEN)], (path, names)
        assert COMMAND_KEY_WORD not in response.text
    candles = await rh_admin.get("/market/candles", params={"symbol": "BTC/USDT"})
    assert not [k for k in keys_of(candles.json()) if any(word in k.lower() for word in FORBIDDEN)]


COMMAND_KEY_WORD = "kkkkkkkkkkkkkkkk"


async def test_a_published_equity_of_zero_is_a_real_balance_not_a_missing_one(
    rh: Harness, rh_reader: Session, fake_link: FakeLink, seeded: dict[str, Any]
) -> None:
    for zero in (0.0, "0.0", 0):
        alive(rh, fake_link, equity=zero, realized_equity=zero)
        body = (await rh_reader.get("/account/summary")).json()
        assert body["equity"] == 0 and body["realized_equity"] == 0, zero
        assert body["total_pnl"] == -10000


async def test_a_heartbeat_from_the_future_is_suspect_and_never_counts_as_age_zero(
    rh: Harness, rh_reader: Session, fake_link: FakeLink, seeded: dict[str, Any]
) -> None:
    from api.services.runner_status import RunnerStatusService

    future = (rh.clock() + timedelta(minutes=10)).isoformat()
    alive(rh, fake_link, at=future, heartbeat_at=future)
    snapshot = await RunnerStatusService(rh.container).snapshot()
    assert snapshot.suspect is True and snapshot.alive is False and snapshot.data is None
    body = (await rh_reader.get("/status")).json()
    assert body["connection"] != "live"

    near = (rh.clock() + timedelta(seconds=5)).isoformat()
    alive(rh, fake_link, at=near, heartbeat_at=near)
    assert (await RunnerStatusService(rh.container).snapshot()).alive is True


async def test_pair_pause_reason_uses_the_configured_hours(
    rh: Harness, rh_reader: Session, fake_link: FakeLink, seeded: dict[str, Any]
) -> None:
    from api.reasons import translate

    assert "24" not in translate("pair_paused").text
    assert "48 horas" in translate("pair_paused", pair_pause_hours=48).text
    alive(rh, fake_link, risk={**status_payload()["risk"], "pair_pause_hours": 48})
    texts = json.dumps((await rh_reader.get("/decisions", params={"limit": 200})).json())
    assert "24 horas" not in texts
