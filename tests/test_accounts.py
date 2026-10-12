import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from cuentas import assistant, bot, converse, config
from cuentas.handlers import processor
from cuentas.ledger import Ledger
from cuentas.models import Transaction
from cuentas.tools import ToolExecutor, ToolInputError


def tx(amount, account="", day="2026-10-15", kind="expense", desc="algo"):
    return Transaction(user_id=1, kind=kind, amount=Decimal(amount), category="otros", description=desc,
                       date=day, account=account)


def test_balances_follow_movements(repo, profile, now):
    ledger = Ledger(repo, profile, now)
    ledger.set_balance("nequi", Decimal("300000"))
    t = tx("20000", "nequi")
    ledger.record(t)
    assert profile.accounts["nequi"]["saldo"] == Decimal("280000")
    ledger.record(tx("50000", "nequi", kind="income"))
    assert profile.accounts["nequi"]["saldo"] == Decimal("330000")
    ledger.change_account(t, "efectivo")  # efectivo no lleva saldo: solo se devuelve a Nequi
    assert profile.accounts["nequi"]["saldo"] == Decimal("350000")
    ledger.transfer("nequi", "efectivo", Decimal("50000"))
    assert profile.accounts["nequi"]["saldo"] == Decimal("300000") and "efectivo" not in profile.accounts
    ledger.change_account(t, "nequi")
    ledger.delete(t)
    assert profile.accounts["nequi"]["saldo"] == Decimal("300000")


def test_default_account(repo, profile, now):
    profile.default_account = "daviplata"
    t = tx("1000")
    Ledger(repo, profile, now).record(t)
    assert t.account == "daviplata"


def test_fast_path_with_account(monkeypatch, repo, tg, profile):
    monkeypatch.setattr(assistant, "_client", lambda: (_ for _ in ()).throw(AssertionError("sin IA")))
    Ledger(repo, profile).set_balance("nequi", Decimal("100000"))
    bot.handle_conversation(tg, repo, profile, 1, "gasté 12 mil en uber con nequi")
    t = repo.last_transaction(1)
    assert (t.account, t.description) == ("nequi", "Uber")
    assert "Nequi" in tg.sent[0]["text"] and "$88.000 COP" in tg.sent[0]["text"]


def test_account_tools(repo, profile, now):
    ex = ToolExecutor(Ledger(repo, profile, now), user_text="tengo 500 mil en la Nu")
    ex.run("actualizar_saldo_cuenta", {"cuenta": "nu", "saldo": 500000})
    ex.run("registrar_movimientos", {"movimientos": [
        {"tipo": "gasto", "monto": 40000, "descripcion": "Cine", "categoria": "ocio", "cuenta": "nu"}]})
    out = json.loads(ex.run("consultar_cuentas", {}))
    assert out["saldos"] == {"🟣 Nu": 460000.0}
    assert out["gastado_este_mes_por_cuenta"] == {"🟣 Nu": 40000.0}
    with pytest.raises(ToolInputError):
        ex.run("transferir_entre_cuentas", {"desde": "nu", "hacia": "nu", "monto": 1})
    json.loads(ex.run("actualizar_perfil", {"cuenta_principal": "nequi"}))
    assert profile.default_account == "nequi"


SHOT = {"app": "nequi", "saldo_texto": "$ 250.000,00", "movimientos": [
    {"fecha": "2026-10-14", "descripcion": "Rappi", "monto_texto": "-$35.000,00", "tipo": "gasto"},
    {"fecha": "2026-10-15", "descripcion": "Uber", "monto_texto": "-$ 12.000", "tipo": "gasto"},
    {"fecha": "2026-08-01", "descripcion": "Viejo", "monto_texto": "$9.000", "tipo": "gasto"},
    {"fecha": "2026-10-13", "descripcion": "Pago de Juan", "monto_texto": "+$ 50.000,00", "tipo": "ingreso"},
]}


