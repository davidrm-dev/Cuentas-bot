import json
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from cuentas import bot, finance
from cuentas.handlers import scheduler
from cuentas.ledger import Ledger
from cuentas.models import Debt, Transaction
from cuentas.parsing import confident_parse
from cuentas.tools import ToolExecutor

TODAY = date(2026, 10, 15)


def run(ex, name, **args):
    return json.loads(ex.run(name, args))


def test_lend_partial_payment_and_close(repo, profile, now):
    ex = ToolExecutor(Ledger(repo, profile, now))
    out = run(ex, "registrar_prestamo", tipo="me_deben", persona="Juan", monto=50000, fecha_limite="2026-10-20")
    assert out["registrado"]["balance"] == 50000.0
    out = run(ex, "registrar_abono", persona="juan", monto=20000)
    assert out["saldo_pendiente"] == 30000.0 and not out["pagado_completo"]
    out = run(ex, "registrar_abono", persona="Juan", todo=True)
    assert out["pagado_completo"] and repo.list_debts(1) == []
    assert run(ex, "registrar_abono", persona="Pedro")["error"]


def test_debt_cash_flow_and_committed(profile):
    profile.monthly_income = Decimal("2000000")
    lent = Debt(user_id=1, direction="me_deben", person="Juan", amount=Decimal("100000"), date="2026-10-03")
    lent.add_payment(Decimal("40000"), "2026-10-10")
    owe = Debt(user_id=1, direction="debo", person="Papá", amount=Decimal("300000"), date="2026-09-01",
               due_date="2026-10-28", moved_money=False)
    start, end = finance.month_bounds(TODAY)
    assert finance.debt_cash_flow([lent, owe], start, end) == Decimal("-60000")  # prestó 100, le pagaron 40
    assert finance.payables_due([owe], end) == Decimal("300000")
    committed = finance.committed_month(profile, [], [], TODAY, [lent, owe])
    assert committed == Decimal("360000")


def test_reserves_set_aside_gasoline(repo, profile, now):
    ex = ToolExecutor(Ledger(repo, profile, now))
    run(ex, "fijar_gasto_fijo", nombre="Gasolina", monto_mensual=200000)
    assert profile.reserves["gasolina"]["categoria"] == "transporte"
    Ledger(repo, profile, now).record(Transaction(user_id=1, kind="expense", amount=Decimal("70000"),
                                                  category="transporte", description="Gasolina", date="2026-10-15"))
    Ledger(repo, profile, now).record(Transaction(user_id=1, kind="expense", amount=Decimal("15000"),
                                                  category="transporte", description="Uber", date="2026-10-15"))
    pending = finance.reserves_remaining(profile, repo.list_transactions(1, "2026-10-01", "2026-10-31"))
    assert pending[0]["gastado"] == Decimal("70000") and pending[0]["pendiente"] == Decimal("130000")
    profile.monthly_income = Decimal("1000000")
    profile.savings_goal_pct = Decimal("0")
    afford = run(ex, "evaluar_salida_o_compra")
    assert afford["apartado_para_fijos"] == {"gasolina": 130000.0}
    assert afford["restante_mes"] == 1000000 - 85000 - 130000


def test_plan_includes_debts_and_reserves(profile):
    profile.reserves = {"gasolina": {"monto": Decimal("200000"), "categoria": "transporte"}}
    owe = Debt(user_id=1, direction="debo", person="Papá", amount=Decimal("100000"), date="2026-10-01",
               due_date="2026-10-25")
    owed = Debt(user_id=1, direction="me_deben", person="Juan", amount=Decimal("50000"), date="2026-09-01",
                moved_money=False)
    plan = finance.plan_month(profile, [], [], TODAY, [], cash_now=Decimal("0"),
                              daily_estimate=Decimal("10000"), buffer_pct=Decimal("0"), debts=[owe, owed])
    assert plan["deudas_que_vencen"] == 100000.0 and plan["apartado_fijos"] == {"gasolina": 200000.0}
    assert plan["monto_a_pedir"] == 470000.0  # 17×10k + 100k + 200k
    text = finance.plan_text(plan)
    assert "Apartado para gasolina: $200.000 COP" in text and "Te deben (no lo conté como seguro): Juan" in text


def test_debt_reminders_schedule(repo, tg, profile):
    profile.monthly_income = Decimal("2000000")
    owe = Debt(user_id=1, direction="debo", person="Papá", amount=Decimal("300000"), date="2026-10-01",
               due_date="2026-10-18")
    lent = Debt(user_id=1, direction="me_deben", person="Juan", amount=Decimal("50000"), date="2026-10-01",
                due_date="2026-10-14")
    repo.save_debt(owe)
    repo.save_debt(lent)
    now = datetime(2026, 10, 15, 13, 0, tzinfo=ZoneInfo("UTC"))  # 8:00 en Bogotá
    scheduler.run_for_user(repo, tg, profile, [], now)
    texts = [m["text"] for m in tg.sent]
    assert any("Le debes <b>$300.000 COP</b> a <b>Papá</b>; vence en 3 días" in t and "te alcanza" in t for t in texts)
    assert any("<b>Juan</b> te debe" in t and "hace 1 día" in t for t in texts)
    assert all(m["keyboard"][0][0]["callback_data"].startswith("dp:") for m in tg.sent if "debe" in m["text"])
    tg.sent.clear()
    scheduler.run_for_user(repo, tg, profile, [], now)  # no se repite el mismo día
    assert not any("debe" in m["text"] for m in tg.sent)


def test_debt_buttons(repo, tg, profile):
    debt = Debt(user_id=1, direction="debo", person="Papá", amount=Decimal("300000"), date="2026-10-01",
                due_date="2026-10-18")
    repo.save_debt(debt)
    msg = {"chat": {"id": 1}, "message_id": 9}
    bot.handle_callback(tg, repo, {"id": "c", "from": {"id": 1}, "data": f"dz:{debt.id}", "message": msg})
    assert debt.due_date > "2026-10-18"
    bot.handle_callback(tg, repo, {"id": "c", "from": {"id": 1}, "data": f"dp:{debt.id}", "message": msg})
    assert debt.closed and "le pagaste a Papá" in tg.sent[-1]["text"]


def test_loan_phrases_skip_fast_path():
    for text in ("le presté 50 mil a juan", "le pagué 50 mil a mi papá", "juan me pagó 20 mil", "debo 30 mil"):
        assert confident_parse(text) is None


def test_prestamos_command(repo, tg, profile):
    repo.save_debt(Debt(user_id=1, direction="me_deben", person="Juan", amount=Decimal("50000"), date="2026-10-01"))
    bot.handle_command(tg, repo, profile, 1, "/prestamos", False)
    assert "Te deben" in tg.sent[0]["text"] and "Juan" in tg.sent[0]["text"]
