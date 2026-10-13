"""Orquestación de mensajes de Telegram: comandos, texto, botones y alertas."""
from __future__ import annotations

import html
import logging
import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from . import accounts, assistant, categories, config, finance, learning, parsing, usage
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
    "• <i>Me voy a pasar este mes: tengo un concierto de 250 mil y una boda. ¿Cuánto le pido a mis papás?</i>\n"
    "• <i>Ponme un tope de 400 mil en ocio</i>\n\n"
    "• <i>Le presté 50 mil a Juan, me paga el viernes</i> · <i>Le debo 200 mil a mi papá</i>\n"
    "• <i>Aparta 200 mil al mes para gasolina</i>\n\n"
    "• <i>Gasté 20 mil en almuerzo con Nequi</i> · <i>Tengo 300 mil en Daviplata</i>\n\n"
    "• <i>Ahorré 100 mil para el viaje en la cajita Nu</i> · <i>¿Cómo voy con mis ahorros?</i>\n\n"
    "Comandos: /resumen /ahorro /cuentas /prestamos /fijos /dashboard /deshacer /consumo /ayuda"
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
    return [[{"text": "🏷️ Categoría", "callback_data": f"c:{ref}"},
             {"text": "💳 Cuenta", "callback_data": f"a:{ref}"},
             {"text": "↩️ Deshacer", "callback_data": f"u:{ref}"}]]


def account_keyboard(tx: Transaction) -> list[list[dict[str, Any]]]:
    ref = f"{tx.date}:{tx.id}"
    buttons = [{"text": accounts.label(k), "callback_data": f"sa:{ref}:{k}"} for k in accounts.ACCOUNTS
               if k != tx.account]
    return [buttons[i:i + 3] for i in range(0, len(buttons), 3)]


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


_AMOUNT_IN_TEXT = re.compile(r"\$\s?[\d.]+")


def drop_unbacked_amounts(text: str, notes: list[str]) -> str:
    """Quita frases del modelo con montos que no aparecen en los desgloses calculados por código."""
    if not notes:
        return text
    allowed = {a.replace(" ", "") for n in notes for a in _AMOUNT_IN_TEXT.findall(n)}
    sentences = re.split(r"(?<=[.!?])\s+", text)
    kept = [s for s in sentences if all(a.replace(" ", "") in allowed for a in _AMOUNT_IN_TEXT.findall(s))]
    return " ".join(kept).strip() or "Ese es el plan. Si quieres, ajustamos los eventos o el colchón."


_TELEGRAM_TAGS = re.compile(r"</?(?!(?:b|i|u|s|code|pre|a)\b)[a-zA-Z_][\w-]*[^>]*>")
_BARE_AMOUNT = re.compile(r"(?<![\d.,$])\$?\s?(\d{4,})(?:[.,]00)?\s?(COP|USD|EUR|MXN)\b")


def tidy_reply(text: str, currency: str) -> str:
    """Deja la respuesta del modelo lista para Telegram: sin etiquetas desconocidas (<response>...),
    montos con formato local y nombres de categorías legibles en lugar de sus claves internas."""
    text = _TELEGRAM_TAGS.sub("", text)
    text = _BARE_AMOUNT.sub(lambda m: fmt(Decimal(m.group(1)), m.group(2)), text)
    for key in list(categories.CATEGORIES) + list(categories.INCOME_CATEGORIES):
        if "_" in key or f"<i>{key}</i>" in text:
            text = text.replace(f"<i>{key}</i>", categories.label(key)).replace(key, categories.label(key))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def confirmation_text(profile: Profile, recorded: list[Transaction], model_text: str) -> str:
    cur = profile.currency
    today = finance.today_for(profile).isoformat()
    lines = []
    for tx in recorded:
        when = "" if tx.date == today else f" · {tx.date[8:]}/{tx.date[5:7]}"
        if tx.kind == "transfer":
            lines.append(f"🔁 Transferencia: <b>{fmt(tx.amount, cur)}</b> · {accounts.label(tx.account)} → "
                         f"{accounts.label(tx.merchant)}{when} <i>(no es gasto)</i>")
            continue
        verb = "Ingreso" if tx.kind == "income" else "Gasto"
        via = f" · {accounts.label(tx.account)}" if tx.account else ""
        lines.append(f"✅ {verb}: <b>{fmt(tx.amount, cur)}</b> · {categories.label(tx.category)}{via}{when}")
        if tx.description:
            lines.append(f"<i>{html.escape(tx.description)}</i>")
    accounts_used = {a for tx in recorded for a in (tx.account, tx.merchant if tx.kind == "transfer" else "")
                     if a in profile.accounts}
    for acc in accounts_used:
        lines.append(f"Saldo en {accounts.ACCOUNTS[acc][0]}: {fmt(profile.accounts[acc]['saldo'], cur)}")
    questions = [s for s in re.split(r"(?<=[.!?])\s+", model_text) if s.strip().endswith("?")]
    if questions:
        lines.append("")
        lines += questions[:2]
    return "\n".join(lines)


