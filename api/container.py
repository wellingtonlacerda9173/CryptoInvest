from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.binance_keys import ExchangeKeyVerifier
from api.clock import Clock
from api.config import ApiSettings
from api.security.crypto import KeyRing
from api.security.hashgate import HashGate
from api.security.passwords import PasswordService
from api.security.throttle import AuditSampler, FailureThrottle, LockEpochs, UserPressure
from api.security.tokens import username_tag

if TYPE_CHECKING:
    from api.runner_link import RunnerLink
    from api.services.backtests import BacktestRunner
    from api.services.launcher import RunnerLauncher
    from api.services.realtime import RealtimeHub
    from api.services.ticker import TickerService


@dataclass(slots=True)
class Container:
    """Dependencias de processo (substituiveis nos testes): nada aqui e global."""

    settings: ApiSettings
    sessions: async_sessionmaker[AsyncSession]
    passwords: PasswordService
    keyring: KeyRing
    verifier: ExchangeKeyVerifier
    clock: Clock
    ip_throttle: FailureThrottle
    # Bloqueio duro por par (usuario, IP): o nome historico `user_throttle` e mantido
    user_throttle: FailureThrottle
    link: "RunnerLink | None" = None
    hub: "RealtimeHub | None" = None
    launcher: "RunnerLauncher | None" = None
    backtests: "BacktestRunner | None" = None
    ticker: "TickerService | None" = None
    command_key: bytes | None = None
    gate_cache: dict[str, tuple[float, Any]] = field(default_factory=dict)
    user_pressure: UserPressure = field(init=False)
    hash_gate: HashGate = field(init=False)
    lock_epochs: LockEpochs = field(init=False)
    audit_sampler: AuditSampler = field(default_factory=AuditSampler)

    def __post_init__(self) -> None:
        s = self.settings
        self.user_pressure = UserPressure(
            base_seconds=s.login_user_delay_base_seconds, cap_seconds=s.login_user_delay_cap_seconds
        )
        self.lock_epochs = LockEpochs(recheck_seconds=s.unlock_recheck_seconds)
        self.hash_gate = HashGate(s.login_max_concurrent_hashes, s.login_hash_queue_timeout_seconds)

    @property
    def session_secret(self) -> str:
        return self.settings.session_secret.get_secret_value()

    def user_tag(self, username: str) -> str:
        return username_tag(self.session_secret, username)

    @property
    def recovery_secret(self) -> str:
        key = self.settings.recovery_hmac_key
        return key.get_secret_value() if key is not None else self.session_secret

    @property
    def recovery_legacy_secret(self) -> str | None:
        """Segredo antigo (session_secret), aceito na migracao dos codigos de recuperacao ja emitidos."""
        legacy = self.settings.recovery_accept_legacy_hashes
        return self.session_secret if legacy and self.recovery_secret != self.session_secret else None
