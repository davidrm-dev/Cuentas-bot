import json
from datetime import date
from decimal import Decimal

import pytest

from cuentas import learning
from cuentas.categories import guess
from cuentas.ledger import Ledger
from cuentas.models import Transaction
from cuentas.parsing import quick_parse
from cuentas.tools import ToolExecutor, ToolInputError


def run(executor, name, **args):
    return json.loads(executor.run(name, args))


def test_register_multiple_movements_and_learn(repo, profile, now):
    ex = ToolExecutor(Ledger(repo, profile, now), source="voice")
    out = run(ex, "registrar_movimientos", movimientos=[
        {"tipo": "gasto", "monto": 18000, "descripcion": "Almuerzo", "categoria": "restaurantes",
         "comercio": "Crepes & Waffles"},
        {"tipo": "ingreso", "monto": 3000000, "descripcion": "Sueldo", "categoria": "salario"},
    ])
    assert len(out["registrados"]) == 2
    assert out["gasto_del_mes"] == 18000.0
    assert profile.merchant_categories["crepes & waffles"] == "restaurantes"
    assert profile.stats["avg_ticket"]["restaurantes"] == {"mean": 18000.0, "count": 1}
    assert profile.stats["usage"]["voice"] == 2
    assert all(t.date == "2026-10-15" for t in ex.recorded)


def test_register_fixes_inconsistent_category_and_future_date(repo, profile, now):
    ex = ToolExecutor(Ledger(repo, profile, now))
    run(ex, "registrar_movimientos", movimientos=[
        {"tipo": "ingreso", "monto": 50, "descripcion": "venta", "categoria": "ocio", "fecha": "2030-01-01"},
    ])
    tx = ex.recorded[0]
    assert tx.category == "otros_ingresos"
    assert tx.date == "2026-10-15"


def test_invalid_amount_rejected(repo, profile, now):
    ex = ToolExecutor(Ledger(repo, profile, now))
    with pytest.raises(ToolInputError):
        ex.run("registrar_movimientos", {"movimientos": [
            {"tipo": "gasto", "monto": -5, "descripcion": "x", "categoria": "otros"}]})


def test_alerts_are_deduplicated(repo, profile, now):
    profile.budgets["ocio"] = Decimal("100")
    ex = ToolExecutor(Ledger(repo, profile, now))
    for _ in range(3):
        run(ex, "registrar_movimientos", movimientos=[
            {"tipo": "gasto", "monto": 60, "descripcion": "bar", "categoria": "ocio"}])
    keys = [a.key for a in ex.alerts]
    assert keys.count("budget:ocio:2026-10:100") == 1


def test_correction_teaches_merchant(repo, profile, now):
    ex = ToolExecutor(Ledger(repo, profile, now))
    run(ex, "registrar_movimientos", movimientos=[
        {"tipo": "gasto", "monto": 30, "descripcion": "Rappi", "categoria": "restaurantes", "comercio": "Rappi"}])
    run(ex, "corregir_ultimo_movimiento", accion="cambiar_categoria", categoria="supermercado")
    assert profile.merchant_categories["rappi"] == "supermercado"
    assert "restaurantes" not in profile.stats["avg_ticket"]
    assert guess("pedido en rappi", profile.merchant_categories) == "supermercado"


def test_subscription_past_date_is_rolled_forward(repo, profile, now):
    ex = ToolExecutor(Ledger(repo, profile, now))
    out = run(ex, "registrar_suscripcion", nombre="Netflix", monto=45000, ciclo="monthly",
              proximo_cobro="2026-10-05")
    assert out["suscripcion"]["next_charge"] == "2026-11-05"
    out = run(ex, "registrar_suscripcion", nombre="netflix", monto=50000, ciclo="monthly",
              proximo_cobro="2026-11-05")
    assert out["actualizada"] is True
    assert len(repo.list_subscriptions(1)) == 1
    out = run(ex, "cancelar_suscripcion", nombre="Netflix")
    assert out["ahorro_mensual"] == 50000.0
    assert repo.list_subscriptions(1) == []


def test_profile_and_budget_tools(repo, profile, now):
    ex = ToolExecutor(Ledger(repo, profile, now))
    run(ex, "actualizar_perfil", ingreso_mensual=3000000, dia_de_pago=30, moneda="cop", meta_ahorro_pct=15)
    assert profile.monthly_income == Decimal("3000000.00") and profile.currency == "COP"
    run(ex, "fijar_presupuesto", categoria="ocio", monto=200000)
    assert profile.budgets == {"ocio": Decimal("200000.00")}
    run(ex, "fijar_presupuesto", categoria="ocio", monto=0)
    assert profile.budgets == {}
    with pytest.raises(ToolInputError):
        ex.run("actualizar_perfil", {"zona_horaria": "Marte/Base"})


def test_recompute_builds_baselines_and_insights(profile):
    history = []
    for month in ("2026-08", "2026-09"):
        history += [
            Transaction(user_id=1, kind="income", amount=Decimal("1000"), category="salario",
                        description="sueldo", date=f"{month}-01"),
            Transaction(user_id=1, kind="expense", amount=Decimal("300"), category="vivienda",
                        description="arriendo", date=f"{month}-02", necessity="esencial"),
            Transaction(user_id=1, kind="expense", amount=Decimal("100"), category="ocio",
                        description="bar", date=f"{month}-05"),  # sábado en sept., sábado/miércoles
        ]
    history.append(Transaction(user_id=1, kind="expense", amount=Decimal("999"), category="ocio",
                               description="mes en curso", date="2026-10-03"))
    learning.recompute(profile, history, date(2026, 10, 15))
    assert profile.stats["months_observed"] == 2
    assert profile.stats["monthly_avg"] == {"vivienda": 300.0, "ocio": 100.0}
    assert profile.stats["avg_monthly_income"] == 1000.0
    assert any("Ahorra en promedio el 60%" in i for i in profile.insights)
    assert "Patrones observados" in learning.context_for_llm(profile)


@pytest.mark.parametrize("text,kind,amount,category", [
    ("Gasté 12.500 en uber", "expense", Decimal("12500.00"), "transporte"),
    ("pagué 20 mil del almuerzo", "expense", Decimal("20000.00"), "restaurantes"),
    ("me pagaron el sueldo 2500000", "income", Decimal("2500000.00"), "salario"),
    ("compré 3,5 en el super", "expense", Decimal("3.50"), "supermercado"),
])
def test_quick_parse(text, kind, amount, category):
    entry = quick_parse(text)
    assert entry is not None
    assert (entry.kind, entry.amount, entry.category) == (kind, amount, category)


def test_quick_parse_ignores_questions():
    assert quick_parse("¿cuánto llevo este mes?") is None