def handle_conversation(tg: Telegram, repo: Repository, profile: Profile, chat_id: int, text: str,
                        source: str = "text", reply_to: int | None = None,
                        image: tuple[bytes, str] | None = None) -> None:
    """Pasa el mensaje (escrito, transcrito o un pantallazo) al asistente y envía su respuesta."""
    learning.observe_input(profile, {"voice": "voice", "image": "image"}.get(source, "text"))
    ledger = Ledger(repo, profile)
    if image is None and config.FAST_PATH and fast_record(tg, ledger, chat_id, text, source, reply_to):
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
    tg.typing(chat_id)
    result = assistant.reply(ledger, text, source=source, history=history, image=image)
    if any(result.tokens.values()):
        usage.add_llm(profile.stats, ledger.today, result.tokens)
    repo.save_profile(profile)
    keyboard = tx_keyboard(result.recorded[-1]) if len(result.recorded) == 1 else None
    for note in result.notes:
        undo = [[{"text": "↩️ Deshacer importación", "callback_data": f"ui:{result.import_id}"}]] \
            if result.import_id and note.startswith("📸") else None
        tg.send(chat_id, note, keyboard=undo, reply_to=reply_to)
    if result.import_id:
        keyboard = None  # el botón de deshacer va en el detalle de la importación
    result.text = drop_unbacked_amounts(tidy_reply(result.text, profile.currency), result.notes)
    if result.recorded and not result.import_id:
        # La confirmación de lo registrado la arma el código (formato fijo); del modelo solo queda una pregunta.
        result.text = confirmation_text(profile, result.recorded, result.text)
    tg.send(chat_id, result.text, keyboard=keyboard, reply_to=None if result.notes else reply_to)
    send_alerts(tg, chat_id, result.alerts)
    repo.add_message(profile.user_id, "user", f"[pantallazo] {text}" if image else text)
    repo.add_message(profile.user_id, "assistant", result.text)


