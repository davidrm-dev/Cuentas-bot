"""Cálculos financieros puros: resúmenes, alertas, "¿puedo salir?" y dashboard.

No tocan AWS: reciben listas de entidades y devuelven datos, para poder testearlos.
"""
from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from . import categories
from .models import Profile, Subscription, Transaction, money

ZERO = Decimal("0")


@dataclass
class Alert:
    level: str  # "critical" | "warning"
    key: str  # clave de deduplicación
    message: str


def today_for(profile: Profile, now: datetime | None = None) -> date:
    now = now or datetime.now(tz=ZoneInfo("UTC"))
    return now.astimezone(ZoneInfo(profile.timezone)).date()


def month_bounds(d: date) -> tuple[date, date]:
    return d.replace(day=1), d.replace(day=calendar.monthrange(d.year, d.month)[1])


def previous_month(d: date) -> date:
    return d.replace(day=1) - timedelta(days=1)


def fmt(amount: Decimal | float, currency: str) -> str:
    value = Decimal(str(amount))
    sign = "-" if value < 0 else ""
    text = f"{abs(value):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    if text.endswith(",00"):
        text = text[:-3]
    return f"{sign}${text} {currency}"


def totals(txs: Iterable[Transaction]) -> dict[str, Any]:
    by_category: dict[str, Decimal] = defaultdict(lambda: ZERO)
    income = expense = discretionary = ZERO
    for tx in txs:
        if tx.kind == "income":
            income += tx.amount
        else:
            expense += tx.amount
            by_category[tx.category] += tx.amount
            if tx.necessity == "discrecional":
                discretionary += tx.amount
    return {
        "income": income,
        "expense": expense,
        "net": income - expense,
        "discretionary": discretionary,
        "by_category": dict(sorted(by_category.items(), key=lambda kv: -kv[1])),
    }


def monthly_fixed(subs: Iterable[Subscription]) -> Decimal:
    return sum((s.monthly_cost for s in subs if s.active), ZERO)


def pending_charges_this_month(subs: Iterable[Subscription], today: date) -> Decimal:
    """Suscripciones que aún se cobrarán antes de fin de mes (no registradas todavía)."""
    _, last = month_bounds(today)
    total = ZERO
    for s in subs:
        if not s.active:
            continue
        charge = date.fromisoformat(s.next_charge)
        while today <= charge <= last:
            total += s.amount
            if s.cycle != "weekly":
                break
            charge += timedelta(days=7)
    return total


def reference_income(profile: Profile) -> Decimal | None:
    """Ingreso declarado o, si no lo dio, el que el bot aprendió de sus registros."""
    if profile.monthly_income:
        return profile.monthly_income
    learned = profile.stats.get("avg_monthly_income")
    return money(learned) if learned else None


def spendable_income(profile: Profile) -> Decimal | None:
    income = reference_income(profile)
    if income is None:
        return None
    return money(income * (Decimal(100) - profile.savings_goal_pct) / Decimal(100))


def upcoming_charges(subs: Iterable[Subscription], today: date, days: int = 7) -> list[Subscription]:
    limit = today + timedelta(days=days)
    return sorted(
        (s for s in subs if s.active and today <= date.fromisoformat(s.next_charge) <= limit),
        key=lambda s: s.next_charge,
    )


# ---------------------------------------------------------------------------
# Alertas
# ---------------------------------------------------------------------------

