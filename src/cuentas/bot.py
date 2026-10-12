"""Orquestación de mensajes de Telegram: comandos, texto, botones y alertas."""
from __future__ import annotations

import html
import logging
from typing import Any

from . import assistant, categories, config, finance, learning, parsing, usage
from .finance import Alert, fmt
from .ledger import Ledger
from .models import Profile, Transaction
from .repository import Repository
from .telegram import Telegram, webapp_button

log = logging.getLogger(__name__)

HELP = (
    "<b>Soy tu contador personal</b> 🧾\n\n"
    "Mándame audios o mensajes como:\n"
    "• <i>Gasté 25 mil en el súper</i>\n"
    "• <i>Me pagaron el sueldo, 3 millones</i>\n"
    "• <i>Tengo Netflix de 45 mil el 5 de cada mes</i>\n"
    "• <i>¿Cuánto llevo en restaurantes este mes?</i>\n"
    "• <i>¿Debería salir hoy?</i>\n"
    "• <i>Ponme un tope de 400 mil en ocio</i>\n\n"
    "Comandos: /resumen /suscripciones /dashboard /deshacer /consumo /ayuda"
)


def ensure_profile(repo: Repository, user: dict[str, Any], chat_id: int) -> tuple[Profile, bool]:
    profile = repo.get_profile(user["id"])
    if profile:
        if profile.chat_id != chat_id and chat_id > 0:
            profile.chat_id = chat_id
            repo.save_profile(profile)
        return profile, False
    profile = Profile(user_id=user["id"], chat_id=chat_id, first_name=user.get("first_name", ""))
    repo.save_profile(profile)
    return profile, True


def tx_keyboard(tx: Transaction) -> list[list[dict[str, Any]]]:
    ref = f"{tx.date}:{tx.id}"
    return [[{"text": "🏷️ Cambiar categoría", "callback_data": f"c:{ref}"},
             {"text": "↩️ Deshacer", "callback_data": f"u:{ref}"}]]


def category_keyboard(tx: Transaction) -> list[list[dict[str, Any]]]:
    ref = f"{tx.date}:{tx.id}"
    options = categories.INCOME_CATEGORIES if tx.kind == "income" else categories.CATEGORIES
    buttons = [{"text": categories.label(c), "callback_data": f"s:{ref}:{c}"} for c in options if c != tx.category]
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


def send_alerts(tg: Telegram, chat_id: int, alerts: list[Alert]) -> None:
    for alert in sorted(alerts, key=lambda a: a.level != "critical"):
        icon = "🚨 <b>Gasto crítico</b>" if alert.level == "critical" else "⚠️ <b>Atención</b>"
        tg.send(chat_id, f"{icon}\n{html.escape(alert.message)}",
                keyboard=webapp_button("Ver detalle") if alert.level == "critical" else None)


def handle_conversation(tg: Telegram, repo: Repository, profile: Profile, chat_id: int, text: str,
                        source: str = "text", reply_to: int | None = None) -> None:
    """Pasa el mensaje (escrito o transcrito) al asistente y envía su respuesta."""
    learning.observe_input(profile, "voice" if source == "voice" else "text")
    ledger = Ledger(repo, profile)
    if config.FAST_PATH and fast_record(tg, ledger, chat_id, text, source, reply_to):
        repo.save_profile(profile)
        return
    if not usage.llm_allowed(profile.stats, ledger.today):
        repo.save_profile(profile)
        tg.send(chat_id, f"Llegaste al límite diario de {config.DAILY_LLM_LIMIT} consultas con IA 🛑 "
                         "(es para cuidar tus costos). Mañana sigo; mientras tanto puedo registrar frases "
                         "simples como <i>gasté 12 mil en uber</i>.", reply_to=reply_to)
        return
    tg.typing(chat_id)
    history = repo.recent_messages(profile.user_id, config.HISTORY_TURNS * 2)
    result = assistant.reply(ledger, text, source=source, history=history)
    if any(result.tokens.values()):
        usage.add_llm(profile.stats, ledger.today, result.tokens)
    repo.save_profile(profile)
    keyboard = tx_keyboard(result.recorded[-1]) if len(result.recorded) == 1 else None
    tg.send(chat_id, result.text, keyboard=keyboard, reply_to=reply_to)
    send_alerts(tg, chat_id, result.alerts)
    repo.add_message(profile.user_id, "user", text)
    repo.add_message(profile.user_id, "assistant", result.text)


def fast_record(tg: Telegram, ledger: Ledger, chat_id: int, text: str, source: str,
                reply_to: int | None) -> bool:
    """Registra sin IA las frases inequívocas. Devuelve False si hace falta el modelo."""
    profile = ledger.profile
    entry = parsing.confident_parse(text, profile.merchant_categories)
    if entry is None:
        return False
    merchant = entry.description if len(entry.description.split()) <= 3 else ""
    tx = Transaction(user_id=profile.user_id, kind=entry.kind, amount=entry.amount, category=entry.category,
                     description=entry.description.capitalize(), merchant=merchant, date=ledger.today.isoformat(),
                     necessity=categories.default_necessity(entry.category), source=source)
    alerts = ledger.record(tx)
    usage.add_fast(profile.stats, ledger.today)
    verb = "Ingreso" if tx.kind == "income" else "Gasto"
    tg.send(chat_id, f"✅ {verb}: <b>{fmt(tx.amount, profile.currency)}</b> · {categories.label(tx.category)}\n"
                     f"<i>{html.escape(tx.description)}</i>", keyboard=tx_keyboard(tx), reply_to=reply_to)
    send_alerts(tg, chat_id, alerts)
    ledger.repo.add_message(profile.user_id, "user", text)
    ledger.repo.add_message(profile.user_id, "assistant", f"Registré {verb.lower()} de {tx.amount} en {tx.category}")
    return True


