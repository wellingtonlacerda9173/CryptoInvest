class LiveError(Exception):
    """Base dos erros do gateway Live."""


class BinanceApiError(LiveError):
    """Resposta de erro da corretora (a requisicao foi processada e RECUSADA)."""

    def __init__(self, status: int, code: int, message: str, retry_after: float = 0.0) -> None:
        super().__init__(f"binance {status} code={code}: {message}")
        self.retry_after = retry_after
        self.status = status
        self.code = code
        self.message = message


class OutcomeUnknownError(LiveError):
    """Timeout, queda de conexao ou 5xx: a ordem pode ou nao ter sido aceita. Consultar antes de reenviar."""


class ReadOnlyModeError(LiveError):
    """Modo somente leitura (dry-run): nenhuma requisicao que altere estado sai."""


class ForbiddenEndpointError(LiveError):
    """Apenas /api/v3 (spot) e permitido: sem margem, futuros, saque ou transferencia."""


class IpBannedError(LiveError):
    """HTTP 418: IP banido pela corretora ate `until` (epoch segundos)."""

    def __init__(self, until: float) -> None:
        super().__init__(f"ip banned until {until:.0f}")
        self.until = until


class RateLimitedError(LiveError):
    """Orcamento de peso local esgotado ou 429 persistente."""


class ClockDriftError(LiveError):
    """Relogio local diverge do servidor alem do limite: requisicoes assinadas recusadas."""


class FilterViolationError(LiveError):
    """Ordem fora dos filtros do simbolo (LOT_SIZE, PRICE_FILTER, NOTIONAL, PERCENT_PRICE_BY_SIDE...)."""


class SymbolNotAllowedError(LiveError):
    """Simbolo fora da lista permitida ou nao negociavel em spot."""


class ProtectionError(LiveError):
    """Operacao recusada porque deixaria (ou deixou) a posicao sem protecao valida."""


class ProtectiveOrderCancelError(ProtectionError):
    """Cancelar OCO/stop fora de raise_stop, emergencia ou venda executada e proibido."""


class EntryNotApprovedError(LiveError):
    """Entrada sem selo valido do gate de risco."""


class TradingHaltedError(LiveError):
    """Gateway pausado (reconciliacao divergente ou falha critica): novas entradas recusadas."""


class InsufficientBalanceError(LiveError):
    """Saldo livre (limitado pelo capital Live) insuficiente."""


class OrderOutcomeUnknownError(LiveError):
    """Apos retries a corretora nao confirma se a ordem existe; ela fica pendente de resolucao."""
