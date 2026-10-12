"""Tarea diaria (EventBridge Scheduler).

Por cada usuario:
1. Registra los cobros de suscripciones que vencen hoy (o que quedaron atrasados).
2. Avisa de cobros de los próximos 2 días.
3. Recalcula lo aprendido de sus hábitos con los últimos meses.
4. El día 1 envía el cierre del mes anterior.
"""
from __future__ import annotations

import html
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .. import bot, categories, finance, learning
from ..finance import fmt, month_bounds, previous_month
from ..ledger import Ledger
from ..models import Profile, Subscription, Transaction
from ..repository import Repository
from ..telegram import Telegram, webapp_button

log = logging.getLogger()
log.setLevel(logging.INFO)
LEARNING_MONTHS = 6


def handler(_event: dict[str, Any], _context: Any) -> dict[str, int]:
    repo, tg = Repository(), Telegram()
    now = datetime.now(tz=ZoneInfo("UTC"))
    # Margen de un día para cubrir husos horarios adelantados respecto a UTC.
    due_by_user: dict[int, list[Subscription]] = defaultdict(list)
    for sub in repo.due_subscriptions((now.date() + timedelta(days=1)).isoformat()):
        due_by_user[sub.user_id].append(sub)

    processed = errors = 0
    for profile in repo.list_users():
        try:
            run_for_user(repo, tg, profile, due_by_user.get(profile.user_id, []), now)
            processed += 1
        except Exception:  # noqa: BLE001 - un usuario con problemas no frena al resto
            errors += 1
            log.exception("Scheduler falló para el usuario %s", profile.user_id)
    return {"processed": processed, "errors": errors}


def run_for_user(repo: Repository, tg: Telegram, profile: Profile, due: list[Subscription],
                 now: datetime) -> None:
    ledger = Ledger(repo, profile, now)
    today = ledger.today
    cur = profile.currency

    # 1) Cobros de suscripciones
    charged, alerts = [], []
    for sub in due:
        while sub.active and date.fromisoformat(sub.next_charge) <= today:
            charged.append((sub.name, sub.amount, sub.next_charge))
            alerts += ledger.charge_subscription(sub)
    if charged:
        lines = [f"• {html.escape(name)}: {fmt(amount, cur)} ({day})" for name, amount, day in charged]
        tg.send(profile.chat_id, "🔁 <b>Registré estos cobros automáticos</b>\n" + "\n".join(lines))
        bot.send_alerts(tg, profile.chat_id, alerts)

    # 2) Recordatorio de próximos cobros
    subs = repo.list_subscriptions(profile.user_id)
    soon = [s for s in finance.upcoming_charges(subs, today + timedelta(days=1), 1)
            if repo.claim_alert(profile.user_id, f"remind:{s.id}:{s.next_charge}")]
    if soon:
        lines = [f"• {html.escape(s.name)}: {fmt(s.amount, cur)} el {s.next_charge}" for s in soon]
        tg.send(profile.chat_id, "⏰ <b>Próximos cobros</b>\n" + "\n".join(lines) +
                "\n\n¿Alguna ya no la usas? Dime <i>cancela X</i> y dejo de contarla.")

    # 3) Aprendizaje
    history_start = month_bounds(today.replace(day=1) - timedelta(days=31 * LEARNING_MONTHS))[0]
    history = repo.list_transactions(profile.user_id, history_start.isoformat(), today.isoformat())
    learning.recompute(profile, history, today)
    repo.save_profile(profile)

    # 4) Cierre de mes
    if today.day == 1:
        prev = previous_month(today)
        if repo.claim_alert(profile.user_id, f"monthly:{prev.strftime('%Y-%m')}"):
            start, end = month_bounds(prev)
            month_txs = [t for t in history if start.isoformat() <= t.date <= end.isoformat()]
            if month_txs:
                tg.send(profile.chat_id, monthly_close_text(profile, month_txs, prev),
                        keyboard=webapp_button("📊 Ver el mes completo"))


def monthly_close_text(profile: Profile, txs: list[Transaction], month: date) -> str:
    cur = profile.currency
    t = finance.totals(txs)
    lines = [f"📅 <b>Cierre de {month.strftime('%m/%Y')}</b>",
             f"Ingresos: {fmt(t['income'], cur)}",
             f"Gastos: {fmt(t['expense'], cur)}",
             f"Balance: <b>{fmt(t['net'], cur)}</b>"]
    if t["income"] > 0:
        rate = t["net"] / t["income"] * 100
        goal = profile.savings_goal_pct
        lines.append(f"Ahorraste el {rate:.0f}% " + ("✅" if rate >= goal else f"(meta: {goal:.0f}%)"))
    for cat, amount in list(t["by_category"].items())[:3]:
        lines.append(f"• {categories.label(cat)}: {fmt(amount, cur)}")
    over = [c for c, b in profile.budgets.items() if t["by_category"].get(c, 0) > b]
    if over:
        lines.append("Te pasaste en: " + ", ".join(categories.label(c) for c in over))
    return "\n".join(lines)
