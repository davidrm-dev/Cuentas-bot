"""Procesa updates de Telegram desde SQS: texto, comandos, botones y audios."""
from __future__ import annotations

import json
import logging
from typing import Any

import boto3

from .. import bot, config
from ..repository import Repository
from ..telegram import Telegram

log = logging.getLogger()
log.setLevel(logging.INFO)

MAX_AUDIO_SECONDS = 300
MEDIA_FORMATS = {
    "audio/ogg": "ogg", "audio/opus": "ogg", "audio/mpeg": "mp3", "audio/mp3": "mp3",
    "audio/mp4": "mp4", "audio/x-m4a": "mp4", "audio/m4a": "mp4", "video/mp4": "mp4",
    "audio/wav": "wav", "audio/x-wav": "wav", "audio/flac": "flac", "audio/webm": "webm",
}

_repo: Repository | None = None
_tg: Telegram | None = None


def _deps() -> tuple[Repository, Telegram]:
    global _repo, _tg
    if _repo is None:
        _repo, _tg = Repository(), Telegram()
    return _repo, _tg  # type: ignore[return-value]


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    repo, tg = _deps()
    for record in event.get("Records", []):
        update = json.loads(record["body"])
        try:
            process_update(repo, tg, update)
        except Exception:  # noqa: BLE001
            # No reintentamos: un reintento podría registrar dos veces el mismo gasto.
            log.exception("Error procesando update %s", update.get("update_id"))
            chat_id = _chat_id(update)
            if chat_id:
                try:
                    tg.send(chat_id, "😕 Tuve un problema procesando tu mensaje. ¿Lo intentas de nuevo?")
                except Exception:  # noqa: BLE001
                    log.exception("Tampoco se pudo avisar al usuario")
    return {"batchItemFailures": []}


def _chat_id(update: dict[str, Any]) -> int | None:
    msg = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
    return (msg.get("chat") or {}).get("id")


def process_update(repo: Repository, tg: Telegram, update: dict[str, Any]) -> None:
    if "callback_query" in update:
        bot.handle_callback(tg, repo, update["callback_query"])
        return
    message = update.get("message")
    if not message or "from" not in message:
        return
    chat_id = message["chat"]["id"]
    if message["chat"].get("type") != "private":
        tg.send(chat_id, "Por privacidad solo llevo cuentas en chats privados 🙂")
        return
    profile, is_new = bot.ensure_profile(repo, message["from"], chat_id)

    text = message.get("text") or message.get("caption")
    if text and text.startswith("/"):
        if bot.handle_command(tg, repo, profile, chat_id, text, is_new):
            return
    if text:
        bot.handle_conversation(tg, repo, profile, chat_id, text, source="text")
        return

    for kind, default_format in (("voice", "ogg"), ("audio", ""), ("video_note", "mp4")):
        if kind in message:
            media = message[kind]
            media_format = MEDIA_FORMATS.get(media.get("mime_type", ""), default_format)
            start_transcription(tg, profile.user_id, chat_id, message["message_id"], update["update_id"],
                                media, media_format)
            return
    if is_new:
        bot.handle_command(tg, repo, profile, chat_id, "/start", is_new)
    else:
        tg.send(chat_id, "Puedo leer texto y escuchar audios. Cuéntame un gasto o pregúntame algo 🙂")


def start_transcription(tg: Telegram, user_id: int, chat_id: int, message_id: int, update_id: int,
                        media: dict[str, Any], media_format: str) -> None:
    if media.get("duration", 0) > MAX_AUDIO_SECONDS:
        tg.send(chat_id, "Ese audio es muy largo 😅. Mándame audios de menos de 5 minutos.")
        return
    if not media_format:
        tg.send(chat_id, "No puedo escuchar ese formato de audio. Prueba con una nota de voz.")
        return
    tg.typing(chat_id, "record_voice")
    audio = tg.download_file(media["file_id"])
    job_name = f"cuentas_{user_id}_{chat_id}_{message_id}_{update_id}"
    key = f"audio/{job_name}.{media_format}"
    boto3.client("s3").put_object(Bucket=config.AUDIO_BUCKET, Key=key, Body=audio)
    # Al terminar, EventBridge dispara transcribe_done con el nombre del job.
    boto3.client("transcribe").start_transcription_job(
        TranscriptionJobName=job_name,
        LanguageCode=config.TRANSCRIBE_LANGUAGE,
        MediaFormat=media_format,
        Media={"MediaFileUri": f"s3://{config.AUDIO_BUCKET}/{key}"},
        OutputBucketName=config.AUDIO_BUCKET,
        OutputKey=f"transcripts/{job_name}.json",
    )
