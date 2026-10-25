import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ApiCredentials:
    """Chave e segredo de UM ambiente (`mainnet`/`testnet`/`local_fake`); `repr`/`str` nunca os expoem."""

    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    environment: str

    def __post_init__(self) -> None:
        if not self.api_key.strip() or not self.api_secret.strip():
            raise ValueError("credenciais vazias")
        if self.environment not in ("mainnet", "testnet", "local_fake"):
            raise ValueError("ambiente das credenciais invalido")

    def __repr__(self) -> str:
        return f"ApiCredentials(***, environment={self.environment})"

    __str__ = __repr__

    @classmethod
    def from_secret_file(cls, path: Path) -> "ApiCredentials":
        """Arquivo secreto JSON `{"api_key": ..., "api_secret": ..., "environment": ...}` (Docker secrets)."""
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            api_key=str(data["api_key"]),
            api_secret=str(data["api_secret"]),
            environment=str(data["environment"]),
        )