def test_import_screenshot_dedupes_and_undo(repo, tg, profile, now):
    ledger = Ledger(repo, profile, now)
    ledger.record(tx("12000", "nequi", day="2026-10-14", desc="uber"))  # ya registrado (±1 día)
    ex = ToolExecutor(ledger, source="image")
    out = json.loads(ex.run("importar_pantallazo", SHOT))
    assert (out["registrados"], out["duplicados_omitidos"], out["antiguos_omitidos"]) == (2, 1, 1)
    assert profile.accounts["nequi"]["saldo"] == Decimal("250000")
    assert "Leí tu pantallazo de 💜 Nequi" in ex.notes[0] and "Omití 1 que ya tenías" in ex.notes[0]
    assert len(repo.list_transactions(1, "2026-10-01", "2026-10-31")) == 3
    msg = {"chat": {"id": 1}, "message_id": 3}
    bot.handle_callback(tg, repo, {"id": "x", "from": {"id": 1}, "data": f"ui:{ex.import_id}", "message": msg})
    assert len(repo.list_transactions(1, "2026-10-01", "2026-10-31")) == 1
    assert "nequi" not in profile.accounts  # no llevaba saldo antes de importar
    assert "Deshice la importación" in tg.sent[-1]["text"]


def test_import_only_from_images(repo, profile, now):
    with pytest.raises(ToolInputError):
        ToolExecutor(Ledger(repo, profile, now), source="text").run("importar_pantallazo", SHOT)


def test_photo_goes_to_model_with_image(monkeypatch, repo, tg, profile):
    calls = []

    class Fake:
        def create(self, **kw):
            calls.append(kw)
            if len(calls) == 1:
                use = SimpleNamespace(type="tool_use", id="i1", name="importar_pantallazo", input=SHOT)
                return SimpleNamespace(stop_reason="tool_use", content=[use], usage=None)
            return SimpleNamespace(stop_reason="end_turn", usage=None,
                                   content=[SimpleNamespace(type="text", text="Listo, importado.")])

    monkeypatch.setattr(assistant, "_client", lambda: SimpleNamespace(beta=SimpleNamespace(messages=Fake())))
    tg.download_file = lambda file_id: b"\x89PNG..."
    update = {"update_id": 5, "message": {"message_id": 7, "from": {"id": 1}, "chat": {"id": 1, "type": "private"},
                                          "photo": [{"file_id": "small"}, {"file_id": "big", "file_size": 1000}],
                                          "caption": "mi nequi"}}
    processor.process_update(repo, tg, update)
    blocks = calls[0]["messages"][0]["content"]
    assert blocks[1]["type"] == "image" and blocks[2]["text"] == "mi nequi"
    assert tg.sent[0]["text"].startswith("📸") and tg.sent[0]["keyboard"][0][0]["callback_data"].startswith("ui:")
    assert tg.sent[1]["text"] == "Listo, importado."


def test_nova_receives_image(monkeypatch, repo, profile, now):
    seen = {}

    class Fake:
        def converse(self, **kw):
            seen.update(kw)
            return {"stopReason": "end_turn", "usage": {}, "output": {"message": {"role": "assistant",
                                                                                  "content": [{"text": "ok"}]}}}

    monkeypatch.setattr(config, "LLM_PROVIDER", "nova")
    monkeypatch.setattr(converse, "_client", lambda: Fake())
    assistant.reply(Ledger(repo, profile, now), "lee esto", source="image", image=(b"img", "png"))
    assert seen["messages"][0]["content"][1] == {"image": {"format": "png", "source": {"bytes": b"img"}}}


@pytest.mark.parametrize("text,expected", [
    ("-$21.500,00", "21500.00"), ("$ 7.958", "7958.00"), ("19.558,74", "19558.74"), ("US$ 5.00", "5.00"),
    ("1,955.87", "1955.87"), ("$1.250.000,00", "1250000.00"), ("0,90", "0.90"), ("$ 1.234", "1234.00"),
])
def test_parse_screen_amount(text, expected):
    from cuentas.parsing import parse_screen_amount

    assert parse_screen_amount(text) == Decimal(expected)


