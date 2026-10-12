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
