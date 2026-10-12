"""Validación del initData de Telegram Mini Apps.

https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import parse_qsl


class InvalidInitData(ValueError):
    pass


def validate_init_data(init_data: str, bot_token: str, max_age_seconds: int = 24 * 3600,
                       now: float | None = None) -> dict[str, Any]:
    """Devuelve el usuario de Telegram si la firma es válida; si no, lanza InvalidInitData."""
    if not init_data:
        raise InvalidInitData("initData vacío")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise InvalidInitData("Falta hash")
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise InvalidInitData("Firma inválida")
    auth_date = int(pairs.get("auth_date", "0"))
    if (now or time.time()) - auth_date > max_age_seconds:
        raise InvalidInitData("initData expirado")
    try:
        user = json.loads(pairs["user"])
    except (KeyError, json.JSONDecodeError):
        raise InvalidInitData("Falta usuario")
    return user