def check_alerts(profile: Profile, tx: Transaction, month_txs: list[Transaction],
                 subs: list[Subscription], today: date) -> list[Alert]:
    """Alertas disparadas por un gasto recién registrado (month_txs ya lo incluye)."""
    if tx.kind != "expense":
        return []
    cur = profile.currency
    ym = today.strftime("%Y-%m")
    t = totals(month_txs)
    alerts: list[Alert] = []

    # 1) Presupuesto por categoría
    budget = profile.budgets.get(tx.category)
    if budget:
        spent = t["by_category"].get(tx.category, ZERO)
        pct = spent / budget * 100
        name = categories.label(tx.category)
        if pct >= 100:
            alerts.append(Alert("critical", f"budget:{tx.category}:{ym}:100",
                                f"Superaste el presupuesto de {name}: llevas {fmt(spent, cur)} de "
                                f"{fmt(budget, cur)} ({pct:.0f}%)."))
        elif pct >= 80:
            alerts.append(Alert("warning", f"budget:{tx.category}:{ym}:80",
                                f"Ya usaste el {pct:.0f}% del presupuesto de {name} "
                                f"({fmt(spent, cur)} de {fmt(budget, cur)})."))

    # 2) Gasto total contra lo que se puede gastar sin tocar la meta de ahorro
    spendable = spendable_income(profile)
    if spendable:
        committed = t["expense"] + pending_charges_this_month(subs, today)
        pct = committed / spendable * 100
        if pct >= 100:
            alerts.append(Alert("critical", f"spendable:{ym}:100",
                                f"Este mes ya comprometiste {fmt(committed, cur)}, más de lo disponible "
                                f"({fmt(spendable, cur)}) sin tocar tu meta de ahorro del "
                                f"{profile.savings_goal_pct:.0f}%."))
        elif pct >= 85:
            alerts.append(Alert("warning", f"spendable:{ym}:85",
                                f"Llevas comprometido el {pct:.0f}% de lo que puedes gastar este mes. "
                                f"Te quedan {fmt(spendable - committed, cur)}."))

        # 3) Ritmo de gasto: proyección a fin de mes
        _, last = month_bounds(today)
        if today.day >= 5:
            variable = t["expense"] - sum((x.amount for x in month_txs if x.source == "subscription"), ZERO)
            projected = variable / today.day * last.day + monthly_fixed(subs)
            if projected > spendable and pct < 100:
                week = today.isocalendar().week
                alerts.append(Alert("warning", f"pace:{ym}:w{week}",
                                    f"A este ritmo cerrarías el mes gastando ~{fmt(money(projected), cur)}, "
                                    f"por encima de tu disponible ({fmt(spendable, cur)})."))

    # 4) Gasto inusualmente alto para la categoría (aprendido)
    ticket = profile.stats.get("avg_ticket", {}).get(tx.category)
    if ticket and ticket.get("count", 0) >= 5:
        mean = Decimal(str(ticket["mean"]))
        if mean > 0 and tx.amount >= mean * 3:
            alerts.append(Alert("warning", f"unusual:{tx.id}",
                                f"Este gasto ({fmt(tx.amount, cur)}) es {tx.amount / mean:.1f}× lo que sueles "
                                f"gastar en {categories.label(tx.category)} ({fmt(money(mean), cur)})."))

    # 5) Categoría muy por encima de su promedio mensual (aprendido)
    baseline = profile.stats.get("monthly_avg", {}).get(tx.category)
    if baseline and profile.stats.get("months_observed", 0) >= 2 and not budget:
        base = Decimal(str(baseline))
        spent = t["by_category"].get(tx.category, ZERO)
        if base > 0 and spent > base * Decimal("1.5"):
            alerts.append(Alert("warning", f"baseline:{tx.category}:{ym}",
                                f"En {categories.label(tx.category)} llevas {fmt(spent, cur)}, un "
                                f"{(spent / base - 1) * 100:.0f}% más que tu promedio mensual "
                                f"({fmt(money(base), cur)})."))
    return alerts


# ---------------------------------------------------------------------------
# ¿Puedo salir / darme este gusto?
# ---------------------------------------------------------------------------