def undo_import(tg: Telegram, repo: Repository, profile: Profile, callback: dict[str, Any], import_id: str) -> None:
    message = callback.get("message") or {}
    data = repo.get_import(profile.user_id, import_id)
    if data is None:
        tg.answer_callback(callback["id"], "Esa importación ya no se puede deshacer")
        return
    ledger = Ledger(repo, profile)
    removed = 0
    for ref in data.get("refs") or []:
        tx_date, tx_id = ref.split(":")
        tx = repo.get_transaction(profile.user_id, tx_date, tx_id)
        if tx:
            ledger.delete(tx)
            removed += 1
    account = data.get("account")
    if account:
        if data.get("had_balance"):
            ledger.set_balance(account, Decimal(str(data["balance_before"])))
        else:
            profile.accounts.pop(account, None)
            repo.save_profile(profile)
    repo.delete_import(profile.user_id, import_id)
    tg.answer_callback(callback["id"], "Importación deshecha")
    tg.edit(message["chat"]["id"], message["message_id"],
            f"↩️ Deshice la importación del pantallazo ({removed} movimiento(s) borrados y saldo restaurado).")


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
                     necessity=categories.default_necessity(entry.category), source=source,
                     account=entry.account)
    alerts = ledger.record(tx)
    usage.add_fast(profile.stats, ledger.today)
    verb = "Ingreso" if tx.kind == "income" else "Gasto"
    via = f" · {accounts.label(tx.account)}" if tx.account else ""
    balance = profile.accounts.get(tx.account)
    left = f"\nSaldo en {accounts.ACCOUNTS[tx.account][0]}: {fmt(balance['saldo'], profile.currency)}" if balance else ""
    tg.send(chat_id, f"✅ {verb}: <b>{fmt(tx.amount, profile.currency)}</b> · {categories.label(tx.category)}{via}\n"
                     f"<i>{html.escape(tx.description)}</i>{left}", keyboard=tx_keyboard(tx), reply_to=reply_to)
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
        debts = repo.list_debts(profile.user_id, include_closed=True)
        pockets = repo.list_pockets(profile.user_id)
        tg.send(chat_id, finance.summary_text(profile, ledger.month_transactions(), subs, ledger.today, debts,
                                              pockets),
                keyboard=webapp_button("📊 Ver dashboard completo"))
        return True
    if cmd == "/consumo":
        tg.send(chat_id, usage.usage_text(profile.stats, ledger.today))
        return True
    if cmd == "/dashboard":
        tg.send(chat_id, "Tu dashboard con gráficas, presupuestos y movimientos:", keyboard=webapp_button())
        return True
    if cmd in ("/suscripciones", "/fijos"):
        subs = repo.list_subscriptions(profile.user_id)
        cur = profile.currency
        if not subs and not profile.reserves:
            tg.send(chat_id, "No tienes pagos fijos registrados. Dime algo como "
                             "<i>pago Spotify 17 mil el 10 de cada mes</i> o "
                             "<i>gasto 200 mil al mes en gasolina</i>.")
            return True
        lines = []
        if subs:
            lines.append("🔁 <b>Suscripciones</b>")
            lines += [f"• <b>{html.escape(s.name)}</b>: {fmt(s.amount, cur)} ({s.cycle}) — próximo {s.next_charge}"
                      for s in sorted(subs, key=lambda s: s.next_charge)]
            lines.append(f"Costo fijo mensual: <b>{fmt(finance.monthly_fixed(subs), cur)}</b>")
        reserves = finance.reserves_remaining(profile, ledger.month_transactions())
        if reserves:
            lines.append("\n⛽ <b>Apartado cada mes</b>")
            lines += [f"• {html.escape(r['nombre'].capitalize())}: {fmt(r['gastado'], cur)} de {fmt(r['monto'], cur)}"
                      f" — quedan {fmt(r['pendiente'], cur)} apartados" for r in reserves]
        tg.send(chat_id, "\n".join(lines))
        return True
    if cmd == "/ahorro":
        summary = finance.savings_summary(profile, repo.list_pockets(profile.user_id), ledger.today)
        tg.send(chat_id, finance.savings_text(summary), keyboard=webapp_button("🐷 Ver en el dashboard"))
        return True
    if cmd == "/cuentas":
        tg.send(chat_id, finance.accounts_text(profile, ledger.month_transactions()))
        return True
    if cmd in ("/prestamos", "/deudas"):
        tg.send(chat_id, debts_text(profile, repo.list_debts(profile.user_id), ledger.today))
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


def debts_text(profile: Profile, debts: list, today: Any) -> str:
    if not debts:
        return ("No tienes préstamos registrados. Dime algo como <i>le presté 50 mil a Juan</i> "
                "o <i>le debo 200 mil a mi papá, le pago el 30</i>.")
    cur = profile.currency
    lines = []
    for direction, title in (("me_deben", "🤝 <b>Te deben</b>"), ("debo", "💳 <b>Debes</b>")):
        group = sorted((d for d in debts if d.direction == direction), key=lambda d: d.due_date or "9999")
        if not group:
            continue
        lines.append(title)
        for d in group:
            due = ""
            if d.due_date:
                days = (date.fromisoformat(d.due_date) - today).days
                due = (f" — vence el {d.due_date[8:]}/{d.due_date[5:7]}" if days > 0 else
                       " — <b>vence hoy</b>" if days == 0 else f" — <b>vencido hace {-days} días</b>")
            paid = f" (abonado {fmt(d.paid, cur)})" if d.paid else ""
            lines.append(f"• {html.escape(d.person)}: <b>{fmt(d.balance, cur)}</b>{paid}{due}")
        total = sum((d.balance for d in group), Decimal(0))
        lines.append(f"Total: {fmt(total, cur)}\n")
    return "\n".join(lines).strip()


