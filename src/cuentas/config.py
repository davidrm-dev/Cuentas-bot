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
MODEL_ID = os.environ.get("MODEL_ID", "anthropic.claude-opus-5-5")
FALLBACK_MODEL_ID = os.environ.get("FALLBACK_MODEL_ID", "anthropic.claude-opus-4-8")
LLM_EFFORT = os.environ.get("LLM_EFFORT", "medium")

DEFAULT_CURRENCY = os.environ.get("DEFAULT_CURRENCY", "USD")
DEFAULT_TIMEZONE = os.environ.get("DEFAULT_TIMEZONE", "America/Bogota")
TRANSCRIBE_LANGUAGE = os.environ.get("TRANSCRIBE_LANGUAGE", "es-US")

# Historial conversacional que se le da al modelo como contexto.
HISTORY_TURNS = int(os.environ.get("HISTORY_TURNS", "6"))


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
