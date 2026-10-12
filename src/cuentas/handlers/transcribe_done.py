"""EventBridge -> fin de un job de Amazon Transcribe: lee el texto y lo pasa al asistente."""
from __future__ import annotations

import html
import json
import logging
from typing import Any

import boto3

from .. import bot, config
from ..repository import Repository
from ..telegram import Telegram

log = logging.getLogger()
log.setLevel(logging.INFO)
s3 = boto3.client("s3")

_repo: Repository | None = None
_tg: Telegram | None = None


def parse_job_name(name: str) -> tuple[int, int, int, int]:
    """cuentas_<user>_<chat>_<message>_<update> -> ids (chat puede ser negativo)."""
    prefix, user_id, chat_id, message_id, update_id = name.split("_")
    if prefix != "cuentas":
        raise ValueError(name)
    return int(user_id), int(chat_id), int(message_id), int(update_id)


def handler(event: dict[str, Any], _context: Any) -> None:
    global _repo, _tg
    if _repo is None:
        _repo, _tg = Repository(), Telegram()
    repo, tg = _repo, _tg
    assert tg is not None

    detail = event.get("detail", {})
    job_name = detail.get("TranscriptionJobName", "")
    status = detail.get("TranscriptionJobStatus")
    try:
        user_id, chat_id, message_id, _ = parse_job_name(job_name)
    except ValueError:
        log.info("Job ajeno al bot: %s", job_name)
        return
    if not repo.claim_alert(user_id, f"job:{job_name}", ttl_days=2):
        return  # EventBridge entrega al menos una vez

    transcript_key = f"transcripts/{job_name}.json"
    try:
        if status != "COMPLETED":
            tg.send(chat_id, "No pude entender el audio 😕. ¿Me lo escribes o lo grabas de nuevo?", reply_to=message_id)
            return
        body = json.loads(s3.get_object(Bucket=config.AUDIO_BUCKET, Key=transcript_key)["Body"].read())
        text = " ".join(t["transcript"] for t in body["results"]["transcripts"]).strip()
        if not text:
            tg.send(chat_id, "El audio llegó en silencio 🤫. ¿Lo intentas otra vez?", reply_to=message_id)
            return
        profile = repo.get_profile(user_id)
        if profile is None:
            return
        tg.send(chat_id, f"🗣️ <i>{html.escape(text)}</i>", reply_to=message_id)
        bot.handle_conversation(tg, repo, profile, chat_id, text, source="voice")
    except Exception:  # noqa: BLE001
        log.exception("Error procesando transcripción %s", job_name)
        tg.send(chat_id, "😕 Tuve un problema procesando tu audio. ¿Lo intentas de nuevo?")
    finally:
        _cleanup(job_name, transcript_key)


def _cleanup(job_name: str, transcript_key: str) -> None:
    # Los audios son datos personales: se borran apenas se procesan (y hay lifecycle de respaldo).
    listing = s3.list_objects_v2(Bucket=config.AUDIO_BUCKET, Prefix=f"audio/{job_name}.")
    keys = [{"Key": o["Key"]} for o in listing.get("Contents", [])] + [{"Key": transcript_key}]
    s3.delete_objects(Bucket=config.AUDIO_BUCKET, Delete={"Objects": keys, "Quiet": True})
    try:
        boto3.client("transcribe").delete_transcription_job(TranscriptionJobName=job_name)
    except Exception:  # noqa: BLE001
        log.warning("No se pudo borrar el job %s", job_name, exc_info=True)