def debt_keyboard(debt: Any) -> list[list[dict[str, Any]]]:
    paid = "✅ Ya le pagué" if debt.direction == "debo" else "✅ Ya me pagó"
    return [[{"text": paid, "callback_data": f"dp:{debt.id}"},
             {"text": "⏰ En 3 días", "callback_data": f"dz:{debt.id}"}]]


def handle_debt_callback(tg: Telegram, repo: Repository, profile: Profile, callback: dict[str, Any],
                         action: str, debt_id: str) -> None:
    message = callback.get("message") or {}
    debt = repo.get_debt(profile.user_id, debt_id)
    if debt is None or debt.closed:
        tg.answer_callback(callback["id"], "Ese préstamo ya está saldado")
        return
    ledger = Ledger(repo, profile)
    cur = profile.currency
    if action == "dp":
        paid = debt.add_payment(debt.balance, ledger.today.isoformat())
        repo.save_debt(debt)
        tg.answer_callback(callback["id"], "Registrado")
        who = f"le pagaste a {debt.person}" if debt.direction == "debo" else f"{debt.person} te pagó"
        tg.edit(message["chat"]["id"], message["message_id"], f"✅ Listo: {html.escape(who)} {fmt(paid, cur)}.")
    else:  # dz: posponer
        base = max(ledger.today, date.fromisoformat(debt.due_date)) if debt.due_date else ledger.today
        debt.due_date = (base + timedelta(days=3)).isoformat()
        repo.save_debt(debt)
        tg.answer_callback(callback["id"], "Te lo recuerdo en 3 días")
        tg.edit(message["chat"]["id"], message["message_id"],
                f"⏰ Te recuerdo lo de {html.escape(debt.person)} el {debt.due_date[8:]}/{debt.due_date[5:7]}.")


def handle_callback(tg: Telegram, repo: Repository, callback: dict[str, Any]) -> None:
    data = callback.get("data", "")
    message = callback.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    profile = repo.get_profile(callback["from"]["id"])
    parts = data.split(":")
    if profile is not None and len(parts) == 2 and parts[0] == "ui":
        undo_import(tg, repo, profile, callback, parts[1])
        return
    if profile is not None and len(parts) == 2 and parts[0] in ("dp", "dz"):
        handle_debt_callback(tg, repo, profile, callback, parts[0], parts[1])
        return
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
    elif action == "a":
        tg.answer_callback(callback["id"])
        tg.edit(chat_id, message_id,
                f"¿Con qué cuenta pagaste <b>{html.escape(tx.description)}</b> ({fmt(tx.amount, cur)})?",
                keyboard=account_keyboard(tx))
    elif action == "sa" and len(parts) == 4 and accounts.is_valid(parts[3]):
        ledger.change_account(tx, parts[3])
        tg.answer_callback(callback["id"], "Cuenta actualizada")
        tg.edit(chat_id, message_id,
                f"✅ {html.escape(tx.description)} — {fmt(tx.amount, cur)} · {categories.label(tx.category)} · "
                f"{accounts.label(tx.account)}", keyboard=tx_keyboard(tx))
    elif action == "s" and len(parts) == 4 and categories.is_valid(parts[3]):
        ledger.recategorize(tx, parts[3])
        tg.answer_callback(callback["id"], "Categoría actualizada; lo tendré en cuenta")
        tg.edit(chat_id, message_id,
                f"✅ {html.escape(tx.description)} — {fmt(tx.amount, cur)} → {categories.label(tx.category)}\n"
                f"<i>Aprendido: la próxima vez lo clasifico así.</i>",
                keyboard=tx_keyboard(tx))
    else:
        tg.answer_callback(callback["id"], "Acción no válida")
