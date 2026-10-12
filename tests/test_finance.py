from datetime import date
from decimal import Decimal

from cuentas import finance
from cuentas.models import Subscription, Transaction, add_cycle

TODAY = date(2026, 10, 15)


def tx(amount, category="restaurantes", kind="expense", day="2026-10-10", **kw):
    return Transaction(user_id=1, kind=kind, amount=Decimal(str(amount)), category=category,
                       description=category, date=day, **kw)


def test_totals_by_category_and_discretionary():
    t = finance.totals([tx(100), tx(50, "supermercado", necessity="esencial"),
                        tx(1000, "salario", kind="income")])
    assert t["expense"] == Decimal("150.00")
    assert t["income"] == Decimal("1000.00")
    assert t["discretionary"] == Decimal("100.00")
    assert list(t["by_category"]) == ["restaurantes", "supermercado"]


def test_budget_alert_levels(profile):
    profile.budgets["restaurantes"] = Decimal("100")
    txs = [tx(70), tx(15)]
    alerts = finance.check_alerts(profile, txs[-1], txs, [], TODAY)
    assert [a.level for a in alerts] == ["warning"]
    txs.append(tx(30))
    alerts = finance.check_alerts(profile, txs[-1], txs, [], TODAY)
    assert alerts[0].level == "critical" and "Superaste" in alerts[0].message


def test_spendable_alert_counts_pending_subscriptions(profile):
    profile.monthly_income = Decimal("1000")
    profile.savings_goal_pct = Decimal("20")  # disponible 800
    sub = Subscription(user_id=1, name="Arriendo", amount=Decimal("500"), cycle="monthly",
                       next_charge="2026-10-30", category="vivienda")
    txs = [tx(350)]
    alerts = finance.check_alerts(profile, txs[0], txs, [sub], TODAY)
    assert any(a.key == "spendable:2026-10:100" and a.level == "critical" for a in alerts)


def test_unusual_expense_uses_learned_ticket(profile):
    profile.stats["avg_ticket"] = {"restaurantes": {"mean": 20.0, "count": 8}}
    alerts = finance.check_alerts(profile, tx(90), [tx(90)], [], TODAY)
    assert any(a.key.startswith("unusual:") for a in alerts)


def test_income_does_not_alert(profile):
    profile.budgets["restaurantes"] = Decimal("1")
    assert finance.check_alerts(profile, tx(10, "salario", kind="income"), [], [], TODAY) == []


def test_affordability_verdicts(profile):
    profile.monthly_income = Decimal("3100")
    profile.savings_goal_pct = Decimal("0")
    txs = [tx(1500, "vivienda")]  # restan 1600 para 17 días -> ~94/día
    result = finance.affordability(profile, txs, [], TODAY, Decimal("50"))
    assert result["veredicto"] == "si"
    assert result["dias_restantes_mes"] == 17
    result = finance.affordability(profile, txs, [], TODAY, Decimal("250"))
    assert result["veredicto"] == "con_cuidado"
    result = finance.affordability(profile, txs, [], TODAY, Decimal("900"))
    assert result["veredicto"] == "no"


def test_affordability_without_income_asks_for_it(profile):
    assert finance.affordability(profile, [], [], TODAY)["veredicto"] == "sin_datos"


def test_affordability_uses_learned_income_and_outing_ticket(profile):
    profile.stats["avg_monthly_income"] = 2000
    profile.stats["avg_ticket"] = {"ocio": {"mean": 40.0, "count": 4}, "restaurantes": {"mean": 20.0, "count": 4}}
    result = finance.affordability(profile, [], [], TODAY)
    assert result["costo_estimado_salida"] == 30.0
    assert result["costo_estimado_fuente"] == "historial"
    assert result["veredicto"] == "si"


def test_add_cycle_keeps_month_end_anchor():
    assert add_cycle(date(2026, 1, 31), "monthly", 31) == date(2026, 2, 28)
    assert add_cycle(date(2026, 2, 28), "monthly", 31) == date(2026, 3, 31)
    assert add_cycle(date(2026, 12, 15), "monthly") == date(2027, 1, 15)
    assert add_cycle(date(2026, 10, 15), "weekly") == date(2026, 10, 22)


def test_pending_weekly_charges():
    sub = Subscription(user_id=1, name="Clases", amount=Decimal("10"), cycle="weekly", next_charge="2026-10-16")
    assert finance.pending_charges_this_month([sub], TODAY) == Decimal("30")  # 16, 23 y 30


def test_dashboard_shape(profile):
    profile.budgets["restaurantes"] = Decimal("200")
    txs = [tx(100), tx(1000, "salario", kind="income", day="2026-10-01")]
    data = finance.dashboard(profile, txs, txs, [], TODAY, TODAY)
    assert data["totals"]["expense"] == 100.0
    assert data["budgets"][0]["pct"] == 50.0
    assert data["daily"][-1]["date"] == "2026-10-15"
    assert data["daily"][-1]["cumulative"] == 100.0


def test_fmt():
    assert finance.fmt(Decimal("12500"), "COP") == "$12.500 COP"
    assert finance.fmt(Decimal("12.5"), "USD") == "$12,50 USD"
