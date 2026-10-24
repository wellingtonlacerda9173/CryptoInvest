"""Configurações versionadas: validação por campo, detecção de aumento de risco e gravação append-only.

Regras (impostas aqui E no runner):
- o servidor REJEITA valores acima dos tetos rígidos (`RiskConfig`), não importa quem peça;
- reduções de risco valem na hora (comando `apply-config` ao runner, que as revalida);
- aumentos (ou efeito incerto) exigem confirmação explícita e só valem no próximo (re)início do robô, e
  com posição aberta o runner nem nesse início os aceita (`effective_document`);
- toda gravação passa por reautenticação recente (2FA), guarda ator, motivo, diff e hash e é auditada.
"""

from collections.abc import Iterable
from decimal import Decimal
from types import UnionType
from typing import Any, Union, cast, get_args, get_origin

from pydantic import BaseModel, ValidationError
from pydantic.fields import FieldInfo
from sqlalchemy import select

from api.audit import AuditAction, AuditResult, AuditService, RequestMeta
from api.container import Container
from api.errors import Conflict, FieldError, Unprocessable
from api.schemas_control import CommandOut
from api.schemas_settings import (
    ActivationName,
    EffectiveConfigOut,
    FieldChangeOut,
    FieldSpecOut,
    HardLimitOut,
    SectionName,
    SettingsErrorOut,
    SettingsHistoryPage,
    SettingsOut,
    SettingsUpdateOut,
    SettingsValidationOut,
    SettingsVersionOut,
)
from api.services.commands import CommandService, to_out
from api.services.fmt import pct
from api.services.runner_status import RunnerStatusService
from api.services.trading import stored_capital
from engine.application.trading_config import (
    LABELS,
    SECTIONS,
    ConfigDocument,
    Effect,
    FieldChange,
    default_document,
    describe,
    diff_documents,
    runner_patch,
)
from engine.domain.risk import (
    MAX_CONSECUTIVE_LOSSES,
    MAX_DAILY_LOSS,
    MAX_DRAWDOWN,
    MAX_OPEN_POSITIONS,
    MAX_RISK_PER_TRADE,
    MAX_TOTAL_EXPOSURE,
    MIN_PAIR_PAUSE_HOURS,
)
from engine.domain.rules import DEFAULT_MIN_REWARD_RISK
from engine.infrastructure.persistence.config_repository import (
    ConfigConflictError,
    ConfigRepository,
    ConfigVersion,
)
from engine.infrastructure.persistence.models import ControlCommandRow
from engine.runner.paper_control import CommandName

RESTART_RULE = (
    "Mudanças que reduzem o risco valem na hora. Mudanças que aumentam o risco só valem no próximo início "
    "do robô, e apenas se não houver nenhuma posição aberta."
)
DEFAULT_CAPITAL = Decimal(10_000)
PCT_LIKE = frozenset(
    {
        "risk_per_trade",
        "max_daily_loss",
        "max_position_fraction",
        "daily_profit_target",
        "max_drawdown",
        "max_coin_exposure_fraction",
        "max_total_exposure_fraction",
        "max_spread",
        "taker_fee_rate",
        "max_entry_deviation",
        "partial_exit_fraction",
        "profit_giveback",
    }
)
HARD: tuple[tuple[str, str, Decimal | int, str], ...] = (
    ("risk.risk_per_trade", "max", MAX_RISK_PER_TRADE, "Risco por operação: no máximo {v}"),
    ("risk.max_daily_loss", "max", MAX_DAILY_LOSS, "Perda diária: no máximo {v}"),
    ("risk.max_drawdown", "max", MAX_DRAWDOWN, "Queda máxima do saldo: no máximo {v}"),
    ("risk.max_open_positions", "max", MAX_OPEN_POSITIONS, "Posições abertas ao mesmo tempo: no máximo {v}"),
    (
        "risk.consecutive_loss_limit",
        "max",
        MAX_CONSECUTIVE_LOSSES,
        "Perdas seguidas permitidas: no máximo {v}",
    ),
    (
        "risk.pair_pause_hours",
        "min",
        MIN_PAIR_PAUSE_HOURS,
        "Pausa do par após perdas seguidas: no mínimo {v} horas",
    ),
    ("risk.max_total_exposure_fraction", "max", MAX_TOTAL_EXPOSURE, "Exposição total: no máximo {v}"),
    (
        "risk.min_reward_risk",
        "min",
        DEFAULT_MIN_REWARD_RISK,
        "Retorno/risco mínimo: no mínimo {v}",
    ),
)
HARD_PATHS = frozenset(path for path, *_ in HARD)


