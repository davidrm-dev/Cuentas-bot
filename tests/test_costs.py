from datetime import date
from types import SimpleNamespace

import pytest

from cuentas import assistant, bot, config, usage
from cuentas.handlers import processor
from cuentas.parsing import confident_parse

TODAY = date(2026, 10, 15)


@pytest.mark.parametrize("text", [
    "gasté 3 millones en arriendo",        # "millones" lo interpreta mejor el modelo
    "ayer gasté 20 mil en uber",           # fecha distinta a hoy
    "¿cuánto gasté en uber?",              # pregunta
    "gasté 10 mil en uber y 20 mil en café",  # dos movimientos
    "gasté 5000 en cosas",                 # categoría dudosa
    "no, eran 20 mil en uber",             # corrección
    "pago netflix 45 mil cada mes",        # suscripción
])
def test_fast_path_defers_ambiguous_messages(text):
    assert confident_parse(text) is None


def test_fast_path_uses_learned_merchants():
    assert confident_parse("gasté 9 mil en ara") is None
    entry = confident_parse("gasté 9 mil en ara", {"ara": "supermercado"})
    assert entry.category == "supermercado"


def no_model(monkeypatch):
    def boom():
        raise AssertionError("no debía llamar al modelo")
    monkeypatch.setattr(assistant, "_client", boom)


def test_fast_path_records_without_model(monkeypatch, repo, tg, profile):
    no_model(monkeypatch)
    bot.handle_conversation(tg, repo, profile, 1, "Gasté 12.500 en uber")
    tx = repo.last_transaction(1)
    assert (tx.amount, tx.category, tx.merchant) == (12500, "transporte", "uber")
    assert "Gasto" in tg.sent[0]["text"] and tg.sent[0]["keyboard"]
    month = next(iter(profile.stats["consumo"].values()))
    assert month["fast"] == 1 and month["llm_calls"] == 0


def test_daily_llm_limit(monkeypatch, repo, tg, profile):
    no_model(monkeypatch)
    from cuentas.finance import today_for

    today = today_for(profile)  # el día del usuario (Bogotá), no el del servidor
    profile.stats["llm_daily"] = {"date": today.isoformat(), "count": config.DAILY_LLM_LIMIT}
    bot.handle_conversation(tg, repo, profile, 1, "¿debería salir hoy?")
    assert "límite diario" in tg.sent[0]["text"]
    profile.stats["llm_daily"] = {"date": "2000-01-01", "count": 999}
    assert usage.llm_allowed(profile.stats, today)


def test_usage_estimate_and_text():
    stats = {}
    usage.add_llm(stats, TODAY, {"input": 1_000_000, "output": 100_000, "cache_read": 1_000_000})
    usage.add_audio(stats, TODAY, 5)   # se factura mínimo 15 s
    usage.add_audio(stats, TODAY, 45)
    month = stats["consumo"]["2026-10"]
    assert month["audio_seconds"] == 60
    cost = usage.estimate(month)
    assert cost["llm"] == pytest.approx(0.10 + 0.05 + 0.01)
    assert cost["audio"] == pytest.approx(0.024)
    text = usage.usage_text(stats, TODAY)
    assert "2.000.000 entrada" in text and "Total: $0.18" in text


def test_usage_keeps_few_months():
    stats = {}
    for m in range(1, 10):
        usage.add_fast(stats, date(2026, m, 1))
    assert len(stats["consumo"]) <= 6


def test_allowlist_blocks_strangers(monkeypatch, repo, tg):
    monkeypatch.setattr(config, "ALLOWED_USER_IDS", {42})
    update = {"update_id": 1, "message": {"message_id": 1, "from": {"id": 7}, "chat": {"id": 7, "type": "private"},
                                          "text": "hola"}}
    processor.process_update(repo, tg, update)
    assert repo.profiles == {}
    assert tg.sent[0]["text"].startswith("Este bot es privado")
    assert processor.is_allowed(42)


def test_model_id_per_provider(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "anthropic")
    assert config.model_id("anthropic.claude-haiku-5-5") == "claude-haiku-5-5"
    monkeypatch.setattr(config, "LLM_PROVIDER", "bedrock")
    assert config.model_id("claude-haiku-5-5") == "anthropic.claude-haiku-5-5"
    assert config.model_id("anthropic.claude-haiku-5-5") == "anthropic.claude-haiku-5-5"


def test_anthropic_client_reads_key_from_ssm(monkeypatch):
    import anthropic as sdk

    monkeypatch.setattr(config, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(config, "secret", lambda name: "sk-ant-test")
    assistant._client.cache_clear()
    try:
        client = assistant._client()
        assert isinstance(client, sdk.Anthropic) and client.api_key == "sk-ant-test"
    finally:
        assistant._client.cache_clear()
