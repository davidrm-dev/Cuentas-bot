"""Conversación con Claude (Amazon Bedrock) usando herramientas sobre el libro contable."""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

import anthropic
from botocore.exceptions import BotoCoreError, ClientError
from anthropic import Anthropic, AnthropicBedrockMantle, BetaFallbackState, BetaRefusalFallbackMiddleware

from . import categories, config, converse, learning, parsing
from .finance import Alert, fmt
from .ledger import Ledger
from .models import Transaction
from .tools import TOOLS, ToolExecutor, ToolInputError

log = logging.getLogger(__name__)
MAX_STEPS = 5

SYSTEM_PROMPT = """Eres "Cuentas", el contador personal del usuario dentro de Telegram. Llevas sus finanzas: \
registras gastos e ingresos, clasificas cada gasto, controlas suscripciones y presupuestos, avisas de gastos \
críticos y respondes preguntas sobre su dinero (cuánto gastó, en qué, si le conviene salir o comprar algo).

Cómo trabajar:
- Cuando el usuario cuente un gasto o ingreso, regístralo con `registrar_movimientos` sin pedir confirmación. \
Elige la categoría más probable; si el comercio aparece entre los "comercios ya clasificados", usa esa categoría. \
Si el monto no está claro, pregunta antes de registrar.
- Los mensajes de voz llegan transcritos automáticamente y pueden traer errores ("mil" mal escuchado, nombres \
de comercios deformados). Interprétalos con sentido común; si un monto es ambiguo, pregunta.
- Montos coloquiales: "20 mil" o "20 lucas" = 20000; "1.500" en español suele ser mil quinientos.
- Pagos recurrentes (Netflix, gimnasio, arriendo, plan del celular) van con `registrar_suscripcion`, con la \
próxima fecha de cobro calculada desde hoy. Si además ya pagó este mes, registra también ese gasto.
- Para preguntas como "¿debería salir hoy?" o "¿me alcanza para...?" usa `evaluar_salida_o_compra` y responde con \
un veredicto claro (sí / con cuidado / no) apoyado en los números que devuelve: disponible, presupuesto diario, \
días que faltan. Ten en cuenta sus patrones y su meta de ahorro; propone una alternativa concreta si la respuesta es no.
- Consejos y planes: si el usuario cuenta un plan (eventos a los que debe ir, un imprevisto, pedir prestado a \
alguien) usa `planificar_mes` con cada gasto planeado como evento. Si `estado` es "faltan_datos", pregunta \
exactamente lo que falta. El desglose con las cifras se le envía al usuario automáticamente: tú responde en 2 a \
4 líneas con un veredicto y consejos prácticos (cómo abaratar un evento, qué recortar, cómo devolver el préstamo). \
Sé honesto, sin sermones, y no escribas cifras que la herramienta no haya dado.
- Préstamos: "le presté X a Juan" → `registrar_prestamo` tipo me_deben; "le debo X a mi papá" o "me prestaron" → \
tipo debo. Si no dice cuándo se paga, pregunta una vez si quiere que se lo recuerdes en alguna fecha. Pagos de \
préstamos ("Juan me pagó", "le pagué a mi papá") van con `registrar_abono`, nunca como gasto o ingreso. Cuando se \
acerque una deuda, ayúdale a planear el pago con lo que tiene disponible.
- Gastos fijos sin fecha exacta (gasolina, mercado, transporte del mes) van con `fijar_gasto_fijo`: ese dinero se \
aparta del disponible para que no se lo gaste en otra cosa. Recuérdalo cuando pregunte si puede gastar.
- Cuentas: si menciona con qué pagó o dónde recibió (Nequi, Daviplata, Nu, Bancolombia, efectivo, PayPal...), \
ponlo en `cuenta`. "Tengo X en Nequi" → `actualizar_saldo_cuenta`. "Pasé X de Bancolombia a Nequi" o "saqué del \
cajero" → `transferir_entre_cuentas` (no es gasto). "Mi cuenta principal es Nequi" → `actualizar_perfil`.
- Pantallazos: si llega una imagen de una app financiera (Nequi, Daviplata, Nu, Bancolombia, PayPal...), \
identifica la app por su nombre, logo o colores y usa `importar_pantallazo` con el saldo y los movimientos que se \
lean con claridad (fecha con el año actual si no aparece). Copia cada monto en `monto_texto` EXACTAMENTE como \
se ve, con sus puntos y comas: en Colombia "$21.500,00" son veintiún mil quinientos pesos (el punto separa miles \
y la coma los centavos); no lo conviertas tú. Un comprobante de un solo pago es un \
movimiento. Si la imagen no es financiera o no se lee, dilo y no registres nada.
- Para consultas de gastos usa `consultar_gastos` con fechas exactas. Nunca inventes cifras: todo número sale \
de una herramienta o del contexto.
- Si el usuario te da datos de su situación (sueldo, día de cobro, moneda, meta de ahorro), guárdalos con \
`actualizar_perfil`. Si no conoces su ingreso, pídeselo de forma natural una vez, sin insistir.
- Las alertas de gasto crítico se envían aparte automáticamente; no las repitas, pero puedes aconsejar sobre ellas.
- Adáptate al perfil y a los patrones observados: si gasta mucho en una categoría, menciónalo cuando sea útil; \
si prefiere voz, responde más breve.

Estilo:
- Español neutro, cercano y directo, como un contador de confianza. Respuestas cortas: 1 a 4 líneas para \
registros; algo más para análisis.
- Formato HTML de Telegram únicamente: <b>, <i>, <code>. Nada de Markdown (ni **, ni #, ni tablas).
- Montos con la moneda del usuario. Confirma los registros mostrando monto y categoría.
- Para gráficas y detalle completo, sugiere abrir el dashboard (botón del menú)."""