def handle_command(tg: Telegram, repo: Repository, profile: Profile, chat_id: int, command: str,
                   is_new: bool) -> bool:
    """Devuelve True si el texto era un comando conocido."""
    cmd = command.split()[0].split("@")[0].lower()
    ledger = Ledger(repo, profile)
    if cmd == "/start":
        if config.WEBAPP_URL and chat_id > 0:
            try:
                tg.set_menu_button(chat_id, config.WEBAPP_URL)
            except Exception:  # noqa: BLE001 - el botón de menú es opcional
                log.warning("No se pudo fijar el botón de menú", exc_info=True)
        name = html.escape(profile.first_name or "")
        intro = f"¡Hola {name}! 👋\n\n" if is_new else f"¡Hola de nuevo {name}!\n\n"
        tg.send(chat_id, intro + HELP + "\n\nPara empezar, cuéntame: <b>¿cuánto ganas al mes y qué día te pagan?</b>",
                keyboard=webapp_button())
        return True
    if cmd in ("/ayuda", "/help"):
        tg.send(chat_id, HELP)
        return True
    if cmd == "/resumen":
        subs = repo.list_subscriptions(profile.user_id)
        tg.send(chat_id, finance.summary_text(profile, ledger.month_transactions(), subs, ledger.today),
                keyboard=webapp_button("📊 Ver dashboard completo"))
        return True
    if cmd == "/consumo":
        tg.send(chat_id, usage.usage_text(profile.stats, ledger.today))
        return True
    if cmd == "/dashboard":
        tg.send(chat_id, "Tu dashboard con gráficas, presupuestos y movimientos:", keyboard=webapp_button())
        return True
    if cmd == "/suscripciones":
        subs = repo.list_subscriptions(profile.user_id)
        if not subs:
            tg.send(chat_id, "No tienes suscripciones registradas. Dime algo como "
                             "<i>pago Spotify 17 mil el 10 de cada mes</i>.")
            return True
        cur = profile.currency
        lines = [f"• <b>{html.escape(s.name)}</b>: {fmt(s.amount, cur)} ({s.cycle}) — próximo {s.next_charge}"
                 for s in sorted(subs, key=lambda s: s.next_charge)]
        lines.append(f"\nCosto fijo mensual: <b>{fmt(finance.monthly_fixed(subs), cur)}</b>")
        tg.send(chat_id, "🔁 <b>Suscripciones</b>\n" + "\n".join(lines))
        return True
    if cmd == "/deshacer":
        tx = repo.last_transaction(profile.user_id)
        if tx is None:
            tg.send(chat_id, "No hay nada que deshacer.")
        else:
            ledger.delete(tx)
            tg.send(chat_id, f"↩️ Eliminado: {html.escape(tx.description)} ({fmt(tx.amount, profile.currency)})")
        return True
    return False


def handle_callback(tg: Telegram, repo: Repository, callback: dict[str, Any]) -> None:
    data = callback.get("data", "")
    message = callback.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    profile = repo.get_profile(callback["from"]["id"])
    parts = data.split(":")
    if profile is None or len(parts) < 3:
        tg.answer_callback(callback["id"], "Acción no válida")
        return
    action, tx_date, tx_id = parts[0], parts[1], parts[2]
    tx = repo.get_transaction(profile.user_id, tx_date, tx_id)
    if tx is None:
        tg.answer_callback(callback["id"], "Ese movimiento ya no existe")
        return
    ledger = Ledger(repo, profile)
    cur = profile.currency
    if action == "u":
        ledger.delete(tx)
        tg.answer_callback(callback["id"], "Eliminado")
        tg.edit(chat_id, message_id, f"↩️ <s>{html.escape(tx.description)} — {fmt(tx.amount, cur)}</s> (eliminado)")
    elif action == "c":
        tg.answer_callback(callback["id"])
        tg.edit(chat_id, message_id,
                f"¿En qué categoría va <b>{html.escape(tx.description)}</b> ({fmt(tx.amount, cur)})?",
                keyboard=category_keyboard(tx))
    elif action == "s" and len(parts) == 4 and categories.is_valid(parts[3]):
        ledger.recategorize(tx, parts[3])
        tg.answer_callback(callback["id"], "Categoría actualizada; lo tendré en cuenta")
        tg.edit(chat_id, message_id,
                f"✅ {html.escape(tx.description)} — {fmt(tx.amount, cur)} → {categories.label(tx.category)}\n"
                f"<i>Aprendido: la próxima vez lo clasifico así.</i>",
                keyboard=tx_keyboard(tx))
    else:
        tg.answer_callback(callback["id"], "Acción no válida")
