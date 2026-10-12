import json
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from cuentas import bot, finance
from cuentas.handlers import scheduler
from cuentas.ledger import Ledger
from cuentas.models import SavingsPocket
from cuentas.tools import ToolExecutor, ToolInputError

TODAY = date(2026, 10, 15)


def run(ex, **args):
    return json.loads(ex.run("registrar_ahorro", args))


def test_deposit_goal_and_monthly_quota(repo, profile, now):
    Ledger(repo, profile, now).set_balance("nequi", Decimal("500000"))
    ex = ToolExecutor(Ledger(repo, profile, now), user_text="quiero juntar 1.800.000 para un viaje en febrero")
    run(ex, bolsillo="Viaje", accion="configurar", meta=1800000, fecha_meta="2027-02-15")
    ex = ToolExecutor(Ledger(repo, profile, now), user_text="pasé 200 mil de nequi al ahorro del viaje")
    out = run(ex, bolsillo="viaje", accion="depositar", monto=200000, cuenta_origen="nequi", guardado_en="nequi",
              lugar="bolsillo")
    pocket = repo.list_pockets(1)[0]
    assert pocket.balance == Decimal("200000") and pocket.account == "nequi" and len(repo.list_pockets(1)) == 1
    assert profile.accounts["nequi"]["saldo"] == Decimal("300000")  # salió de Nequi, no es gasto
    assert repo.list_transactions(1, "2026-10-01", "2026-10-31") == []
    assert out["progreso_pct"] == pytest.approx(11.1)
    assert "aparta $400.000 COP al mes" in ex.notes[0]  # falta 1.600.000 en 4 meses


def test_withdraw_never_goes_negative_and_guard(repo, profile, now):
    ex = ToolExecutor(Ledger(repo, profile, now), user_text="tengo 300 mil ahorrados para emergencias")
    run(ex, bolsillo="Emergencias", accion="fijar_saldo", monto=300000, emergencia=True)
    ex = ToolExecutor(Ledger(repo, profile, now), user_text="saqué 500 mil del ahorro de emergencias")
    run(ex, bolsillo="emergencias", accion="retirar", monto=500000)
    assert repo.list_pockets(1)[0].balance == 0
    with pytest.raises(ToolInputError):  # monto que el usuario no dijo
        ToolExecutor(Ledger(repo, profile, now), user_text="ahorré algo hoy").run(
            "registrar_ahorro", {"bolsillo": "Emergencias", "accion": "depositar", "monto": 50000})


def test_savings_effect_on_available(profile):
    profile.monthly_income = Decimal("2000000")  # meta 10% = 200.000
    pocket = SavingsPocket(user_id=1, name="Viaje")
    pocket.move(Decimal("350000"), "2026-10-05")
    assert finance.savings_effect(profile, [pocket], TODAY) == Decimal("150000")  # solo lo que pasa de la meta
    emergency = SavingsPocket(user_id=1, name="Emergencias", balance=Decimal("500000"))  # saldo previo
    emergency.move(Decimal("-400000"), "2026-10-10")  # retiro: vuelve a estar disponible
    assert finance.savings_effect(profile, [pocket, emergency], TODAY) == Decimal("-50000")
    old = SavingsPocket(user_id=1, name="Viejo", balance=Decimal("900000"))  # saldo previo: no es flujo del mes
    assert finance.savings_flow([old], *finance.month_bounds(TODAY)) == 0


def test_summary_rate_and_emergency_fund(profile):
    profile.stats["avg_monthly_expense"] = 1000000
    cdt = SavingsPocket(user_id=1, name="CDT", balance=Decimal("1000000"), rate_ea=Decimal("12"), emergency=True)
    summary = finance.savings_summary(profile, [cdt], TODAY)
    assert summary["bolsillos"][0]["rendimiento_mensual"] == pytest.approx(9488.79, abs=0.02)
    fe = summary["fondo_emergencia"]
    assert fe["meses_cubiertos"] == 1.0 and fe["recomendado_minimo"] == 3000000
    text = finance.savings_text(summary)
    assert "Rinde ~$9.488,79 COP al mes (12% EA)" in text and "cubres 1 meses" in text


def test_ahorro_command_and_payday_reminder(repo, tg, profile):
    profile.payday = 15
    profile.monthly_income = Decimal("2000000")
    pocket = SavingsPocket(user_id=1, name="Viaje", goal=Decimal("1200000"), goal_date="2027-01-15")
    repo.save_pocket(pocket)
    bot.handle_command(tg, repo, profile, 1, "/ahorro", False)
    assert "Viaje" in tg.sent[0]["text"]
    tg.sent.clear()
    scheduler.run_for_user(repo, tg, profile, [], datetime(2026, 10, 15, 13, tzinfo=ZoneInfo("UTC")))
    payday = [m["text"] for m in tg.sent if "Día de pago" in m["text"]]
    assert payday and "$200.000 COP" in payday[0] and "Viaje: $400.000 COP" in payday[0]