def affordability(profile: Profile, month_txs: list[Transaction], subs: list[Subscription],
                  today: date, cost: Decimal | None = None) -> dict[str, Any]:
    cur = profile.currency
    spendable = spendable_income(profile)
    _, last = month_bounds(today)
    days_left = (last - today).days + 1
    t = totals(month_txs)
    outing_spent = sum((t["by_category"].get(c, ZERO) for c in categories.OUTING_CATEGORIES), ZERO)

    tickets = profile.stats.get("avg_ticket", {})
    samples = [tickets[c] for c in categories.OUTING_CATEGORIES if c in tickets]
    n = sum(s["count"] for s in samples)
    typical_outing = money(sum(Decimal(str(s["mean"])) * s["count"] for s in samples) / n) if n else None
    estimated = cost if cost is not None else typical_outing

    result: dict[str, Any] = {
        "moneda": cur,
        "dias_restantes_mes": days_left,
        "gastado_en_salidas_este_mes": float(outing_spent),
        "costo_estimado_salida": float(estimated) if estimated is not None else None,
        "costo_estimado_fuente": "usuario" if cost is not None else ("historial" if typical_outing else None),
    }
    if profile.payday:
        result["dias_hasta_dia_de_pago"] = _days_until_payday(today, profile.payday)

    if spendable is None:
        result.update(veredicto="sin_datos",
                      razon="No conozco tus ingresos todavía; dímelos para poder responder con números.")
        return result

    committed = t["expense"] + pending_charges_this_month(subs, today)
    remaining = spendable - committed
    daily = money(remaining / days_left) if remaining > 0 else ZERO
    result.update(
        disponible_mes=float(spendable),
        comprometido_mes=float(committed),
        restante_mes=float(remaining),
        presupuesto_diario=float(daily),
    )
    outing_budget = sum((profile.budgets.get(c, ZERO) for c in categories.OUTING_CATEGORIES), ZERO)
    if outing_budget:
        result["presupuesto_salidas_restante"] = float(outing_budget - outing_spent)

    if remaining <= 0:
        verdict, reason = "no", "Ya gastaste lo disponible del mes sin tocar tu meta de ahorro."
    elif estimated is None:
        verdict, reason = "si", f"Tienes {fmt(daily, cur)} por día hasta fin de mes; no sé cuánto cuesta la salida."
    elif outing_budget and outing_spent + estimated > outing_budget:
        verdict, reason = "con_cuidado", "Te pasarías del presupuesto que fijaste para salidas."
    elif estimated <= daily:
        verdict, reason = "si", "Cabe en tu presupuesto diario."
    elif estimated <= daily * 3 and estimated <= remaining * Decimal("0.25"):
        verdict, reason = "con_cuidado", "Puedes, pero tendrías que gastar menos los próximos días."
    else:
        verdict, reason = "no", "Se come una parte grande de lo que te queda para el resto del mes."
    result.update(veredicto=verdict, razon=reason)
    return result


def _days_until_payday(today: date, payday: int) -> int:
    day = min(payday, calendar.monthrange(today.year, today.month)[1])
    target = today.replace(day=day)
    if target < today:
        nxt = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
        target = nxt.replace(day=min(payday, calendar.monthrange(nxt.year, nxt.month)[1]))
    return (target - today).days


# ---------------------------------------------------------------------------
# Resúmenes
# ---------------------------------------------------------------------------

def summary_payload(profile: Profile, txs: list[Transaction], start: date, end: date,
                    category: str | None = None) -> dict[str, Any]:
    if category:
        txs = [t for t in txs if t.category == category]
    t = totals(txs)
    biggest = sorted((x for x in txs if x.kind == "expense"), key=lambda x: -x.amount)[:5]
    return {
        "desde": start.isoformat(),
        "hasta": end.isoformat(),
        "moneda": profile.currency,
        "ingresos": float(t["income"]),
        "gastos": float(t["expense"]),
        "balance": float(t["net"]),
        "gasto_discrecional": float(t["discretionary"]),
        "por_categoria": {k: float(v) for k, v in t["by_category"].items()},
        "presupuestos": {k: float(v) for k, v in profile.budgets.items()},
        "mayores_gastos": [
            {"fecha": x.date, "monto": float(x.amount), "categoria": x.category, "descripcion": x.description}
            for x in biggest
        ],
        "cantidad_movimientos": len(txs),
    }


def summary_text(profile: Profile, txs: list[Transaction], subs: list[Subscription], today: date) -> str:
    """Resumen corto del mes para el chat (el detalle va en la mini app)."""
    cur = profile.currency
    t = totals(txs)
    lines = [f"<b>📊 {today.strftime('%m/%Y')}</b>",
             f"Ingresos: <b>{fmt(t['income'], cur)}</b>",
             f"Gastos: <b>{fmt(t['expense'], cur)}</b>"]
    spendable = spendable_income(profile)
    if spendable:
        committed = t["expense"] + pending_charges_this_month(subs, today)
        lines.append(f"Disponible restante: <b>{fmt(spendable - committed, cur)}</b>")
    if t["by_category"]:
        lines.append("")
        for cat, amount in list(t["by_category"].items())[:5]:
            budget = profile.budgets.get(cat)
            extra = f" / {fmt(budget, cur)}" if budget else ""
            lines.append(f"{categories.label(cat)}: {fmt(amount, cur)}{extra}")
    nxt = upcoming_charges(subs, today, 7)
    if nxt:
        lines.append("")
        lines.append("Próximos cobros: " + ", ".join(f"{s.name} ({s.next_charge[5:]})" for s in nxt))
    return "\n".join(lines)


