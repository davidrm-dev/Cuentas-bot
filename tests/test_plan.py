import json
from datetime import date
from decimal import Decimal

from cuentas import finance
from cuentas.ledger import Ledger
from cuentas.models import Subscription, Transaction
from cuentas.tools import ToolExecutor

TODAY = date(2026, 10, 15)


def tx(amount, category, kind="expense", day="2026-10-05", **kw):
    return Transaction(user_id=1, kind=kind, amount=Decimal(amount), category=category,
                       description=category, date=day, **kw)


def test_nice_ceil():
    assert finance.nice_ceil(Decimal("437512")) == Decimal("440000")
    assert finance.nice_ceil(Decimal("90001")) == Decimal("91000")
    assert finance.nice_ceil(Decimal("-5")) == 0


def test_loan_until_payday_with_events(profile):
    profile.monthly_income = Decimal("2000000")
    profile.payday = 30
    # Historial aprendido: 30.000/día en gasto variable (arriendo no cuenta como día a día)
    profile.stats.update(months_observed=2, monthly_avg={"supermercado": 450000, "transporte": 300000,
                                                          "restaurantes": 150000, "vivienda": 800000})
    month = [tx("2000000", "salario", kind="income", day="2026-10-01"), tx("800000", "vivienda"),
             tx("700000", "supermercado")]
    subs = [Subscription(user_id=1, name="Netflix", amount=Decimal("45000"), cycle="monthly", next_charge="2026-10-20")]
    events = [{"descripcion": "Concierto", "monto": Decimal("250000"), "fecha": "2026-10-25"},
              {"descripcion": "Boda prima", "monto": Decimal("150000")}]
    plan = finance.plan_month(profile, month, subs, TODAY, events)
    # 15 al 29 de octubre = 15 días × 30.000 = 450.000; + 45.000 Netflix + 400.000 eventos = 895.000
    assert plan["dias"] == 15 and plan["gasto_diario"] == 30000.0
    assert plan["gasto_dia_a_dia"] == 450000.0 and plan["cobros_fijos_pendientes"] == 45000.0
    assert plan["disponible"] == 500000.0  # 2.000.000 - 1.500.000 gastados
    # 895.000 + 10% = 984.500; faltan 484.500 -> pedir 490.000
    assert plan["faltante_exacto"] == 484500.0 and plan["monto_a_pedir"] == 490000.0
    assert plan["monto_a_pedir_austero"] < plan["monto_a_pedir"]  # recortar restaurantes ayuda
    assert plan["meses_para_devolver"] == 3  # cuotas de 200.000 (10% del ingreso)


def test_plan_uses_cash_and_flags_events_after_payday(profile):
    profile.payday = 20
    plan = finance.plan_month(profile, [], [], TODAY,
                              [{"descripcion": "Viaje", "monto": Decimal("300000"), "fecha": "2026-10-28"}],
                              cash_now=Decimal("100000"), daily_estimate=Decimal("20000"))
    assert plan["dias"] == 5 and plan["total_eventos"] == 0
    assert plan["eventos_despues_del_pago"] == ["Viaje"]
    assert plan["monto_a_pedir"] == 10000.0  # 5 × 20.000 + 10% = 110.000; tiene 100.000


def test_plan_asks_for_missing_data(profile):
    plan = finance.plan_month(profile, [], [], TODAY, [{"descripcion": "Cine", "monto": Decimal("40000")}])
    assert plan["estado"] == "faltan_datos" and len(plan["faltan"]) == 2


def test_plan_tool(repo, profile, now):
    out = json.loads(ToolExecutor(Ledger(repo, profile, now)).run("planificar_mes", {
        "eventos": [{"descripcion": "Grado de mi hermano", "monto": 200000, "fecha": "2026-10-24"}],
        "saldo_actual": 300000, "gasto_diario_estimado": 25000}))
    assert out["estado"] == "ok" and out["total_eventos"] == 200000.0 and out["monto_a_pedir"] > 0


def test_plan_text_is_local_format(profile):
    plan = finance.plan_month(profile, [], [], TODAY,
                              [{"descripcion": "Concierto", "monto": Decimal("250000"), "fecha": "2026-10-25"},
                               {"descripcion": "Boda prima", "monto": Decimal("150000")}],
                              cash_now=Decimal("400000"), daily_estimate=Decimal("30000"))
    text = finance.plan_text(plan)
    assert "17 × $30.000 COP = $510.000 COP" in text
    assert "Concierto (25/10): $250.000 COP" in text
    assert "Pide $610.000 COP" in text  # (510k + 400k) × 1,1 = 1.001.000 - 400k = 601.000 -> 610.000
    assert "," not in text.split("Pide")[1].split("\n")[0]


def test_plan_note_sent_before_advice(monkeypatch, repo, tg, profile):
    from types import SimpleNamespace

    from cuentas import assistant, bot

    class Fake:
        n = 0

        def create(self, **kw):
            self.n += 1
            if self.n == 1:
                block = SimpleNamespace(type="tool_use", id="p1", name="planificar_mes", input={
                    "eventos": [{"descripcion": "Cine", "monto": 40000}], "saldo_actual": 10000,
                    "gasto_diario_estimado": 20000})
                return SimpleNamespace(stop_reason="tool_use", content=[block], usage=None)
            return SimpleNamespace(stop_reason="end_turn", usage=None,
                                   content=[SimpleNamespace(type="text", text="Pídelo y devuélvelo en 2 meses.")])

    monkeypatch.setattr(assistant, "_client", lambda: SimpleNamespace(beta=SimpleNamespace(messages=Fake())))
    bot.handle_conversation(tg, repo, profile, 1, "¿cuánto le pido a mis papás para el cine?")
    assert tg.sent[0]["text"].startswith("🧮") and "Pide" in tg.sent[0]["text"]
    assert tg.sent[1]["text"] == "Pídelo y devuélvelo en 2 meses."