def test_import_flags_outliers_and_reads_text_amounts(repo, profile, now):
    ex = ToolExecutor(Ledger(repo, profile, now), source="image")
    out = json.loads(ex.run("importar_pantallazo", {"app": "nequi", "movimientos": [
        {"fecha": "2026-10-14", "descripcion": "Royal Films", "monto_texto": "-$21.500,00", "tipo": "gasto"},
        {"fecha": "2026-10-14", "descripcion": "Uber", "monto_texto": "-$7.958", "tipo": "gasto"},
        {"fecha": "2026-10-13", "descripcion": "Tienda", "monto_texto": "-$12.300", "tipo": "gasto"},
        {"fecha": "2026-10-13", "descripcion": "Moto", "monto_texto": "-$102.000.000", "tipo": "gasto"},
        {"fecha": "2026-10-13", "descripcion": "Ilegible", "monto_texto": "$ --", "tipo": "gasto"},
    ]}))
    amounts = sorted(t.amount for t in ex.recorded)
    assert amounts == [Decimal("7958"), Decimal("12300"), Decimal("21500"), Decimal("102000000")]
    assert "⚠️ Revisa estos montos" in ex.notes[0] and "Moto" in ex.notes[0]
    assert "No pude leer el monto de 1" in ex.notes[0] and out["registrados"] == 4


def test_import_links_movements_told_by_voice_later(repo, profile, now):
    ledger = Ledger(repo, profile, now)
    told = tx("21500", day="2026-10-12", desc="algo")  # lo contó el 12, pero fue el 4
    told.source = "voice"
    ledger.record(told)
    ex = ToolExecutor(ledger, source="image")
    out = json.loads(ex.run("importar_pantallazo", {"app": "nequi", "movimientos": [
        {"fecha": "2026-10-04", "descripcion": "Pagaste en ROYAL FILMS", "monto_texto": "-$21.500,00", "tipo": "gasto"}]}))
    txs = repo.list_transactions(1, "2026-10-01", "2026-10-31")
    assert out["registrados"] == 0 and len(txs) == 1
    assert (txs[0].date, txs[0].account, txs[0].description) == ("2026-10-04", "nequi", "algo")
    assert "Ajusté 1 que me habías contado antes" in ex.notes[0]


def test_guards_block_unrequested_changes(repo, profile, now):
    ledger = Ledger(repo, profile, now)
    ex = ToolExecutor(ledger, user_text="Recibí 20000 de un arreglo de un computador en efectivo")
    with pytest.raises(ToolInputError):  # Nova inventó el saldo de Nequi
        ex.run("actualizar_saldo_cuenta", {"cuenta": "nequi", "saldo": 70000})
    ex.run("registrar_movimientos", {"movimientos": [
        {"tipo": "ingreso", "monto": 20000, "descripcion": "arreglo", "categoria": "otros_ingresos", "cuenta": "efectivo"}]})
    assert "nequi" not in profile.accounts and len(ex.recorded) == 1

    plan = ToolExecutor(ledger, user_text="¿Cuánto les pido a mis papás si gasto como 30 mil al día?")
    with pytest.raises(ToolInputError):
        plan.run("registrar_movimientos", {"movimientos": [
            {"tipo": "gasto", "monto": 30000, "descripcion": "Gasto diario habitual", "categoria": "otros"}]})
    with pytest.raises(ToolInputError):
        plan.run("registrar_suscripcion", {"nombre": "Gasto diario", "monto": 30000, "ciclo": "monthly",
                                           "proximo_cobro": "2026-10-16"})

    ok = ToolExecutor(ledger, user_text="pago Spotify 17 mil cada mes, el 20")
    ok.run("registrar_suscripcion", {"nombre": "Spotify", "monto": 17000, "ciclo": "monthly",
                                     "proximo_cobro": "2026-10-20"})
    ToolExecutor(ledger, user_text="tengo 23.927 en nequi").run("actualizar_saldo_cuenta",
                                                                 {"cuenta": "nequi", "saldo": 23927})
    assert profile.accounts["nequi"]["saldo"] == Decimal("23927")
