"""Registro del consumo (modelo y transcripción) para controlar y mostrar el costo del bot."""
from __future__ import annotations

from datetime import date
from typing import Any

from . import config

TRANSCRIBE_MIN_SECONDS = 15  # Amazon Transcribe cobra mínimo 15 s por audio


def _month(profile_stats: dict[str, Any], today: date) -> dict[str, Any]:
    months = profile_stats.setdefault("consumo", {})
    key = today.strftime("%Y-%m")
    for old in sorted(months)[:-5]:  # conservar solo los últimos meses
        months.pop(old, None)
    return months.setdefault(key, {"llm_calls": 0, "fast": 0, "input": 0, "output": 0,
                                   "cache_read": 0, "cache_write": 0, "audios": 0, "audio_seconds": 0})


def llm_allowed(stats: dict[str, Any], today: date) -> bool:
    daily = stats.get("llm_daily") or {}
    return daily.get("date") != today.isoformat() or daily.get("count", 0) < config.DAILY_LLM_LIMIT


def add_llm(stats: dict[str, Any], today: date, tokens: dict[str, int]) -> None:
    m = _month(stats, today)
    m["llm_calls"] += 1
    for key in ("input", "output", "cache_read", "cache_write"):
        m[key] += int(tokens.get(key, 0))
    daily = stats.get("llm_daily") or {}
    if daily.get("date") != today.isoformat():
        daily = {"date": today.isoformat(), "count": 0}
    daily["count"] += 1
    stats["llm_daily"] = daily


def add_fast(stats: dict[str, Any], today: date) -> None:
    _month(stats, today)["fast"] += 1


def add_audio(stats: dict[str, Any], today: date, seconds: int) -> None:
    m = _month(stats, today)
    m["audios"] += 1
    m["audio_seconds"] += max(TRANSCRIBE_MIN_SECONDS, int(seconds))


def estimate(month: dict[str, Any]) -> dict[str, float]:
    pin, pout = config.PRICE_INPUT_MTOK / 1e6, config.PRICE_OUTPUT_MTOK / 1e6
    llm = (month.get("input", 0) * pin + month.get("cache_write", 0) * pin * 1.25
           + month.get("cache_read", 0) * pin * 0.1 + month.get("output", 0) * pout)
    audio = month.get("audio_seconds", 0) / 60 * config.PRICE_TRANSCRIBE_MIN
    return {"llm": round(llm, 4), "audio": round(audio, 4), "total": round(llm + audio, 4)}


def usage_text(stats: dict[str, Any], today: date) -> str:
    m = (stats.get("consumo") or {}).get(today.strftime("%Y-%m"))
    if not m:
        return "Este mes todavía no has gastado nada en el bot 🙂"
    cost = estimate(m)
    n = lambda v: f"{v:,}".replace(",", ".")  # noqa: E731 - miles con punto
    return "\n".join([
        f"<b>💸 Consumo del bot en {today.strftime('%m/%Y')}</b> (estimado, USD)",
        f"Mensajes con IA: {m['llm_calls']} · registros rápidos sin IA: {m['fast']}",
        f"Tokens: {n(m['input'] + m['cache_read'] + m['cache_write'])} entrada · {n(m['output'])} salida",
        f"Audios: {m['audios']} ({m['audio_seconds'] / 60:.1f} min facturables)",
        "",
        f"IA: ${cost['llm']:.3f} · Transcripción: ${cost['audio']:.3f}",
        f"<b>Total: ${cost['total']:.2f}</b>",
        "<i>No incluye Lambda/DynamoDB/S3, que con este uso suelen quedar en la capa gratuita.</i>",
    ])
