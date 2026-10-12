import hashlib
import hmac
import json
import time
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from cuentas import assistant, bot
from cuentas.handlers import scheduler
from cuentas.handlers.transcribe_done import parse_job_name
from cuentas.ledger import Ledger
from cuentas.models import Subscription
from cuentas.webapp_auth import InvalidInitData, validate_init_data

TOKEN = "123:TEST"


def signed_init_data(user_id=1, auth_date=None, token=TOKEN):
    fields = {"auth_date": str(auth_date or int(time.time())), "query_id": "AAA",
              "user": json.dumps({"id": user_id, "first_name": "Ana"})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_init_data_valid():
    assert validate_init_data(signed_init_data(), TOKEN)["id"] == 1


def test_init_data_tampered_or_expired():
    with pytest.raises(InvalidInitData):
        validate_init_data(signed_init_data().replace("Ana", "Eva"), TOKEN)
    with pytest.raises(InvalidInitData):
        validate_init_data(signed_init_data(token="otro"), TOKEN)
    with pytest.raises(InvalidInitData):
        validate_init_data(signed_init_data(auth_date=1), TOKEN)


def test_parse_job_name_with_negative_chat():
    assert parse_job_name("cuentas_7_-1001_33_99") == (7, -1001, 33, 99)
    with pytest.raises(ValueError):
        parse_job_name("otro_1_2_3_4")


def test_scheduler_charges_due_subscription_and_reminds(repo, tg, profile, now):
    sub = Subscription(user_id=1, name="Gym", amount=Decimal("90000"), cycle="monthly",
                       next_charge="2026-10-15", anchor_day=15)
    soon = Subscription(user_id=1, name="Spotify", amount=Decimal("17000"), cycle="monthly",
                        next_charge="2026-10-16")
    repo.save_subscription(sub)
    repo.save_subscription(soon)
    scheduler.run_for_user(repo, tg, profile, [sub], now)
    txs = repo.list_transactions(1, "2026-10-01", "2026-10-31")
    assert [(t.description, t.source) for t in txs] == [("Suscripción: Gym", "subscription")]
    assert sub.next_charge == "2026-11-15"
    texts = "\n".join(m["text"] for m in tg.sent)
    assert "Gym" in texts and "Próximos cobros" in texts and "Spotify" in texts
    # Una segunda corrida el mismo día no duplica ni el cobro ni el recordatorio
    tg.sent.clear()
    scheduler.run_for_user(repo, tg, profile, [sub], now)
    assert len(repo.list_transactions(1, "2026-10-01", "2026-10-31")) == 1
    assert tg.sent == []


def test_callback_recategorizes_and_learns(repo, tg, profile, now):
    ledger = Ledger(repo, profile)
    from cuentas.models import Transaction

    tx = Transaction(user_id=1, kind="expense", amount=Decimal("10"), category="otros",
                     description="Tienda D1", merchant="D1", date="2026-10-15")
    ledger.record(tx)
    bot.handle_callback(tg, repo, {"id": "x", "from": {"id": 1}, "data": f"s:{tx.date}:{tx.id}:supermercado",
                                   "message": {"chat": {"id": 1}, "message_id": 5}})
    assert repo.get_transaction(1, tx.date, tx.id).category == "supermercado"
    assert profile.merchant_categories["d1"] == "supermercado"


class FakeMessages:
    """Simula Claude: primero pide registrar un gasto y luego responde con texto."""

    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            block = SimpleNamespace(type="tool_use", id="tu_1", name="registrar_movimientos", input={
                "movimientos": [{"tipo": "gasto", "monto": 25000, "descripcion": "Mercado",
                                 "categoria": "supermercado"}]})
            return SimpleNamespace(stop_reason="tool_use", content=[block], usage=USAGE)
        return SimpleNamespace(stop_reason="end_turn", usage=USAGE,
                               content=[SimpleNamespace(type="text", text="Anotado: $25.000 en 🛒 Supermercado")])


USAGE = SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=3000,
                        cache_creation_input_tokens=0)


def test_assistant_tool_loop(monkeypatch, repo, tg, profile):
    fake = FakeMessages()
    monkeypatch.setattr(assistant, "_client", lambda: SimpleNamespace(beta=SimpleNamespace(messages=fake)))
    bot.handle_conversation(tg, repo, profile, 1, "ayer gasté 25 mil en el mercado")
    assert tg.sent[0]["text"].startswith("Anotado")
    assert any(b["callback_data"].startswith("u:") for b in tg.sent[0]["keyboard"][0])
    tool_turn = fake.calls[1]["messages"][2]  # usuario, asistente (tool_use), resultados
    assert tool_turn["content"][0]["type"] == "tool_result"
    assert json.loads(tool_turn["content"][0]["content"])["registrados"][0]["monto"] == 25000.0
    assert fake.calls[0]["model"].startswith("anthropic.")
    assert [m[1] for m in repo.messages] == ["user", "assistant"]
    month = profile.stats["consumo"][next(iter(profile.stats["consumo"]))]
    assert month["llm_calls"] == 1 and month["input"] == 2000 and month["cache_read"] == 6000


def test_assistant_offline_fallback(monkeypatch, repo, profile, now):
    import anthropic
    import httpx2

    def boom():
        raise anthropic.APIConnectionError(request=httpx2.Request("POST", "https://example.invalid"))

    monkeypatch.setattr(assistant, "_client", lambda: SimpleNamespace(
        beta=SimpleNamespace(messages=SimpleNamespace(create=lambda **_: boom()))))
    result = assistant.reply(Ledger(repo, profile, now), "gasté 8000 en taxi")
    assert "Gasto registrado" in result.text
    assert result.recorded[0].category == "transporte"
