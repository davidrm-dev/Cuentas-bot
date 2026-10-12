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
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from .. import bot, categories, finance, learning
from ..finance import fmt, month_bounds, previous_month
from ..ledger import Ledger
from ..models import Debt, Profile, Subscription, Transaction
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

    # 3) Préstamos: recordatorios por fecha límite
    debts = repo.list_debts(profile.user_id, include_closed=True)
    for debt in debts:
        reminder = debt_reminder(profile, debt, today, ledger, subs, debts)
        if reminder and repo.claim_alert(profile.user_id, reminder[0]):
            tg.send(profile.chat_id, reminder[1], keyboard=bot.debt_keyboard(debt))

    # 3b) Día de pago: recordar cuánto apartar para el ahorro
    if profile.payday and today.day == min(profile.payday, month_bounds(today)[1].day):
        pockets = repo.list_pockets(profile.user_id)
        if repo.claim_alert(profile.user_id, f"payday-save:{today.strftime('%Y-%m')}"):
            tg.send(profile.chat_id, payday_savings_text(profile, pockets, today))

    # 4) Aprendizaje
    history_start = month_bounds(today.replace(day=1) - timedelta(days=31 * LEARNING_MONTHS))[0]
    history = repo.list_transactions(profile.user_id, history_start.isoformat(), today.isoformat())
    learning.recompute(profile, history, today)
    repo.save_profile(profile)

    # 5) Cierre de mes
    if today.day == 1:
        prev = previous_month(today)
        if repo.claim_alert(profile.user_id, f"monthly:{prev.strftime('%Y-%m')}"):
            start, end = month_bounds(prev)
            month_txs = [t for t in history if start.isoformat() <= t.date <= end.isoformat()]
            if month_txs:
                tg.send(profile.chat_id, monthly_close_text(profile, month_txs, prev),
                        keyboard=webapp_button("📊 Ver el mes completo"))


def payday_savings_text(profile: Profile, pockets: list, today: date) -> str:
    cur = profile.currency
    summary = finance.savings_summary(profile, pockets, today)
    lines = ["💰 <b>¡Día de pago!</b> Antes de gastar, aparta tu ahorro:"]
    if summary.get("meta_ahorro_mes"):
        lines.append(f"• Tu meta de ahorro ({profile.savings_goal_pct:.0f}%): {fmt(Decimal(str(summary['meta_ahorro_mes'])), cur)}")
    for b in summary["bolsillos"]:
        if b.get("cuota_mensual"):
            lines.append(f"• {html.escape(b['nombre'])}: {fmt(Decimal(str(b['cuota_mensual'])), cur)} para llegar a la meta")
    fe = summary.get("fondo_emergencia")
    if fe and fe["tienes"] < fe["recomendado_minimo"]:
        lines.append(f"• Fondo de emergencia: cubres {fe['meses_cubiertos']:g} de 3 meses recomendados")
    if len(lines) == 1:
        lines.append("Dime cuánto quieres apartar y a qué bolsillo: <i>ahorré 200 mil en la cajita Nu</i>.")
    return "\n".join(lines)


def debt_reminder(profile: Profile, debt: Debt, today: date, ledger: Ledger, subs: list[Subscription],
                  debts: list[Debt]) -> tuple[str, str] | None:
    """(clave única, texto) si toca recordar este préstamo hoy: 3 días antes, 1 día antes, el día y cada semana vencido."""
    if debt.closed or not debt.due_date:
        return None
    days = (date.fromisoformat(debt.due_date) - today).days
    if days in (3, 1):
        bucket, when = f"{days}d", f"en {days} día{'s' if days > 1 else ''} ({debt.due_date[8:]}/{debt.due_date[5:7]})"
    elif days == 0:
        bucket, when = "hoy", "hoy"
    elif days < 0 and (-days) % 7 == 1:
        bucket, when = f"vencido{-days}", f"hace {-days} día{'s' if -days > 1 else ''}"
    else:
        return None
    cur = profile.currency
    name, amount = html.escape(debt.person), fmt(debt.balance, cur)
    if debt.direction == "me_deben":
        verb = "quedó de pagarte" if days >= 0 else "debía pagarte"
        text = f"🔔 <b>{name}</b> te debe <b>{amount}</b> y {verb} {when}. ¿Ya te pagó?"
    else:
        verb = "vence" if days >= 0 else "venció"
        text = f"⏰ Le debes <b>{amount}</b> a <b>{name}</b>; {verb} {when}."
        spendable = finance.spendable_income(profile)
        if spendable:
            # committed ya incluye esta deuda, así que lo que queda es después de pagarla.
            pockets = ledger.repo.list_pockets(profile.user_id)
            left = spendable - finance.committed_month(profile, ledger.month_transactions(), subs, today, debts,
                                                       pockets)
            text += (f"\nCon lo que tienes planeado te alcanza: después de pagarla te quedarían ~{fmt(left, cur)} "
                     "para el resto del mes." if left >= 0 else
                     f"\nOjo: este mes vas justo, te faltarían ~{fmt(-left, cur)}. Si quieres, pregúntame cómo "
                     "organizar el pago o si conviene abonar una parte.")
        else:
            text += "\nAparta ese dinero para no gastártelo. Si me dices tu ingreso te ayudo a planear el pago."
    return f"debt:{debt.id}:{debt.due_date}:{bucket}", text


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
    if profile.reserves:
        lines.append("Este mes vuelvo a apartar: " + ", ".join(
            f"{n} {fmt(r['monto'], cur)}" for n, r in profile.reserves.items()))
    over = [c for c, b in profile.budgets.items() if t["by_category"].get(c, 0) > b]
    if over:
        lines.append("Te pasaste en: " + ", ".join(categories.label(c) for c in over))
    return "\n".join(lines)