@dataclass
class AssistantResult:
    text: str
    alerts: list[Alert] = field(default_factory=list)
    recorded: list[Transaction] = field(default_factory=list)
    tokens: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    import_id: str | None = None


@lru_cache(maxsize=1)
def _client() -> Anthropic | AnthropicBedrockMantle:
    # Si el modelo principal rechaza por política, el SDK reintenta con el de respaldo (raro en este uso).
    middleware = [BetaRefusalFallbackMiddleware([{"model": config.model_id(config.FALLBACK_MODEL_ID)}])]
    if config.LLM_PROVIDER == "anthropic":
        return Anthropic(api_key=config.secret(config.ANTHROPIC_KEY_PARAM), timeout=60, max_retries=2,
                         middleware=middleware)
    # En Bedrock, el rol de ejecución de Lambda provee las credenciales (cadena estándar de AWS).
    return AnthropicBedrockMantle(aws_region=config.BEDROCK_REGION, timeout=60, max_retries=2,
                                  middleware=middleware)


def _context_block(ledger: Ledger, history: list[dict[str, str]]) -> str:
    local_now = ledger.now.astimezone(ZoneInfo(ledger.profile.timezone))
    parts = [
        "<contexto>",
        f"Fecha y hora local: {learning.WEEKDAYS[local_now.weekday()]} {local_now.strftime('%Y-%m-%d %H:%M')}",
        learning.context_for_llm(ledger.profile),
    ]
    debts = ledger.repo.list_debts(ledger.profile.user_id)
    if debts:
        cur = ledger.profile.currency
        lines = [f"{d.person} le debe {fmt(d.balance, cur)} al usuario" if d.direction == "me_deben"
                 else f"el usuario le debe {fmt(d.balance, cur)} a {d.person}" for d in debts[:10]]
        lines = [f"{line} (vence {d.due_date})" if d.due_date else line for line, d in zip(lines, debts)]
        parts.append("Préstamos abiertos: " + "; ".join(lines))
    if history:
        parts.append("Conversación reciente:")
        parts += [f"{'Usuario' if m['role'] == 'user' else 'Cuentas'}: {m['text']}" for m in history]
    parts.append("</contexto>")
    return "\n".join(parts)


