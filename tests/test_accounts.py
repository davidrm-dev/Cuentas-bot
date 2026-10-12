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
    ex = ToolExecutor(Ledger(repo, profile, now))
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


SHOT = {"app": "nequi", "saldo": 250000, "movimientos": [
    {"fecha": "2026-10-14", "descripcion": "Rappi", "monto": 35000, "tipo": "gasto"},
    {"fecha": "2026-10-15", "descripcion": "Uber", "monto": 12000, "tipo": "gasto"},
    {"fecha": "2026-08-01", "descripcion": "Viejo", "monto": 9000, "tipo": "gasto"},
    {"fecha": "2026-10-13", "descripcion": "Pago de Juan", "monto": 50000, "tipo": "ingreso"},
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
