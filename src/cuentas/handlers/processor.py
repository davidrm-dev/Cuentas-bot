"""Procesa updates de Telegram desde SQS: texto, comandos, botones y audios."""
from __future__ import annotations

import json
import logging
from typing import Any

import boto3

from .. import bot, config, finance, usage
from ..repository import Repository
from ..telegram import Telegram

log = logging.getLogger()
log.setLevel(logging.INFO)

MAX_AUDIO_SECONDS = 120  # audios largos = más costo de transcripción
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


def is_allowed(user_id: int) -> bool:
    return not config.ALLOWED_USER_IDS or user_id in config.ALLOWED_USER_IDS


def process_update(repo: Repository, tg: Telegram, update: dict[str, Any]) -> None:
    sender = (update.get("message") or update.get("callback_query") or {}).get("from") or {}
    if sender and not is_allowed(sender.get("id", 0)):
        # Bot privado: no se crea perfil ni se gasta en IA para desconocidos.
        log.info("Usuario no autorizado: %s", sender.get("id"))
        if "message" in update and update["message"].get("chat", {}).get("type") == "private":
            tg.send(update["message"]["chat"]["id"], "Este bot es privado 🔒")
        return
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

    image = _image_of(message)
    if image:
        if image.get("file_size", 0) > MAX_IMAGE_BYTES:
            tg.send(chat_id, "Esa imagen es muy pesada 😅. Mándala como foto normal (no como archivo).")
            return
        fmt = image.get("format", "jpeg")
        data = tg.download_file(image["file_id"])
        prompt = (message.get("caption") or "").strip() or \
            "Te envío un pantallazo de una app financiera. Léelo e importa el saldo y los movimientos."
        bot.handle_conversation(tg, repo, profile, chat_id, prompt, source="image",
                                reply_to=message["message_id"], image=(data, fmt))
        return

    text = message.get("text")
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
            if start_transcription(tg, profile.user_id, chat_id, message["message_id"], update["update_id"],
                                   media, media_format):
                usage.add_audio(profile.stats, finance.today_for(profile), media.get("duration", 0))
                repo.save_profile(profile)
            return
    if is_new:
        bot.handle_command(tg, repo, profile, chat_id, "/start", is_new)
    else:
        tg.send(chat_id, "Puedo leer texto y escuchar audios. Cuéntame un gasto o pregúntame algo 🙂")


MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_FORMATS = {"image/jpeg": "jpeg", "image/jpg": "jpeg", "image/png": "png", "image/webp": "webp"}


def _image_of(message: dict[str, Any]) -> dict[str, Any] | None:
    """La foto en mayor resolución, o una imagen enviada como archivo."""
    if message.get("photo"):
        return {**message["photo"][-1], "format": "jpeg"}
    doc = message.get("document") or {}
    if doc.get("mime_type") in IMAGE_FORMATS:
        return {**doc, "format": IMAGE_FORMATS[doc["mime_type"]]}
    return None


def start_transcription(tg: Telegram, user_id: int, chat_id: int, message_id: int, update_id: int,
                        media: dict[str, Any], media_format: str) -> bool:
    if media.get("duration", 0) > MAX_AUDIO_SECONDS:
        tg.send(chat_id, f"Ese audio es muy largo 😅. Mándame audios de menos de {MAX_AUDIO_SECONDS} segundos.")
        return False
    if not media_format:
        tg.send(chat_id, "No puedo escuchar ese formato de audio. Prueba con una nota de voz.")
        return False
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
    return True