def friendly_error(error: dict[str, Any]) -> str:
    """Mensagem em português simples para o erro de validação do pydantic (sem ecoar o valor recebido)."""
    kind = str(error.get("type"))
    ctx = error.get("ctx") or {}
    match kind:
        case "less_than_equal" | "less_than":
            limit = ctx.get("le", ctx.get("lt"))
            return f"O valor máximo permitido é {limit} (teto de segurança do servidor)."
        case "greater_than_equal" | "greater_than":
            limit = ctx.get("ge", ctx.get("gt"))
            return f"O valor mínimo permitido é {limit}."
        case "missing":
            return "Campo obrigatório."
        case "extra_forbidden":
            return "Campo desconhecido."
        case "decimal_parsing" | "int_parsing" | "float_parsing" | "decimal_type" | "int_type" | "float_type":
            return "Informe um número válido."
        case "bool_parsing" | "bool_type":
            return "Informe verdadeiro ou falso."
        case "string_pattern_mismatch" | "string_type" | "string_too_short" | "string_too_long":
            return "Texto em formato inválido."
        case "enum":
            return "Valor não permitido."
        case "too_short":
            return "A lista precisa ter ao menos um item."
        case "too_long":
            return "A lista tem itens demais."
        case "value_error":
            return str(error.get("msg", "Valor inválido.")).removeprefix("Value error, ")
        case _:
            return str(error.get("msg", "Valor inválido."))


def _error_items(error: ValidationError, section: str) -> list[SettingsErrorOut]:
    found: list[SettingsErrorOut] = []
    for item in error.errors(include_input=False, include_url=False):
        loc = [str(p) for p in item["loc"] if str(p) != section or False]
        path = ".".join([section, *(p for p in loc if p != section)]) if loc else section
        found.append(SettingsErrorOut(path=path, message=friendly_error(cast("dict[str, Any]", item))))
    return found


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in patch.items():
        merged[key] = (
            _deep_merge(base[key], value)
            if isinstance(value, dict) and isinstance(base.get(key), dict)
            else value
        )
    return merged


def _unknown(model: type[BaseModel], value: dict[str, Any], prefix: str) -> list[SettingsErrorOut]:
    """Chave desconhecida é erro (um erro de digitação nunca pode virar valor padrão em silêncio)."""
    found: list[SettingsErrorOut] = []
    for key, item in value.items():
        info = model.model_fields.get(key)
        path = f"{prefix}.{key}"
        if info is None:
            found.append(SettingsErrorOut(path=path, message="Campo desconhecido."))
        elif isinstance(item, dict):
            inner = _model_of(info)
            if inner is not None:
                found.extend(_unknown(inner, item, path))
    return found


def _model_of(info: FieldInfo) -> type[BaseModel] | None:
    annotation = info.annotation
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return None


def _bounds(info: FieldInfo) -> tuple[float | None, float | None]:
    low = high = None
    for meta in info.metadata:
        for attr in ("ge", "gt"):
            if (v := getattr(meta, attr, None)) is not None:
                low = float(v)
        for attr in ("le", "lt"):
            if (v := getattr(meta, attr, None)) is not None:
                high = float(v)
    return low, high


def _kind(annotation: Any) -> tuple[str, list[str] | None]:
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        inner = [a for a in get_args(annotation) if a is not type(None)]
        return _kind(inner[0]) if inner else ("text", None)
    if origin in (tuple, list, frozenset, set):
        return "list", None
    if annotation is bool:
        return "boolean", None
    if annotation is int:
        return "integer", None
    if annotation in (float, Decimal):
        return "number", None
    if isinstance(annotation, type) and hasattr(annotation, "__members__"):
        return "choice", [str(m.value) for m in cast("Iterable[Any]", annotation)]
    return "text", None