def dashboard(profile: Profile, month_txs: list[Transaction], history_txs: list[Transaction],
              subs: list[Subscription], month: date, today: date) -> dict[str, Any]:
    """Datos completos para la mini app."""
    start, end = month_bounds(month)
    t = totals(month_txs)
    spendable = spendable_income(profile)
    is_current = start <= today <= end

    daily: dict[str, float] = defaultdict(float)
    for tx in month_txs:
        if tx.kind == "expense":
            daily[tx.date] += float(tx.amount)
    cumulative, acc = [], 0.0
    d = start
    while d <= end and (not is_current or d <= today):
        acc += daily.get(d.isoformat(), 0.0)
        cumulative.append({"date": d.isoformat(), "spent": round(daily.get(d.isoformat(), 0.0), 2),
                           "cumulative": round(acc, 2)})
        d += timedelta(days=1)

    monthly: dict[str, dict[str, float]] = defaultdict(lambda: {"income": 0.0, "expense": 0.0})
    for tx in history_txs:
        monthly[tx.date[:7]]["income" if tx.kind == "income" else "expense"] += float(tx.amount)

    weekday = [0.0] * 7
    for tx in history_txs:
        if tx.kind == "expense" and tx.source != "subscription":
            weekday[date.fromisoformat(tx.date).weekday()] += float(tx.amount)

    committed = t["expense"] + (pending_charges_this_month(subs, today) if is_current else ZERO)
    budgets = []
    for cat, limit in profile.budgets.items():
        spent = t["by_category"].get(cat, ZERO)
        budgets.append({"category": cat, "label": categories.label(cat), "limit": float(limit),
                        "spent": float(spent), "pct": float(spent / limit * 100) if limit else 0.0})

    projection = None
    if is_current and today.day >= 3:
        variable = t["expense"] - sum((x.amount for x in month_txs if x.source == "subscription"), ZERO)
        projection = float(money(variable / today.day * end.day + monthly_fixed(subs)))

    return {
        "profile": {
            "name": profile.first_name,
            "currency": profile.currency,
            "monthly_income": float(profile.monthly_income) if profile.monthly_income else None,
            "learned_income": profile.stats.get("avg_monthly_income"),
            "savings_goal_pct": float(profile.savings_goal_pct),
            "payday": profile.payday,
        },
        "month": start.strftime("%Y-%m"),
        "is_current_month": is_current,
        "totals": {
            "income": float(t["income"]),
            "expense": float(t["expense"]),
            "net": float(t["net"]),
            "discretionary": float(t["discretionary"]),
            "spendable": float(spendable) if spendable else None,
            "committed": float(committed),
            "remaining": float(spendable - committed) if spendable else None,
            "projection": projection,
            "fixed_monthly": float(monthly_fixed(subs)),
        },
        "categories": [
            {"category": k, "label": categories.label(k), "amount": float(v),
             "baseline": profile.stats.get("monthly_avg", {}).get(k)}
            for k, v in t["by_category"].items()
        ],
        "daily": cumulative,
        "monthly_history": [{"month": k, **v} for k, v in sorted(monthly.items())],
        "weekday": [round(v, 2) for v in weekday],
        "budgets": budgets,
        "subscriptions": [s.to_public() for s in sorted(subs, key=lambda s: s.next_charge)],
        "transactions": [tx.to_public() for tx in sorted(month_txs, key=lambda x: (x.date, x.created_at),
                                                         reverse=True)],
        "insights": profile.insights,
        "category_catalog": {k: categories.label(k) for k in categories.CATEGORIES},
    }
