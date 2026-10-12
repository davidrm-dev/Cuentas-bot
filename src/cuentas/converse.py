"""Bucle de herramientas con la API Converse de Bedrock, para modelos que no son Claude (Amazon Nova).

Se usa mientras la cuenta no tiene acceso a Claude. Mismas herramientas y mismo prompt;
cambia solo el formato de mensajes de la API.
"""
from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from typing import Any

from . import config
from .tools import TOOLS, ToolExecutor, ToolInputError

log = logging.getLogger(__name__)
_THINKING = re.compile(r"<thinking>.*?</thinking>", re.S)
_BOLD = re.compile(r"\*\*(.+?)\*\*")
# "700,000 COP" / "$700,000.50 COP" -> "$700.000 COP" (formato colombiano, como el resto del bot)
_MONEY = re.compile(r"(?:\$\s?)?(\d{1,3}(?:,\d{3})+)(?:\.\d{1,2})?(\s?[A-Z]{3}\b)")

NOVA_STYLE = """

Notas de formato para este modelo:
- No escribas tu razonamiento; responde directamente al usuario.
- Usa solo HTML de Telegram (<b>, <i>). Nunca uses Markdown con asteriscos."""


@lru_cache(maxsize=1)
def _client() -> Any:
    import boto3
    from botocore.config import Config

    return boto3.client("bedrock-runtime", region_name=config.BEDROCK_REGION,
                        config=Config(read_timeout=60, retries={"max_attempts": 3, "mode": "standard"}))


def tool_config() -> dict[str, Any]:
    return {"tools": [{"toolSpec": {"name": t["name"], "description": t["description"],
                                    "inputSchema": {"json": t["input_schema"]}}} for t in TOOLS]}


def clean_text(text: str) -> str:
    """Quita bloques de razonamiento, convierte **negritas** a HTML y normaliza montos."""
    text = _BOLD.sub(r"<b>\1</b>", _THINKING.sub("", text))
    return _MONEY.sub(lambda m: f"${m.group(1).replace(',', '.')}{m.group(2)}", text).strip()


def loop(system_prompt: str, context: str, user_text: str, executor: ToolExecutor,
         tokens: dict[str, int], max_steps: int) -> str:
    messages: list[dict[str, Any]] = [{"role": "user", "content": [{"text": context}, {"text": user_text}]}]
    client = _client()
    for step in range(max_steps):
        response = client.converse(
            modelId=config.NOVA_MODEL_ID,
            system=[{"text": system_prompt + NOVA_STYLE}],
            messages=messages,
            toolConfig=tool_config(),
            # Nova recomienda decodificación determinista para usar herramientas.
            inferenceConfig={"maxTokens": config.LLM_MAX_TOKENS, "temperature": 0},
        )
        usage = response.get("usage", {})
        tokens["input"] += usage.get("inputTokens", 0)
        tokens["output"] += usage.get("outputTokens", 0)
        tokens["cache_read"] += usage.get("cacheReadInputTokens", 0)
        tokens["cache_write"] += usage.get("cacheWriteInputTokens", 0)

        message = response["output"]["message"]
        messages.append(message)
        tool_uses = [b["toolUse"] for b in message["content"] if "toolUse" in b]
        if response.get("stopReason") != "tool_use" or not tool_uses:
            text = clean_text("".join(b.get("text", "") for b in message["content"]))
            if response.get("stopReason") == "max_tokens":
                text += "…"
            return text or "Listo ✅"
        results = []
        for use in tool_uses:
            log.info("Nova herramienta %s %s", use["name"], json.dumps(use.get("input"), ensure_ascii=False)[:500])
            try:
                content = json.loads(executor.run(use["name"], dict(use.get("input") or {})))
                results.append({"toolResult": {"toolUseId": use["toolUseId"], "content": [{"json": content}]}})
            except (ToolInputError, KeyError) as exc:
                log.warning("Nova herramienta %s falló: %s", use["name"], exc)
                results.append({"toolResult": {"toolUseId": use["toolUseId"], "status": "error",
                                               "content": [{"text": f"Error: {exc}"}]}})
        if step == max_steps - 2:
            # Evita bucles: en la última vuelta el modelo debe contestar con lo que ya tiene.
            results.append({"text": "Ya tienes la información necesaria. Responde ahora al usuario sin usar más "
                                    "herramientas."})
        messages.append({"role": "user", "content": results})
    return "Me enredé procesando eso. ¿Me lo dices de otra forma?"
