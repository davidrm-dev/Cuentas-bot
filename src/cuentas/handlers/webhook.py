"""Recibe el webhook de Telegram y encola el update (responde rápido; el trabajo va en processor)."""
from __future__ import annotations

import hmac
import json
import logging
from typing import Any

import boto3

from .. import config

log = logging.getLogger()
log.setLevel(logging.INFO)
sqs = boto3.client("sqs")


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    token = headers.get("x-telegram-bot-api-secret-token", "")
    if not hmac.compare_digest(token, config.webhook_secret()):
        return {"statusCode": 401, "body": "unauthorized"}
    try:
        update = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return {"statusCode": 400, "body": "bad request"}
    if "update_id" not in update:
        return {"statusCode": 200, "body": "ignored"}

    source = update.get("message") or update.get("edited_message") or update.get("callback_query") or {}
    group = (source.get("chat") or source.get("message", {}).get("chat") or source.get("from") or {}).get("id", 0)
    sqs.send_message(
        QueueUrl=config.UPDATES_QUEUE_URL,
        MessageBody=json.dumps(update),
        MessageGroupId=str(group),  # orden garantizado por chat
        MessageDeduplicationId=str(update["update_id"]),  # Telegram reintenta: evitamos duplicados
    )
    return {"statusCode": 200, "body": "ok"}