def _walk(model: type[BaseModel], prefix: str, defaults: dict[str, Any], out: list[FieldSpecOut]) -> None:
    for name, info in model.model_fields.items():
        path = f"{prefix}.{name}"
        inner = _model_of(info)
        if inner is not None:
            _walk(inner, path, defaults.get(name, {}), out)
            continue
        kind, choices = _kind(info.annotation)
        low, high = _bounds(info)
        out.append(
            FieldSpecOut(
                path=path,
                label=LABELS.get(path) or LABELS.get(name) or name.replace("_", " ").capitalize(),
                kind=cast("Any", kind),
                default=defaults.get(name),
                minimum=low,
                maximum=high,
                hard_limit=path in HARD_PATHS,
                unit="%" if name in PCT_LIKE else "",
                choices=choices,
            )
        )


def field_specs() -> dict[str, list[FieldSpecOut]]:
    defaults = default_document().dump()
    specs: dict[str, list[FieldSpecOut]] = {}
    for section in SECTIONS:
        info = ConfigDocument.model_fields[section]
        model = _model_of(info)
        assert model is not None  # noqa: S101
        bucket: list[FieldSpecOut] = []
        _walk(model, section, defaults[section], bucket)
        specs[section] = bucket
    return specs


def hard_limits() -> list[HardLimitOut]:
    items: list[HardLimitOut] = []
    for path, kind, value, template in HARD:
        shown = pct(Decimal(str(value)) * 100, 2) if path.rsplit(".", 1)[1] in PCT_LIKE else str(value)
        if path.endswith("min_reward_risk"):
            shown = f"{value}"
        items.append(
            HardLimitOut(
                path=path,
                label=LABELS.get(path.split(".", 1)[1], path),
                kind=cast("Any", kind),
                value=float(value),
                text=template.format(v=shown),
            )
        )
    return items


APPLICATION: dict[str, tuple[str, str]] = {
    "pending_delivery": ("pending", "Aguardando o robô aplicar a redução de risco."),
    "delivered": ("pending", "Enviada ao robô, aguardando a confirmação."),
    "applied": ("applied", "A redução de risco já está valendo no robô."),
    "partially_applied": (
        "partially_applied",
        "O robô aplicou só parte: o que aumenta o risco só vale no próximo início.",
    ),
    "refused": ("refused", "O robô recusou a mudança: ela só vale no próximo início."),
    "expired": ("expired", "Não chegou ao robô a tempo: a mudança só vale no próximo início."),
    "discarded": ("expired", "O robô descartou o pedido: a mudança só vale no próximo início."),
    "failed": ("failed", "O robô não conseguiu aplicar: a mudança só vale no próximo início."),
}


def version_out(version: ConfigVersion, command_state: str | None = None) -> SettingsVersionOut:
    application, application_text = APPLICATION.get(
        command_state or "",
        ("none", "Vale no próximo início do robô." if version.activation == "next_start" else ""),
    )
    if command_state == "applied" and version.activation == "next_start":
        application_text = "A redução de risco já vale no robô; o restante só vale no próximo início."
    return SettingsVersionOut(
        application=cast("Any", application),
        application_text=application_text,
        id=version.id,
        parent_id=version.parent_id,
        created_at=version.created_at,
        actor=version.actor,
        reason=version.reason,
        sections=list(version.sections),
        activation=cast("Any", version.activation),
        risk_increase=version.risk_increase,
        config_hash=version.config_hash,
        chain_hash=version.chain_hash,
        changes=[FieldChangeOut.model_validate(c) for c in version.diff],
    )


