"""Aprendizaje del perfil: cómo gasta el usuario y cómo usa el bot.

Dos niveles:
* Incremental (en cada movimiento/corrección): comercio -> categoría, ticket promedio
  por categoría, hábitos de uso (texto vs voz, horas).
* Recalculado (scheduler diario): promedios mensuales por categoría, ingreso típico,
  días de más gasto y observaciones en lenguaje natural que se le pasan al modelo.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from . import categories
from .finance import fmt, month_bounds
from .models import Profile, Transaction

WEEKDAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")


def merchant_key(text: str) -> str:
    return categories.normalize(text)[:40]


def observe_transaction(profile: Profile, tx: Transaction, local_hour: int | None = None) -> None:
    stats = profile.stats
    if tx.merchant and tx.kind == "expense":
        profile.merchant_categories[merchant_key(tx.merchant)] = tx.category
    if tx.kind == "expense" and tx.source != "subscription":
        _ticket_add(stats, tx.category, float(tx.amount))
    usage = stats.setdefault("usage", {})
    usage[tx.source] = usage.get(tx.source, 0) + 1
    if tx.account:
        accs = usage.setdefault("accounts", {})
        accs[tx.account] = accs.get(tx.account, 0) + 1
    if local_hour is not None:
        hours = usage.setdefault("hours", [0] * 24)
        hours[local_hour] += 1
    stats["last_active"] = tx.created_at


def observe_input(profile: Profile, mode: str) -> None:
    """Cuenta cómo se comunica el usuario ('voice'/'text') aunque no registre gastos."""
    msgs = profile.stats.setdefault("usage", {}).setdefault("messages", {})
    msgs[mode] = msgs.get(mode, 0) + 1


def observe_correction(profile: Profile, tx: Transaction, old_category: str) -> None:
    """El usuario corrigió la categoría: es la señal más fuerte para aprender."""
    if tx.merchant:
        profile.merchant_categories[merchant_key(tx.merchant)] = tx.category
    elif tx.description:
        profile.merchant_categories[merchant_key(tx.description)] = tx.category
    if tx.kind == "expense" and tx.source != "subscription":
        _ticket_remove(profile.stats, old_category, float(tx.amount))
        _ticket_add(profile.stats, tx.category, float(tx.amount))
    corrections = profile.stats.setdefault("corrections", 0)
    profile.stats["corrections"] = corrections + 1


def _ticket_add(stats: dict[str, Any], category: str, amount: float) -> None:
    t = stats.setdefault("avg_ticket", {}).setdefault(category, {"mean": 0.0, "count": 0})
    t["count"] += 1
    t["mean"] = round(t["mean"] + (amount - t["mean"]) / t["count"], 2)


def _ticket_remove(stats: dict[str, Any], category: str, amount: float) -> None:
    t = stats.get("avg_ticket", {}).get(category)
    if not t or t["count"] <= 1:
        stats.get("avg_ticket", {}).pop(category, None)
        return
    t["mean"] = round((t["mean"] * t["count"] - amount) / (t["count"] - 1), 2)
    t["count"] -= 1


def recompute(profile: Profile, history: list[Transaction], today: date) -> None:
    """Recalcula patrones con los últimos meses completos (excluye el mes en curso)."""
    current_start, _ = month_bounds(today)
    complete = [tx for tx in history if date.fromisoformat(tx.date) < current_start]
    months = sorted({tx.date[:7] for tx in complete})
    stats = profile.stats
    stats["months_observed"] = len(months)

    per_month_cat: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    income_by_month: dict[str, float] = defaultdict(float)
    weekday = [0.0] * 7
    weekend = total_var = discretionary = expense_total = 0.0
    for tx in complete:
        amount = float(tx.amount)
        if tx.kind == "transfer":
            continue
        if tx.kind == "income":
            income_by_month[tx.date[:7]] += amount
            continue
        per_month_cat[tx.date[:7]][tx.category] += amount
        expense_total += amount
        if tx.necessity == "discrecional":
            discretionary += amount
        if tx.source != "subscription":
            wd = date.fromisoformat(tx.date).weekday()
            weekday[wd] += amount
            total_var += amount
            if wd >= 5:
                weekend += amount

    if months:
        all_cats = {c for m in per_month_cat.values() for c in m}
        stats["monthly_avg"] = {
            c: round(sum(per_month_cat[m].get(c, 0.0) for m in months) / len(months), 2) for c in all_cats
        }
        stats["avg_monthly_expense"] = round(expense_total / len(months), 2)
    incomes = [v for v in income_by_month.values() if v > 0]
    if incomes:
        stats["avg_monthly_income"] = round(sum(incomes) / len(incomes), 2)
    stats["discretionary_ratio"] = round(discretionary / expense_total, 3) if expense_total else None
    stats["weekday_spend"] = [round(v, 2) for v in weekday]

    profile.insights = _insights(profile, weekday, weekend, total_var)


def _insights(profile: Profile, weekday: list[float], weekend: float, total_var: float) -> list[str]:
    stats, cur = profile.stats, profile.currency
    out: list[str] = []
    monthly = stats.get("monthly_avg") or {}
    if monthly:
        top = sorted(monthly.items(), key=lambda kv: -kv[1])[:3]
        out.append("Donde más se le va el dinero al mes: " +
                   ", ".join(f"{categories.label(c)} (~{fmt(Decimal(str(v)), cur)})" for c, v in top) + ".")
    if total_var > 0:
        peak = max(range(7), key=lambda i: weekday[i])
        out.append(f"Suele gastar más los {WEEKDAYS[peak]}; el fin de semana concentra el "
                   f"{weekend / total_var * 100:.0f}% de sus gastos variables.")
    ratio = stats.get("discretionary_ratio")
    if ratio is not None:
        out.append(f"El {ratio * 100:.0f}% de su gasto es discrecional (no esencial).")
    income = profile.monthly_income or (Decimal(str(stats["avg_monthly_income"]))
                                        if stats.get("avg_monthly_income") else None)
    avg_exp = stats.get("avg_monthly_expense")
    if income and avg_exp:
        rate = (float(income) - avg_exp) / float(income) * 100
        goal = float(profile.savings_goal_pct)
        status = "cumple" if rate >= goal else "no cumple"
        out.append(f"Ahorra en promedio el {rate:.0f}% de sus ingresos ({status} su meta del {goal:.0f}%).")
    usage = stats.get("usage", {}).get("messages", {})
    voice, text = usage.get("voice", 0), usage.get("text", 0)
    if voice + text >= 10:
        pref = "notas de voz" if voice > text else "mensajes escritos"
        out.append(f"Prefiere comunicarse con {pref}.")
    hours = stats.get("usage", {}).get("hours")
    if hours and sum(hours) >= 10:
        peak_hour = max(range(24), key=lambda h: hours[h])
        out.append(f"Registra gastos sobre todo hacia las {peak_hour}:00.")
    if stats.get("corrections", 0) >= 3:
        out.append("Ha corregido varias categorías: confirma la categoría cuando haya dudas.")
    return out


def context_for_llm(profile: Profile) -> str:
    """Resumen del perfil para el modelo: datos declarados + lo aprendido."""
    cur = profile.currency
    lines = [f"Nombre: {profile.first_name or 'desconocido'}", f"Moneda: {cur}",
             f"Zona horaria: {profile.timezone}"]
    if profile.monthly_income:
        lines.append(f"Ingreso mensual declarado: {fmt(profile.monthly_income, cur)}")
    elif profile.stats.get("avg_monthly_income"):
        lines.append(f"Ingreso mensual aprendido (no declarado): "
                     f"{fmt(Decimal(str(profile.stats['avg_monthly_income'])), cur)}")
    else:
        lines.append("Ingreso mensual: desconocido (pregúntaselo cuando sea natural)")
    if profile.payday:
        lines.append(f"Día de cobro: {profile.payday}")
    lines.append(f"Meta de ahorro: {profile.savings_goal_pct:.0f}% del ingreso")
    if profile.budgets:
        lines.append("Presupuestos mensuales: " + ", ".join(
            f"{c}={fmt(v, cur)}" for c, v in profile.budgets.items()))
    if profile.default_account:
        lines.append(f"Cuenta principal: {profile.default_account}")
    if profile.accounts:
        lines.append("Saldos que lleva: " + ", ".join(
            f"{k}={fmt(v['saldo'], cur)}" for k, v in profile.accounts.items()))
    used = profile.stats.get("usage", {}).get("accounts") or {}
    if used:
        lines.append("Cuentas que más usa: " + ", ".join(k for k, _ in sorted(used.items(), key=lambda kv: -kv[1])[:3]))
    if profile.reserves:
        lines.append("Gastos fijos apartados cada mes: " + ", ".join(
            f"{name}={fmt(r['monto'], cur)}" for name, r in profile.reserves.items()))
    if profile.merchant_categories:
        sample = list(profile.merchant_categories.items())[-25:]
        lines.append("Comercios ya clasificados por el usuario: " + ", ".join(f"{m}→{c}" for m, c in sample))
    if profile.insights:
        lines.append("Patrones observados:\n- " + "\n- ".join(profile.insights))
    return "\n".join(lines)
