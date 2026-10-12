"""Configuración leída de variables de entorno (definidas en template.yaml)."""
from __future__ import annotations

import os
from functools import lru_cache

TABLE_NAME = os.environ.get("TABLE_NAME", "cuentas-bot")
AUDIO_BUCKET = os.environ.get("AUDIO_BUCKET", "")
UPDATES_QUEUE_URL = os.environ.get("UPDATES_QUEUE_URL", "")
BOT_TOKEN_PARAM = os.environ.get("BOT_TOKEN_PARAM", "/cuentas-bot/telegram-token")
WEBHOOK_SECRET_PARAM = os.environ.get("WEBHOOK_SECRET_PARAM", "/cuentas-bot/webhook-secret")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", AWS_REGION)
# Haiku 5.5 por costo: ~40 veces más barato que Opus y suficiente para registrar y clasificar gastos.
MODEL_ID = os.environ.get("MODEL_ID", "anthropic.claude-haiku-5-5")
FALLBACK_MODEL_ID = os.environ.get("FALLBACK_MODEL_ID", "anthropic.claude-sonnet-5-5")
LLM_EFFORT = os.environ.get("LLM_EFFORT", "low")
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "1500"))
# Precios por millón de tokens, solo para estimar el consumo en /consumo (revisa la tarifa de Bedrock).
PRICE_INPUT_MTOK = float(os.environ.get("PRICE_INPUT_MTOK", "0.10"))
PRICE_OUTPUT_MTOK = float(os.environ.get("PRICE_OUTPUT_MTOK", "0.50"))
PRICE_TRANSCRIBE_MIN = float(os.environ.get("PRICE_TRANSCRIBE_MIN", "0.024"))

# Control de gasto
# Solo estos usuarios de Telegram pueden usar el bot (vacío = cualquiera). Evita que extraños gasten tu cuenta.
ALLOWED_USER_IDS = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x}
# Máximo de mensajes por usuario y día que pasan por el modelo (el registro rápido sin modelo no cuenta).
DAILY_LLM_LIMIT = int(os.environ.get("DAILY_LLM_LIMIT", "80"))
# Registrar frases simples ("gasté 12 mil en uber") sin llamar al modelo.
FAST_PATH = os.environ.get("FAST_PATH", "true").lower() in ("1", "true", "yes")

DEFAULT_CURRENCY = os.environ.get("DEFAULT_CURRENCY", "USD")
DEFAULT_TIMEZONE = os.environ.get("DEFAULT_TIMEZONE", "America/Bogota")
TRANSCRIBE_LANGUAGE = os.environ.get("TRANSCRIBE_LANGUAGE", "es-US")

# Historial conversacional que se le da al modelo como contexto.
HISTORY_TURNS = int(os.environ.get("HISTORY_TURNS", "4"))


@lru_cache(maxsize=None)
def secret(param_name: str) -> str:
    """Lee un SecureString de SSM Parameter Store (cacheado por contenedor)."""
    env_override = os.environ.get(param_name.strip("/").replace("/", "_").replace("-", "_").upper())
    if env_override:
        return env_override
    import boto3

    ssm = boto3.client("ssm")
    return ssm.get_parameter(Name=param_name, WithDecryption=True)["Parameter"]["Value"]


def bot_token() -> str:
    return secret(BOT_TOKEN_PARAM)


def webhook_secret() -> str:
    return secret(WEBHOOK_SECRET_PARAM)
