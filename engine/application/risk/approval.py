import hashlib
import hmac
import secrets
from datetime import datetime
from decimal import Decimal
from typing import Final, Self

from pydantic import model_validator

from engine.domain.base import DomainModel
from engine.domain.decision import Decision, DecisionAction
from engine.domain.order import EntryOrder
from engine.domain.signal import Signal
from engine.domain.types import Fee, UtcDatetime, Volume

_ISSUER: Final = object()
_KEY: Final = secrets.token_bytes(32)


class ApprovalError(ValueError):
    """A aprovacao nao foi emitida pelo gate de risco neste processo, ou foi adulterada."""


class ApprovalExpiredError(ApprovalError):
    """A aprovacao passou do prazo de validade: a ordem deve ter sido cancelada, nao preenchida."""


class FillGuards(DomainModel):
    """Limites do fill fixados na aprovacao (e cobertos pelo selo): validade, teto de taxa e minimos.

    `max_fee_rate` e a taxa maxima aceita sobre o notional do fill (multiplo da taxa taker configurada);
    `min_quantity`/`min_notional` sao os filtros da corretora vigentes na aprovacao.
    """

    approved_at: UtcDatetime
    expires_at: UtcDatetime
    max_fee_rate: Fee
    min_quantity: Volume
    min_notional: Volume

    @model_validator(mode="after")
    def _check_order(self) -> Self:
        if self.expires_at <= self.approved_at:
            raise ValueError("expires_at must be after approved_at")
        return self


def _seal(order: EntryOrder, signal: Signal, risk_amount: Decimal, guards: FillGuards) -> bytes:
    payload = "".join(
        (order.model_dump_json(), signal.model_dump_json(), str(risk_amount), guards.model_dump_json())
    )
    return hmac.new(_KEY, payload.encode(), hashlib.sha256).digest()


class ApprovedEntry:
    """Entrada aprovada pela gestao de risco. So `evaluate_entry` a produz.

    Imutavel e sem construcao publica. Como `object.__new__` contorna o `__init__`, a emissao carrega um
    selo HMAC (chave aleatoria por processo) sobre ordem, sinal e risco: o executor DEVE chamar `verify()`
    (ou `verify_approval`) imediatamente antes de enviar a ordem e recusar qualquer excecao `ApprovalError`.
    """

    __slots__ = ("_seal", "guards", "order", "risk_amount", "signal")

    order: EntryOrder
    signal: Signal
    risk_amount: Decimal
    guards: FillGuards

    def __init__(
        self,
        order: EntryOrder,
        signal: Signal,
        risk_amount: Decimal,
        *,
        guards: FillGuards | None = None,
        _issuer: object = None,
    ) -> None:
        if _issuer is not _ISSUER or guards is None:
            raise TypeError("ApprovedEntry can only be issued by the risk gate")
        object.__setattr__(self, "order", order)
        object.__setattr__(self, "signal", signal)
        object.__setattr__(self, "risk_amount", risk_amount)
        object.__setattr__(self, "guards", guards)
        object.__setattr__(self, "_seal", _seal(order, signal, risk_amount, guards))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("ApprovedEntry is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("ApprovedEntry is immutable")

    def verify(self) -> Self:
        try:
            seal: bytes = object.__getattribute__(self, "_seal")
            expected = _seal(self.order, self.signal, self.risk_amount, self.guards)
            valid = hmac.compare_digest(seal, expected)
        except (AttributeError, TypeError, ValueError) as error:
            raise ApprovalError("approval is not authentic") from error
        if not valid:
            raise ApprovalError("approval is not authentic")
        return self

    def is_expired(self, at: datetime) -> bool:
        return at > self.guards.expires_at

    def to_decision(self) -> Decision:
        return Decision(
            decided_at=self.order.created_at,
            symbol=self.order.symbol,
            action=DecisionAction.ENTER,
            reason=f"aprovado pelo risco: qty {self.order.quantity}, risco {self.risk_amount}",
            source="risk",
            signal=self.signal,
        )


def verify_approval(candidate: object) -> ApprovedEntry:
    """Ponto de verificacao do executor: aceita apenas `ApprovedEntry` autentico emitido pelo gate."""
    if not isinstance(candidate, ApprovedEntry):
        raise ApprovalError("only ApprovedEntry issued by the risk gate is accepted")
    return candidate.verify()
