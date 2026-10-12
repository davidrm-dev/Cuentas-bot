from cuentas import assistant, config, converse
from cuentas.ledger import Ledger


class FakeConverse:
    def __init__(self):
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        usage = {"inputTokens": 800, "outputTokens": 60}
        if len(self.calls) == 1:
            return {"stopReason": "tool_use", "usage": usage, "output": {"message": {"role": "assistant", "content": [
                {"text": "<thinking>Debo registrar el gasto</thinking>"},
                {"toolUse": {"toolUseId": "t1", "name": "registrar_movimientos", "input": {"movimientos": [
                    {"tipo": "gasto", "monto": 40000, "descripcion": "Cine", "categoria": "ocio"}]}}}]}}}
        return {"stopReason": "end_turn", "usage": usage, "output": {"message": {"role": "assistant", "content": [
            {"text": "<thinking>listo</thinking>Anotado: **$40.000** en ocio"}]}}}


def test_nova_tool_loop(monkeypatch, repo, profile, now):
    fake = FakeConverse()
    monkeypatch.setattr(config, "LLM_PROVIDER", "nova")
    monkeypatch.setattr(converse, "_client", lambda: fake)
    result = assistant.reply(Ledger(repo, profile, now), "ayer fui a cine, 40 mil")
    assert result.text == "Anotado: <b>$40.000</b> en ocio"
    assert result.recorded[0].category == "ocio"
    assert result.tokens["input"] == 1600 and result.tokens["output"] == 120
    tool_result = fake.calls[1]["messages"][2]["content"][0]["toolResult"]
    assert tool_result["toolUseId"] == "t1" and tool_result["content"][0]["json"]["registrados"]
    spec = fake.calls[0]["toolConfig"]["tools"][0]["toolSpec"]
    assert spec["name"] == "registrar_movimientos" and "json" in spec["inputSchema"]


def test_nova_error_falls_back_to_parser(monkeypatch, repo, profile, now):
    from botocore.exceptions import ClientError

    class Denied:
        def converse(self, **_):
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "Converse")

    monkeypatch.setattr(config, "LLM_PROVIDER", "nova")
    monkeypatch.setattr(converse, "_client", lambda: Denied())
    result = assistant.reply(Ledger(repo, profile, now), "gasté 8000 en taxi")
    assert "Gasto registrado" in result.text


def test_nova_loop_guard_asks_for_final_answer(monkeypatch, repo, profile, now):
    class Looper:
        calls = []

        def converse(self, **kwargs):
            self.calls.append(kwargs)
            return {"stopReason": "tool_use", "usage": {}, "output": {"message": {"role": "assistant", "content": [
                {"toolUse": {"toolUseId": f"t{len(self.calls)}", "name": "listar_suscripciones", "input": {}}}]}}}

    looper = Looper()
    monkeypatch.setattr(config, "LLM_PROVIDER", "nova")
    monkeypatch.setattr(converse, "_client", lambda: looper)
    assistant.reply(Ledger(repo, profile, now), "¿qué suscripciones tengo?")
    texts = [b.get("text", "") for m in looper.calls[-1]["messages"] if m["role"] == "user" for b in m["content"]]
    assert any("sin usar más herramientas" in t for t in texts)


def test_profile_tool_ignores_zero_income(repo, profile, now):
    import json

    from cuentas.tools import ToolExecutor

    out = json.loads(ToolExecutor(Ledger(repo, profile, now)).run("actualizar_perfil", {"ingreso_mensual": 0}))
    assert out["perfil"]["ingreso_mensual"] is None


def test_clean_text_formats_money():
    assert converse.clean_text("pide <b>700,000 COP</b> o 580,000.00 COP") == "pide <b>$700.000 COP</b> o $580.000 COP"
    assert converse.clean_text("son 2,500,000 COP") == "son $2.500.000 COP"
    assert converse.clean_text("hoy, 25 de octubre") == "hoy, 25 de octubre"
