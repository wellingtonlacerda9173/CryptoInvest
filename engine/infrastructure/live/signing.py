import hashlib
import hmac
from collections.abc import Mapping
from urllib.parse import urlencode


def sign_query(query: str, secret: str) -> str:
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


def signed_payload(params: Mapping[str, object], secret: str) -> str:
    """Query/corpo urlencoded com `signature` por ultimo, assinando exatamente os bytes enviados."""
    query = urlencode([(k, str(v)) for k, v in params.items()])
    return f"{query}&signature={sign_query(query, secret)}"