def change_out(change: FieldChange, capital: Decimal) -> FieldChangeOut:
    text = describe(change, capital)
    applies = (
        "now"
        if change.section == "ai" or (change.section == "risk" and change.effect is Effect.DECREASE)
        else "next_start"
    )
    return FieldChangeOut(
        path=change.path,
        label=text["label"],
        before=change.before,
        after=change.after,
        before_text=text["before_text"],
        after_text=text["after_text"],
        effect=cast("Any", change.effect.value),
        increases_risk=change.increases_risk,
        cost_text=text["cost_text"],
        applies=cast("Any", applies),
    )


def activation_of(changes: list[FieldChangeOut]) -> tuple[ActivationName, str, list[str], list[str]]:
    if not changes:
        return "none", "Nada mudou.", [], []
    now = [c.label for c in changes if c.applies == "now"]
    later = [c.label for c in changes if c.applies == "next_start"]
    if not later:
        return "immediate", "Vale agora: a mudança só reduz o risco ou não mexe nas ordens.", now, later
    if not now:
        return (
            "next_start",
            "Só vale no próximo início do robô, sem nenhuma posição aberta. "
            "O robô em execução segue como está.",
            now,
            later,
        )
    return (
        "next_start",
        f"Parte vale agora ({', '.join(now)}); o restante ({', '.join(later)}) só vale no próximo início do "
        "robô, sem posição aberta.",
        now,
        later,
    )


def as_applied_by_runner(
    changes: list[FieldChangeOut],
    effective: dict[str, Any] | None,
    patch: dict[str, Any],
    wanted: dict[str, Any],
) -> tuple[list[FieldChangeOut], bool]:
    """Corrige `applies`: uma redução só vale na hora se o robô EM EXECUÇÃO a aceitaria (vai no `patch`) ou
    já está nesse valor. Sem configuração efetiva do robô, toda redução de risco vale só no próximo início.
    Devolve também se alguma mudança foi rebaixada por falta de robô."""
    fixed: list[FieldChangeOut] = []
    demoted = False
    for change in changes:
        field = change.path.removeprefix("risk.")
        if change.applies == "now" and change.path.startswith("risk."):
            reaches = effective is not None and (field in patch or effective.get(field) == wanted.get(field))
            if not reaches:
                demoted = demoted or effective is None
                change = change.model_copy(update={"applies": "next_start"})
        fixed.append(change)
    return fixed, demoted


NO_RUNNER_TEXT = (
    "O robô não está ligado (ou não informou a configuração em vigor): a mudança só vale no próximo início, "
    "sem nenhuma posição aberta."
)