def reply(ledger: Ledger, user_text: str, source: str = "text",
          history: list[dict[str, str]] | None = None,
          image: tuple[bytes, str] | None = None) -> AssistantResult:
    """`image` = (bytes, formato "jpeg"|"png"|"webp") para pantallazos."""
    executor = ToolExecutor(ledger, source=source)
    content: list[dict[str, Any]] = [{"type": "text", "text": _context_block(ledger, history or [])}]
    if image:
        content.append({"type": "image", "source": {"type": "base64", "media_type": f"image/{image[1]}",
                                                    "data": base64.b64encode(image[0]).decode()}})
    content.append({"type": "text", "text": user_text})
    messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
    tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    try:
        if config.LLM_PROVIDER == "nova":
            text = converse.loop(SYSTEM_PROMPT, messages[0]["content"][0]["text"], user_text, executor,
                                 tokens, MAX_STEPS, image)
        else:
            with BetaFallbackState():
                text = _loop(messages, executor, tokens)
    except (anthropic.APIConnectionError, anthropic.APIStatusError, BotoCoreError, ClientError) as exc:
        log.exception("Fallo llamando al modelo: %s", exc)
        text = _offline_reply(ledger, executor, user_text)
    return AssistantResult(text=text, alerts=executor.alerts, recorded=executor.recorded, tokens=tokens,
                           notes=executor.notes, import_id=executor.import_id)


def _add_usage(tokens: dict[str, int], usage: Any) -> None:
    if usage is None:
        return
    tokens["input"] += getattr(usage, "input_tokens", 0) or 0
    tokens["output"] += getattr(usage, "output_tokens", 0) or 0
    tokens["cache_read"] += getattr(usage, "cache_read_input_tokens", 0) or 0
    tokens["cache_write"] += getattr(usage, "cache_creation_input_tokens", 0) or 0


def _loop(messages: list[dict[str, Any]], executor: ToolExecutor, tokens: dict[str, int]) -> str:
    client = _client()
    for _ in range(MAX_STEPS):
        response = client.beta.messages.create(
            model=config.model_id(config.MODEL_ID),
            max_tokens=config.LLM_MAX_TOKENS,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            tools=TOOLS,
            messages=messages,
            output_config={"effort": config.LLM_EFFORT},
        )
        _add_usage(tokens, getattr(response, "usage", None))
        if response.stop_reason == "refusal":
            return "No puedo ayudarte con eso. Si es sobre tus finanzas, intenta decirlo de otra forma."
        # Se reenvía el contenido completo (incluidos bloques de razonamiento) sin modificarlo.
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "pause_turn":
            continue
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not tool_uses:
            text = "".join(b.text for b in response.content if b.type == "text").strip()
            if response.stop_reason == "max_tokens":
                text += "…"
            return text or "Listo ✅"
        results = []
        for block in tool_uses:
            try:
                content, is_error = executor.run(block.name, dict(block.input or {})), False
            except (ToolInputError, KeyError) as exc:
                content, is_error = f"Error: {exc}", True
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": content,
                            "is_error": is_error})
        messages.append({"role": "user", "content": results})
    return "Me enredé procesando eso. ¿Me lo dices de otra forma?"


def _offline_reply(ledger: Ledger, executor: ToolExecutor, user_text: str) -> str:
    """Sin modelo disponible: registramos lo que se pueda con el parser simple."""
    entry = parsing.quick_parse(user_text, ledger.profile.merchant_categories)
    if entry is None:
        return ("Ahora mismo no puedo pensar bien 😵. Puedes registrar gastos con frases simples como "
                "<i>gasté 15000 en uber</i> o revisar el dashboard.")
    executor.run("registrar_movimientos", {"movimientos": [{
        "tipo": "ingreso" if entry.kind == "income" else "gasto",
        "monto": float(entry.amount),
        "descripcion": entry.description,
        "categoria": entry.category,
    }]})
    verb = "Ingreso" if entry.kind == "income" else "Gasto"
    return (f"{verb} registrado: <b>{fmt(entry.amount, ledger.profile.currency)}</b> en "
            f"{categories.label(entry.category)}.")
