"""Funcoes puras da camada nova da API: textos, limites, desempenho, contrato do WebSocket, parametros."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from api.reasons import FALLBACK, GLOSSARY, REASONS, STATES, translate
from api.runner_link import Probe
from api.schemas_backtest import BacktestCreate
from api.schemas_control import EmergencyStopRequest, ResumeRequest
from api.services.backtests import MAX_GRID_COMBINATIONS, resolve_params
from api.services.commands import command_ttl, operator_label, to_out
from api.services.fmt import number, pct
from api.services.limits import build_limits
from api.services.livegate import GateEvaluation, criteria_of, eligibility_of, gate_out
from api.services.perf_math import covered_seconds, daily_returns, idle_fraction, profit_factor_of, sharpe
from api.services.settings_service import (
    HARD_PATHS,
    _deep_merge,
    _unknown,
    activation_of,
    field_specs,
    friendly_error,
    hard_limits,
)
from api.services.trading import safe_cell
from api.ws_schema import (
    CHANNELS,
    RUNNER_TO_CHANNEL,
    ClientMessage,
    build_ws_schema_text,
)
from engine.application.backtest.models import EquityPoint
from engine.application.paper_metrics import LiveGateThresholds, PaperMetrics, evaluate_live_gate
from engine.application.risk.rejections import RejectionCode
from engine.application.trading_config import ConfigDocument
from engine.domain.risk import RiskConfig
from engine.infrastructure.persistence.models import ControlCommandRow
from engine.runner.paper_control import COMMAND_TTL, LIQUIDATE_TTL, CommandName

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
D = Decimal


# --------------------------------------------------------------------------- textos


def test_every_rejection_code_has_a_plain_portuguese_translation() -> None:
    for code in RejectionCode:
        reason = translate(code.value)
        assert reason is not FALLBACK and reason.text and reason.label in {"Não entrou", "Esperou"}
        assert "_" not in reason.text and code.value not in reason.text


def test_decision_outcomes_and_unknown_codes() -> None:
    assert translate("approved").kind == "entered" and translate("no_signal").kind == "waited"
    assert translate("alguma_coisa_nova") is FALLBACK
    assert translate("alguma_coisa_nova", traded=True).label == "Entrou"
    assert {"stops_blind", "history_gap", "catch_up", "event_window", "reward_risk"} <= set(REASONS)


def test_states_glossary_have_plain_text_and_no_infrastructure_jargon() -> None:
    assert {"RUNNING", "PAUSED", "HALTED", "FAILED_PERSISTENCE", "OFFLINE"} <= set(STATES)
    banned = ("redis", "outbox", "timescale", "hmac", "httponly", "docker")
    for label, detail in STATES.values():
        assert not any(word in (label + detail).lower() for word in banned)
    assert len({g.term for g in GLOSSARY}) == len(GLOSSARY) and len(GLOSSARY) >= 15


def test_brazilian_number_format() -> None:
    assert number(1234567.891) == "1.234.567,89" and number(0.5, 1) == "0,5"
    assert pct(D("1.234")) == "1,2%" and pct(5, 0) == "5%"


# --------------------------------------------------------------------------- limites


def view(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "day_start_equity": "10000",
        "equity": "10000",
        "day_pnl": "0",
        "drawdown": "0",
        "loss_streaks": {},
        "pair_pause_until": {},
        "open_positions": [],
        "exposure": "0",
        "day": "2026-10-20",
    }
    return base | overrides


def by_key(limits: Any) -> dict[str, Any]:
    return {limit.key: limit for limit in limits}


def test_limits_show_current_value_against_the_limit_with_a_state_word() -> None:
    limits = by_key(build_limits(view(day_pnl="-30", drawdown="0.018"), RiskConfig(), NOW))
    loss = limits["daily_loss"]
    assert (loss.value, loss.limit, loss.level, loss.status_text) == (0.3, 1.0, "ok", "Folgado")
    assert loss.summary == "Perda diária: 0,3% de 1,0% - Folgado"
    assert limits["drawdown"].summary.startswith("Queda do saldo (drawdown): 1,8% de 5,0%")
    assert list(limits) == [
        "daily_loss", "drawdown", "loss_streak", "open_positions", "total_exposure", "daily_target",
    ]  # fmt: skip


def test_limits_turn_to_attention_and_active_with_the_until_time() -> None:
    near = by_key(build_limits(view(day_pnl="-70", drawdown="0.04"), RiskConfig(), NOW))
    assert near["daily_loss"].level == "warn" and near["drawdown"].level == "warn"
    until = NOW + timedelta(hours=5)
    hit = by_key(build_limits(view(day_pnl="-100", daily_pause_until=until.isoformat()), RiskConfig(), NOW))
    assert hit["daily_loss"].level == "bad" and hit["daily_loss"].status_text == "ATIVA"
    assert hit["daily_loss"].active_until == until and "17:00 UTC" in hit["daily_loss"].summary
    halted = by_key(build_limits(view(halted=True, drawdown="0.05"), RiskConfig(), NOW))
    assert halted["drawdown"].status_text == "DESLIGADO" and halted["drawdown"].level == "bad"


def test_loss_streak_pair_pause_positions_exposure_and_target() -> None:
    streak = by_key(build_limits(view(loss_streaks={"ETH/USDT": 1}), RiskConfig(), NOW))["loss_streak"]
    assert streak.name == "Perdas seguidas (ETH/USDT)" and streak.level == "warn" and streak.value == 1
    until = NOW + timedelta(hours=20)
    paused = by_key(build_limits(view(pair_pause_until={"ETH/USDT": until.isoformat()}), RiskConfig(), NOW))
    assert paused["loss_streak"].level == "bad" and paused["loss_streak"].active_until == until
    full = by_key(
        build_limits(view(open_positions=["BTC/USDT", "ETH/USDT"], exposure="0.55"), RiskConfig(), NOW)
    )
    assert full["open_positions"].status_text == "No limite" and full["total_exposure"].level == "warn"
    target = by_key(build_limits(view(day_pnl="120", target_reached_on="2026-10-20"), RiskConfig(), NOW))
    assert target["daily_target"].status_text == "Atingida" and target["daily_target"].level == "ok"


# --------------------------------------------------------------------------- desempenho


def point(day: int, equity: str, hour: int = 12) -> EquityPoint:
    return EquityPoint(
        at=datetime(2026, 10, day, hour, tzinfo=UTC), equity=D(equity), realized=D(equity), in_position=False
    )


def test_daily_returns_use_the_last_equity_of_each_utc_day() -> None:
    curve = [point(1, "10100", 8), point(1, "10200", 20), point(2, "10200"), point(3, "10404")]
    assert daily_returns(curve, D(10_000)) == pytest.approx([0.02, 0.0, 0.02])


def test_sharpe_needs_two_days_and_a_nonzero_deviation() -> None:
    assert sharpe([0.01]) is None and sharpe([0.01, 0.01, 0.01]) is None
    assert sharpe([0.01, -0.005, 0.02]) == pytest.approx(12.6526, rel=1e-3)


def test_idle_fraction_counts_only_time_without_an_open_position() -> None:
    start, end = NOW, NOW + timedelta(hours=10)
    held = [
        (NOW + timedelta(hours=1), NOW + timedelta(hours=3)),
        (NOW + timedelta(hours=2), NOW + timedelta(hours=4)),
    ]
    assert covered_seconds(held, start, end) == 3 * 3600
    assert idle_fraction(held, start, end) == pytest.approx(0.7)
    assert idle_fraction([(NOW + timedelta(hours=8), None)], start, end) == pytest.approx(0.8)
    assert idle_fraction([], start, start) is None
    assert profit_factor_of(D(30), D(20)) == 1.5 and profit_factor_of(D(30), D(0)) is None


# --------------------------------------------------------------------------- trava do Live


def evaluation(**metrics: Any) -> GateEvaluation:
    values: dict[str, Any] = {
        "operating_days": 23, "trades": 17, "losing_trades": 6, "total_pnl": D(100), "gross_profit": D(400),
        "gross_loss": D(300), "max_drawdown": D("0.018"), "profit_factor": D("1.34"), "payoff": D("1.1"),
        "worst_daily_loss": D(50), "worst_daily_loss_pct": D("0.005"),
    }  # fmt: skip
    thresholds = LiveGateThresholds.from_risk(RiskConfig())
    reason = metrics.pop("ineligible", None)
    report = evaluate_live_gate(PaperMetrics(**(values | metrics)), thresholds, ineligible_reason=reason)
    return GateEvaluation("run-1", report, thresholds, reason, None)


def test_gate_criteria_show_value_against_target_and_three_states() -> None:
    rows = {c.code: c for c in criteria_of(evaluation(ineligible="calendario aberto"))}
    assert (
        rows["operating_days"].status == "in_progress"
        and rows["operating_days"].missing_text == "Faltam 37 dias"
    )
    assert rows["operating_days"].value_text == "23 de 60"
    assert rows["max_drawdown"].status == "met" and rows["max_drawdown"].value_text == "1,8%"
    assert rows["profit_factor"].status == "in_progress"
    assert rows["min_trades"].status == "in_progress" and rows["min_trades"].missing_text == "Faltam 13"
    assert rows["min_losses"].status == "met" and rows["min_losses"].value_text == "6 de 5"
    assert rows["eligible_for_live"].status == "failed" and "calendário de eventos" in (
        rows["eligible_for_live"].reason or ""
    )


def test_gate_failed_when_measured_and_bad_and_live_is_never_available() -> None:
    bad = evaluation(trades=40, losing_trades=12, profit_factor=D("1.1"), max_drawdown=D("0.06"))
    rows = {c.code: c for c in criteria_of(bad)}
    assert rows["profit_factor"].status == "failed" and rows["max_drawdown"].status == "failed"
    good = evaluation(operating_days=61, trades=40, losing_trades=12, profit_factor=D("1.8"))
    out = gate_out(good)
    assert out.approved and not out.live_available and out.phrase_required == "OPERAR COM DINHEIRO REAL"
    assert (
        out.unavailable_reason == "Live ainda não disponível" and out.first_activation_reduced_risk_days == 7
    )
    assert eligibility_of(good).live_available is False and eligibility_of(good).criteria_met == 7


# --------------------------------------------------------------------------- comandos


def test_command_ttls_follow_the_runner_rules() -> None:
    assert command_ttl(CommandName.EMERGENCY_STOP, False) is None
    assert command_ttl(CommandName.EMERGENCY_STOP, True) == LIQUIDATE_TTL
    assert (
        command_ttl(CommandName.PAUSE, False) == COMMAND_TTL == command_ttl(CommandName.APPLY_CONFIG, False)
    )


def test_operator_label_keeps_the_login_for_the_audit() -> None:
    assert operator_label("ana.silva", None) == "ana.silva"
    assert operator_label("ana.silva", "Ana") == "Ana (ana.silva)"
    assert operator_label("ana.silva", "ANA.SILVA") == "ana.silva"
    assert len(operator_label("u" * 90, "N" * 60)) <= 100


def test_pending_command_text_says_pending_delivery_and_why() -> None:
    row = ControlCommandRow(
        id="a" * 32, name="emergency-stop", operator="ana", reason="r", liquidate=False, params={},
        state="pending_delivery", channel=None, created_at=NOW, expires_at=None, result={"waiting": "o robô não está enviando sinal de vida"},
    )  # fmt: skip
    out = to_out(row)
    assert out.state == "pending_delivery" and out.expires_at is None
    assert out.state_text.startswith("Pendente de entrega") and "sinal de vida" in out.state_text


def test_emergency_stop_with_liquidation_requires_name_and_reason() -> None:
    EmergencyStopRequest(liquidate=False)
    EmergencyStopRequest(liquidate=True, operator_name="Ana", reason="incidente")
    for bad in (
        {"liquidate": True},
        {"liquidate": True, "operator_name": "Ana"},
        {"liquidate": True, "reason": "x y z"},
    ):
        with pytest.raises(ValidationError):
            EmergencyStopRequest(**bad)
    with pytest.raises(ValidationError):
        ResumeRequest(reason="ok", operator_name=" ")


def test_probe_defaults() -> None:
    assert Probe(reachable=False).latest == {}


# --------------------------------------------------------------------------- backtests


def create(**overrides: Any) -> BacktestCreate:
    return BacktestCreate.model_validate({"symbols": ["btc/usdt"], **overrides})


def test_backtest_request_defaults_and_period_rules() -> None:
    body = create()
    assert body.symbols == ["BTC/USDT"] and body.years == 1.0 and body.capital == D(10_000)
    for bad in (
        {"years": 1, "start": "2026-01-01T00:00:00Z", "end": "2026-02-01T00:00:00Z"},
        {"start": "2026-01-01T00:00:00Z"},
        {"start": "2026-01-01T00:00:00", "end": "2026-02-01T00:00:00"},
        {"start": "2026-02-01T00:00:00Z", "end": "2026-01-01T00:00:00Z"},
        {"params": {"nada": "1"}},
        {"symbols": ["BTC/USDT", "BTC/USDT"]},
        {"symbols": ["BTCUSDT"]},
        {"taker_fee": "0.5"},
        {"capital": "-1"},
        {"strategies": []},
        {"strategies": ["martingale"]},
    ):
        with pytest.raises(ValidationError):
            create(**bad)


def test_resolve_params_resolves_the_period_and_validates_walk_forward() -> None:
    resolved = resolve_params(create(years=0.5), NOW, 5)
    assert resolved["days"] == 182 and resolved["end"] == "2026-10-20T12:00:00+00:00"
    assert resolved["capital"] == "10000" and "years" not in resolved
    from api.errors import Unprocessable

    with pytest.raises(Unprocessable, match="período máximo"):
        resolve_params(create(years=6), NOW, 5)
    with pytest.raises(Unprocessable, match="curto demais"):
        resolve_params(create(years=0.2, walk_forward=True), NOW, 5)
    with pytest.raises(Unprocessable, match="futuro"):
        resolve_params(create(start="2026-10-01T00:00:00Z", end="2026-12-01T00:00:00Z"), NOW, 5)
    wide = "min_confidence=0.5,0.55,0.6,0.65,0.7;rsi_min=40,45,50,55;reward_risk=2,3"
    assert MAX_GRID_COMBINATIONS < 5 * 4 * 2
    with pytest.raises(Unprocessable, match="Combinações demais"):
        resolve_params(create(years=2, walk_forward=True, grid=wide), NOW, 5)
    with pytest.raises(Unprocessable, match="Grade"):
        resolve_params(create(years=2, walk_forward=True, grid="lixo=1"), NOW, 5)
    ok = resolve_params(create(years=2, walk_forward=True), NOW, 5)
    assert ok["walk_forward"] is True


# --------------------------------------------------------------------------- configuracoes


def test_friendly_validation_messages_are_plain_and_never_echo_the_value() -> None:
    assert "0.01" in friendly_error({"type": "less_than_equal", "ctx": {"le": "0.01"}, "msg": "x"})
    assert friendly_error({"type": "missing"}) == "Campo obrigatório."
    assert friendly_error({"type": "extra_forbidden"}) == "Campo desconhecido."
    assert friendly_error({"type": "decimal_parsing"}) == "Informe um número válido."
    assert friendly_error({"type": "value_error", "msg": "Value error, ligue ao menos uma estratégia"}) == (
        "ligue ao menos uma estratégia"
    )


def test_unknown_keys_are_reported_at_any_depth_and_merge_keeps_omitted_fields() -> None:
    model = ConfigDocument.model_fields["strategies"].annotation
    found = _unknown(model, {"elephant": {"rsi_min": 40, "rsi_maxx": 1}, "typo": 1}, "strategies")  # type: ignore[arg-type]
    assert sorted(e.path for e in found) == ["strategies.elephant.rsi_maxx", "strategies.typo"]
    merged = _deep_merge({"a": {"x": 1, "y": 2}, "b": 3}, {"a": {"x": 9}})
    assert merged == {"a": {"x": 9, "y": 2}, "b": 3}


def test_field_specs_cover_every_field_and_flag_the_hard_ceilings() -> None:
    specs = field_specs()
    assert set(specs) == {"pairs", "strategies", "risk", "ai"}
    risk = {s.path: s for s in specs["risk"]}
    assert risk["risk.risk_per_trade"].maximum == 0.01 and risk["risk.risk_per_trade"].hard_limit
    assert risk["risk.risk_per_trade"].default == "0.005" and risk["risk.risk_per_trade"].unit == "%"
    assert risk["risk.pair_pause_hours"].minimum == 24 and risk["risk.max_open_positions"].maximum == 2
    assert {p for p in HARD_PATHS} <= set(risk)
    assert risk["risk.blocked_assets"].kind == "list" and risk["risk.blocked_assets"].default == ["BNB"]
    assert any(s.path == "strategies.elephant.detector.lookback" for s in specs["strategies"])
    assert next(s for s in specs["pairs"] if s.path == "pairs.timeframe").choices


def test_hard_limits_are_listed_in_plain_text() -> None:
    texts = {h.path: h.text for h in hard_limits()}
    assert texts["risk.risk_per_trade"] == "Risco por operação: no máximo 1,00%"
    assert texts["risk.max_open_positions"].endswith("no máximo 2")
    assert texts["risk.pair_pause_hours"].endswith("no mínimo 24 horas")


def test_activation_text_distinguishes_now_next_start_and_mixed() -> None:
    from api.schemas_settings import FieldChangeOut

    def change(applies: str, label: str) -> FieldChangeOut:
        return FieldChangeOut(
            path=label, label=label, before=1, after=2, before_text="1", after_text="2", effect="decrease",
            increases_risk=False, cost_text="", applies=applies,  # type: ignore[arg-type]
        )  # fmt: skip

    assert activation_of([])[0] == "none"
    assert activation_of([change("now", "A")])[0] == "immediate"
    kind, text, now, later = activation_of([change("next_start", "B")])
    assert kind == "next_start" and "próximo início" in text and not now and later == ["B"]
    kind, text, now, later = activation_of([change("now", "A"), change("next_start", "B")])
    assert kind == "next_start" and now == ["A"] and later == ["B"] and "Parte vale agora" in text


# --------------------------------------------------------------------------- CSV e WebSocket


@pytest.mark.parametrize("raw", ["=SOMA(A1)", "+1", "-1+2", "@cmd", "\tx", "\rx"])
def test_csv_cells_that_look_like_formulas_become_text(raw: str) -> None:
    assert safe_cell(raw) == "'" + raw
    assert safe_cell("BTC/USDT") == "BTC/USDT" and safe_cell(None) == ""


def test_ws_channels_and_runner_mapping() -> None:
    assert set(CHANNELS) == {"status", "decisions", "orders", "trades", "equity", "alerts", "heartbeat"}
    assert set(RUNNER_TO_CHANNEL.values()) <= set(CHANNELS)
    assert RUNNER_TO_CHANNEL["state"] == "status" and RUNNER_TO_CHANNEL["risk"] == "alerts"


def test_ws_client_messages_are_validated_strictly() -> None:
    from pydantic import TypeAdapter

    adapter = TypeAdapter(ClientMessage)
    assert (
        adapter.validate_json('{"type":"subscribe","channels":["status"],"last_event_id":"ab:3"}').type
        == "subscribe"
    )
    assert adapter.validate_json('{"type":"pong"}').type == "pong"
    for bad in (
        '{"type":"subscribe","channels":[]}',
        '{"type":"subscribe","channels":["admin"]}',
        '{"type":"subscribe","channels":["status"],"extra":1}',
        '{"type":"comando","channels":["status"]}',
        "nao-json",
        '{"type":"subscribe","channels":["status"],"last_event_id":"' + "x" * 50 + '"}',
    ):
        with pytest.raises(ValidationError):
            adapter.validate_json(bad)


def test_ws_schema_file_is_up_to_date_and_describes_both_directions() -> None:
    from api.export_openapi import WS_SCHEMA_PATH

    assert WS_SCHEMA_PATH.read_text(encoding="utf-8") == build_ws_schema_text(), (
        "api/ws_schema.json desatualizado"
    )
    schema = json.loads(build_ws_schema_text())
    assert {"hello", "event", "runner", "resync", "ping", "error", "subscribed"} <= {
        m["properties"]["type"]["const"]
        if "const" in m["properties"]["type"]
        else m["properties"]["type"]["default"]
        for m in (schema["$defs"][name] for name in schema["$defs"] if name.endswith("Message"))
        if "type" in m.get("properties", {})
    }
    assert schema["channels"] == list(CHANNELS)
