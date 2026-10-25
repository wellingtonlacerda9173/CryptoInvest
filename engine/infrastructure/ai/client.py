"""Transporte para o modelo. Sem ferramentas, sem ordens: so texto de entrada e texto de saida.

O SDK `anthropic` (extra `ai`) so e importado quando a IA esta habilitada. A API ja nao aceita `temperature`
(removida do SDK e recusada pelos modelos atuais), entao o determinismo vem de esforco baixo, `max_tokens`
curto e do cache por hash do pedido.
"""

from dataclasses import dataclass
from typing import Any, Protocol

import structlog
from pydantic import SecretStr

MAX_OUTPUT_TOKENS = 1024
log = structlog.get_logger()


def _mentions_effort(error: Exception) -> bool:
    text = str(error).lower()
    return "output_config" in text or "effort" in text


@dataclass(frozen=True, slots=True)
class LlmReply:
    text: str
    input_tokens: int
    output_tokens: int


class LlmError(Exception):
    def __init__(self, kind: str, *, retryable: bool) -> None:
        super().__init__(kind)
        self.retryable = retryable


class LlmTimeoutError(LlmError):
    def __init__(self) -> None:
        super().__init__("timeout", retryable=True)


class LlmClient(Protocol):
    async def complete(
        self, *, model: str, system: str, user: str, max_tokens: int, deadline_seconds: float
    ) -> LlmReply: ...


class AnthropicClient:
    def __init__(self, api_key: SecretStr) -> None:
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.AsyncAnthropic(api_key=api_key.get_secret_value(), max_retries=0)
        self._without_effort: set[str] = set()
        self.notes: dict[str, str] = {}

    async def complete(
        self, *, model: str, system: str, user: str, max_tokens: int, deadline_seconds: float
    ) -> LlmReply:
        a = self._anthropic
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "timeout": deadline_seconds,
        }
        if model not in self._without_effort:
            params["output_config"] = {"effort": "low"}
        try:
            try:
                message = await self._client.messages.create(**params)
            except a.BadRequestError as error:
                if "output_config" not in params or not _mentions_effort(error):
                    raise
                # o modelo nao aceita `effort`: reenvia UMA vez sem o parametro e lembra por modelo
                self._without_effort.add(model)
                self.notes[model] = "parametro output_config/effort nao suportado: enviado sem ele"
                log.warning("ai_effort_unsupported", model=model, effect="reenviado sem output_config")
                del params["output_config"]
                message = await self._client.messages.create(**params)
        except a.APITimeoutError as error:
            raise LlmTimeoutError from error
        except (a.RateLimitError, a.InternalServerError, a.APIConnectionError) as error:
            raise LlmError(type(error).__name__, retryable=True) from None
        except a.APIError as error:
            raise LlmError(type(error).__name__, retryable=False) from None
        text = "".join(block.text for block in message.content if block.type == "text")
        return LlmReply(text, message.usage.input_tokens, message.usage.output_tokens)

    async def aclose(self) -> None:
        await self._client.close()