class SettingsService:
    def __init__(self, container: Container, audit: AuditService | None = None) -> None:
        self._c = container
        self._audit = audit or AuditService(container.sessions)

    async def _latest(self) -> ConfigVersion | None:
        async with self._c.sessions() as db:
            return await ConfigRepository(db).latest()

    async def reference_capital(self) -> Decimal:
        snapshot = await RunnerStatusService(self._c).snapshot()
        if snapshot.data and snapshot.data.get("equity"):
            return Decimal(str(snapshot.data["equity"]))
        async with self._c.sessions() as db:
            from api.services.queries import latest_run

            run = await latest_run(db)
            capital = None if run is None else (await stored_capital(db, run.run_id))[0]
        return capital or DEFAULT_CAPITAL

    async def effective_risk(self) -> dict[str, Any] | None:
        """Risco que o robô EM EXECUÇÃO usa (o que ele próprio reporta). Desligado, sem sinal ou sem o campo:
        `None`, e nada vale na hora (só no próximo início)."""
        snapshot = await RunnerStatusService(self._c).snapshot()
        risk = None if not snapshot.is_on or not snapshot.data else snapshot.data.get("risk")
        return dict(risk) if isinstance(risk, dict) and risk else None

    async def _command_states(self, ids: list[int]) -> dict[int, str]:
        if not ids:
            return {}
        version = ControlCommandRow.params["version"].astext
        stmt = (
            select(ControlCommandRow.params, ControlCommandRow.state)
            .where(ControlCommandRow.name == CommandName.APPLY_CONFIG.value)
            .where(version.in_([str(i) for i in ids]))
            .order_by(ControlCommandRow.created_at)
        )
        async with self._c.sessions() as db:
            rows = (await db.execute(stmt)).all()
        return {int(params["version"]): state for params, state in rows}

    async def _versions_out(self, versions: list[ConfigVersion]) -> list[SettingsVersionOut]:
        states = await self._command_states([v.id for v in versions])
        return [version_out(v, states.get(v.id)) for v in versions]

    # ------------------------------------------------------------------ leitura

    async def read(self, *, can_edit: bool) -> SettingsOut:
        latest = await self._latest()
        document = latest.document if latest else default_document()
        snapshot = await RunnerStatusService(self._c).snapshot()
        running_version = None if not snapshot.data else snapshot.data.get("config_version")
        matches: bool | None = None
        if snapshot.alive and latest is not None and running_version is not None:
            matches = running_version == latest.id
        shown = None if latest is None else (await self._versions_out([latest]))[0]
        applied = None if not snapshot.data else snapshot.data.get("applied_config_version")
        if snapshot.alive and latest is not None and isinstance(applied, int) and applied >= latest.id:
            matches = True
        return SettingsOut(
            version=shown,
            document=document.dump(),
            defaults=default_document().dump(),
            fields=field_specs(),
            hard_limits=hard_limits(),
            effective=EffectiveConfigOut(
                config_hash=None if not snapshot.data else snapshot.data.get("config_hash"),
                version_id=running_version,
                runner_connected=snapshot.alive,
                matches_latest=matches,
                restart_rule=RESTART_RULE,
            ),
            can_edit=can_edit,
            capital_reference=float(await self.reference_capital()),
        )

    async def history(self, *, limit: int, cursor: int | None) -> SettingsHistoryPage:
        async with self._c.sessions() as db:
            rows = await ConfigRepository(db).history(limit=limit + 1, before_id=cursor)
        page = rows[:limit]
        return SettingsHistoryPage(
            items=await self._versions_out(page), next_cursor=page[-1].id if len(rows) > limit else None
        )

    # ------------------------------------------------------------------ validação

    async def _candidate(
        self, section: SectionName, value: dict[str, Any], base: ConfigDocument
    ) -> tuple[ConfigDocument | None, list[SettingsErrorOut]]:
        model = _model_of(ConfigDocument.model_fields[section])
        assert model is not None  # noqa: S101
        errors = _unknown(model, value, section)
        if errors:
            return None, errors
        merged = _deep_merge(base.dump()[section], value)
        try:
            return base.with_section(section, merged), []
        except ValidationError as error:
            return None, _error_items(error, section)

    async def validate(self, section: SectionName, value: dict[str, Any]) -> SettingsValidationOut:
        latest = await self._latest()
        base = latest.document if latest else default_document()
        candidate, errors = await self._candidate(section, value, base)
        if candidate is None:
            return SettingsValidationOut(
                valid=False,
                errors=errors,
                changes=[],
                increases_risk=False,
                requires_acknowledgement=False,
                activation="none",
                activation_text="Corrija os campos indicados.",
                config_hash=None,
            )
        capital = await self.reference_capital()
        effective = await self.effective_risk()
        wanted = candidate.dump()["risk"]
        patch = {} if effective is None else runner_patch(effective, wanted)
        changes, demoted = as_applied_by_runner(
            [change_out(c, capital) for c in diff_documents(base, candidate)], effective, patch, wanted
        )
        activation, text, now_fields, _ = activation_of(changes)
        if demoted and not now_fields:
            text = NO_RUNNER_TEXT
        increases = any(c.increases_risk for c in changes)
        return SettingsValidationOut(
            valid=True,
            errors=[],
            changes=changes,
            increases_risk=increases,
            requires_acknowledgement=increases,
            activation=activation,
            activation_text=text,
            config_hash=candidate.digest(),
        )

    # ------------------------------------------------------------------ gravação

    async def update(
        self,
        *,
        section: SectionName,
        value: dict[str, Any],
        reason: str,
        expected_version: int,
        acknowledge: bool,
        actor: str,
        operator: str,
        meta: RequestMeta,
    ) -> SettingsUpdateOut:
        latest = await self._latest()
        base = latest.document if latest else default_document()
        candidate, errors = await self._candidate(section, value, base)
        if candidate is None:
            await self._refused(actor, section, meta, "valores inválidos")
            raise Unprocessable(
                "Corrija os campos indicados e tente de novo.",
                field_errors=[
                    FieldError(loc=e.path.split("."), message=e.message, code="invalid") for e in errors
                ],
            )
        capital = await self.reference_capital()
        effective = await self.effective_risk()
        wanted = candidate.dump()["risk"]
        patch = {} if effective is None else runner_patch(effective, wanted)
        changes, demoted = as_applied_by_runner(
            [change_out(c, capital) for c in diff_documents(base, candidate)], effective, patch, wanted
        )
        if not changes:
            raise Conflict("Nada para salvar: os valores são iguais aos atuais.")
        increases = any(c.increases_risk for c in changes)
        if increases and not acknowledge:
            await self._refused(actor, section, meta, "aumento de risco sem confirmação")
            raise RiskIncreaseUnconfirmed(changes)
        activation, text, now_fields, later_fields = activation_of(changes)
        if demoted and not now_fields:
            text = NO_RUNNER_TEXT
        stored_activation = "immediate" if activation == "immediate" else "next_start"
        parent = latest.id if latest else None
        if expected_version != (parent or 0):
            raise Conflict("A configuração mudou enquanto você editava. Recarregue e tente de novo.")
        try:
            async with self._c.sessions.begin() as db:
                saved = await ConfigRepository(db).append(
                    candidate,
                    actor=actor,
                    reason=reason,
                    activation=stored_activation,
                    risk_increase=increases,
                    sections=[section],
                    diff=[c.model_dump(mode="json") for c in changes],
                    expected_parent=parent,
                )
                await self._audit.record(
                    actor=actor,
                    action=AuditAction.CONFIG_UPDATED,
                    target=f"config:{saved.id}",
                    result=AuditResult.SUCCESS,
                    meta=meta,
                    details={
                        "section": section,
                        "reason": reason,
                        "config_hash": saved.config_hash,
                        "risk_increase": increases,
                        "activation": stored_activation,
                        "changes": [
                            {"path": c.path, "before": c.before, "after": c.after, "effect": c.effect}
                            for c in changes
                        ],
                    },
                    session=db,
                )
        except ConfigConflictError as error:
            raise Conflict(str(error)) from None
        command: CommandOut | None = None
        if patch:
            accepted = await CommandService(self._c, self._audit).submit(
                name=CommandName.APPLY_CONFIG,
                operator=operator,
                reason=f"Configuração v{saved.id}: {reason}"[:280],
                actor=actor,
                meta=meta,
                params={"risk": patch, "version": saved.id, "config_hash": saved.config_hash},
            )
            command = to_out(await CommandService(self._c).get_row(accepted.id))
        return SettingsUpdateOut(
            version=(await self._versions_out([saved]))[0],
            activation=activation,
            activation_text=text,
            applied_now=now_fields,
            pending_restart=later_fields,
            command=command,
        )

    async def _refused(self, actor: str, section: str, meta: RequestMeta, why: str) -> None:
        await self._audit.record(
            actor=actor,
            action=AuditAction.CONFIG_REFUSED,
            target=f"config:{section}",
            result=AuditResult.DENIED,
            meta=meta,
            details={"section": section, "why": why},
        )


class RiskIncreaseUnconfirmed(Conflict):
    """409: a mudança aumenta o risco e falta o segundo clique (`acknowledge_risk_increase`)."""

    slug, title = "risk-increase-unconfirmed", "Confirme o aumento de risco"

    def __init__(self, changes: list[FieldChangeOut]) -> None:
        lines = [
            f"{c.label}: antes {c.before_text}, agora {c.after_text}. {c.cost_text}".strip()
            for c in changes
            if c.increases_risk
        ]
        super().__init__(
            "Esta mudança aumenta o risco e só vale no próximo início do robô. Confirme para salvar. "
            + " | ".join(lines)
        )
