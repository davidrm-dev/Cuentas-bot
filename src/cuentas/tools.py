"""Herramientas que el modelo puede invocar y su ejecución contra el libro contable."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import categories, finance
from .finance import Alert
from .ledger import Ledger
from .models import CYCLES, Subscription, Transaction, money

ALL_CATEGORIES = list(categories.CATEGORIES) + list(categories.INCOME_CATEGORIES)
_DATE = {"type": "string", "description": "Fecha YYYY-MM-DD"}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "registrar_movimientos",
        "description": (
            "Registra uno o varios gastos o ingresos que el usuario mencionó. Úsala cada vez que el usuario "
            "cuente que gastó, pagó, compró, recibió o cobró dinero. Un mensaje puede traer varios movimientos."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "movimientos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tipo": {"type": "string", "enum": ["gasto", "ingreso"]},
                            "monto": {"type": "number", "description": "Monto positivo en la moneda del usuario"},
                            "descripcion": {"type": "string", "description": "Descripción breve en español"},
                            "categoria": {"type": "string", "enum": ALL_CATEGORIES,
                                          "description": "Para ingresos usa salario/freelance/inversiones/otros_ingresos"},
                            "comercio": {"type": "string", "description": "Comercio o persona, si se menciona"},
                            "fecha": {**_DATE, "description": "Fecha del movimiento si no es hoy (YYYY-MM-DD)"},
                            "necesidad": {"type": "string", "enum": ["esencial", "discrecional"]},
                        },
                        "required": ["tipo", "monto", "descripcion", "categoria"],
                    },
                }
            },
            "required": ["movimientos"],
        },
    },
    {
        "name": "corregir_ultimo_movimiento",
        "description": "Corrige o elimina el último movimiento registrado (por ejemplo 'no, eso era transporte' o 'borra eso').",
        "input_schema": {
            "type": "object",
            "properties": {
                "accion": {"type": "string", "enum": ["eliminar", "cambiar_categoria", "cambiar_monto"]},
                "categoria": {"type": "string", "enum": ALL_CATEGORIES},
                "monto": {"type": "number"},
            },
            "required": ["accion"],
        },
    },
    {
        "name": "registrar_suscripcion",
        "description": "Registra un pago recurrente (Netflix, gimnasio, arriendo, plan de celular...). Se cobrará automáticamente en cada ciclo.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string"},
                "monto": {"type": "number"},
                "ciclo": {"type": "string", "enum": list(CYCLES)},
                "proximo_cobro": {**_DATE, "description": "Próxima fecha de cobro YYYY-MM-DD"},
                "categoria": {"type": "string", "enum": list(categories.CATEGORIES)},
            },
            "required": ["nombre", "monto", "ciclo", "proximo_cobro"],
        },
    },
    {
        "name": "cancelar_suscripcion",
        "description": "Da de baja una suscripción registrada.",
        "input_schema": {"type": "object", "properties": {"nombre": {"type": "string"}}, "required": ["nombre"]},
    },
    {
        "name": "listar_suscripciones",
        "description": "Lista las suscripciones activas con su costo y próximo cobro.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "consultar_gastos",
        "description": "Devuelve totales por categoría, ingresos, balance y mayores gastos en un rango de fechas.",
        "input_schema": {
            "type": "object",
            "properties": {
                "desde": _DATE,
                "hasta": _DATE,
                "categoria": {"type": "string", "enum": ALL_CATEGORIES},
            },
            "required": ["desde", "hasta"],
        },
    },
    {
        "name": "evaluar_salida_o_compra",
        "description": (
            "Evalúa si el usuario puede permitirse salir o hacer un gasto, con su disponible del mes, "
            "presupuesto diario y su historial. Úsala ante preguntas como '¿debería salir hoy?' o '¿me alcanza para...?'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "costo_estimado": {"type": "number", "description": "Costo que mencionó el usuario, si lo dijo"},
                "descripcion": {"type": "string"},
            },
        },
    },
    {
        "name": "planificar_mes",
        "description": (
            "Calcula cuánto dinero le falta al usuario para llegar a su próximo pago (o a fin de mes) cubriendo su "
            "gasto diario habitual, los cobros fijos pendientes y los eventos que planea, y cuánto debería pedir "
            "prestado (con un colchón). Úsala cuando cuente un plan, eventos que no puede dejar de ir, imprevistos, "
            "o pregunte cuánto pedir prestado o cómo sobrevivir el mes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "eventos": {
                    "type": "array",
                    "description": "Gastos planeados que el usuario mencionó (eventos, viajes, regalos, pagos pendientes)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "descripcion": {"type": "string"},
                            "monto": {"type": "number", "description": "Costo estimado total del evento"},
                            "fecha": {**_DATE, "description": "Fecha del evento si la dijo (YYYY-MM-DD)"},
                        },
                        "required": ["descripcion", "monto"],
                    },
                },
                "saldo_actual": {"type": "number", "description": "Dinero que el usuario dice tener hoy, si lo dijo"},
                "gasto_diario_estimado": {"type": "number",
                                          "description": "Solo si el usuario dijo cuánto gasta normalmente al día"},
                "colchon_pct": {"type": "number", "minimum": 0, "maximum": 50,
                                "description": "Margen para imprevistos en %, por defecto 10"},
            },
            "required": ["eventos"],
        },
    },
    {
        "name": "actualizar_perfil",
        "description": "Guarda datos del contexto económico del usuario: ingreso mensual, día de pago, moneda, meta de ahorro o zona horaria.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ingreso_mensual": {"type": "number"},
                "dia_de_pago": {"type": "integer", "minimum": 1, "maximum": 31},
                "moneda": {"type": "string", "description": "Código ISO 4217, ej. COP, MXN, EUR, USD"},
                "meta_ahorro_pct": {"type": "number", "minimum": 0, "maximum": 90},
                "zona_horaria": {"type": "string", "description": "Zona IANA, ej. America/Bogota"},
            },
        },
    },
    {
        "name": "fijar_presupuesto",
        "description": "Fija (o quita con monto 0) un tope de gasto mensual para una categoría.",
        "input_schema": {
            "type": "object",
            "properties": {
                "categoria": {"type": "string", "enum": list(categories.CATEGORIES)},
                "monto": {"type": "number"},
            },
            "required": ["categoria", "monto"],
        },
    },
]


class ToolInputError(ValueError):
    pass


@dataclass
class ToolExecutor:
    ledger: Ledger
    source: str = "text"
    alerts: list[Alert] = field(default_factory=list)
    recorded: list[Transaction] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # mensajes armados por código que el bot envía tal cual

    def run(self, name: str, args: dict[str, Any]) -> str:
        handler: Callable[[dict[str, Any]], Any] | None = getattr(self, f"_t_{name}", None)
        if handler is None:
            raise ToolInputError(f"Herramienta desconocida: {name}")
        return json.dumps(handler(args), ensure_ascii=False, default=str)

    # ---- validación ----------------------------------------------------
    @staticmethod
    def _amount(value: Any, allow_zero: bool = False) -> Decimal:
        try:
            amount = money(value)
        except (InvalidOperation, ValueError, TypeError):
            raise ToolInputError(f"Monto inválido: {value!r}")
        if amount < 0 or (amount == 0 and not allow_zero):
            raise ToolInputError("El monto debe ser positivo")
        return amount

    @staticmethod
    def _date(value: Any) -> date:
        try:
            return date.fromisoformat(str(value))
        except ValueError:
            raise ToolInputError(f"Fecha inválida: {value!r} (usa YYYY-MM-DD)")

    # ---- herramientas --------------------------------------------------
    def _t_registrar_movimientos(self, args: dict[str, Any]) -> dict[str, Any]:
        items = args.get("movimientos") or []
        if not items:
            raise ToolInputError("No hay movimientos para registrar")
        profile = self.ledger.profile
        saved = []
        for item in items:
            kind = "income" if item.get("tipo") == "ingreso" else "expense"
            description = str(item.get("descripcion") or "").strip()[:200]
            merchant = str(item.get("comercio") or "").strip()[:80]
            category = item.get("categoria")
            if not categories.is_valid(category):
                category = categories.guess(f"{merchant} {description}", profile.merchant_categories)
            if kind == "income" and category not in categories.INCOME_CATEGORIES:
                category = "otros_ingresos"
            if kind == "expense" and category in categories.INCOME_CATEGORIES:
                category = "otros"
            tx_date = self._date(item["fecha"]) if item.get("fecha") else self.ledger.today
            if tx_date > self.ledger.today:
                tx_date = self.ledger.today
            tx = Transaction(
                user_id=profile.user_id, kind=kind, amount=self._amount(item.get("monto")),
                category=category, description=description, merchant=merchant, date=tx_date.isoformat(),
                necessity=item.get("necesidad") or categories.default_necessity(category), source=self.source,
            )
            self.alerts.extend(self.ledger.record(tx))
            self.recorded.append(tx)
            saved.append({"id": tx.id, "tipo": item.get("tipo"), "monto": float(tx.amount),
                          "categoria": tx.category, "fecha": tx.date})
        month = finance.totals(self.ledger.month_transactions())
        return {
            "registrados": saved,
            "gasto_del_mes": float(month["expense"]),
            "alertas_enviadas_aparte": [a.message for a in self.alerts],
        }

    def _t_corregir_ultimo_movimiento(self, args: dict[str, Any]) -> dict[str, Any]:
        tx = self.ledger.repo.last_transaction(self.ledger.profile.user_id)
        if tx is None:
            return {"error": "No hay movimientos registrados"}
        action = args.get("accion")
        if action == "eliminar":
            self.ledger.delete(tx)
            return {"eliminado": tx.to_public()}
        if action == "cambiar_categoria":
            if not categories.is_valid(args.get("categoria")):
                raise ToolInputError("Falta una categoría válida")
            return {"actualizado": self.ledger.recategorize(tx, args["categoria"]).to_public(),
                    "nota": "Aprendido para futuros gastos similares"}
        if action == "cambiar_monto":
            return {"actualizado": self.ledger.change_amount(tx, self._amount(args.get("monto"))).to_public()}
        raise ToolInputError(f"Acción desconocida: {action}")

    def _t_registrar_suscripcion(self, args: dict[str, Any]) -> dict[str, Any]:
        cycle = args.get("ciclo")
        if cycle not in CYCLES:
            raise ToolInputError(f"Ciclo inválido: {cycle}")
        next_charge = self._date(args.get("proximo_cobro"))
        category = args.get("categoria") if args.get("categoria") in categories.CATEGORIES else "suscripciones"
        profile = self.ledger.profile
        existing = self.ledger.find_subscription(str(args.get("nombre", "")))
        sub = existing or Subscription(user_id=profile.user_id, name=str(args["nombre"]).strip()[:60],
                                       amount=Decimal(0), cycle=cycle, next_charge="")
        sub.amount = self._amount(args.get("monto"))
        sub.cycle, sub.category = cycle, category
        sub.next_charge, sub.anchor_day, sub.active = next_charge.isoformat(), next_charge.day, True
        # Una fecha pasada indica el día de cobro, no un cobro pendiente: avanzamos sin registrar gasto.
        while date.fromisoformat(sub.next_charge) < self.ledger.today:
            sub.advance()
        self.ledger.repo.save_subscription(sub)
        subs = self.ledger.repo.list_subscriptions(profile.user_id)
        return {"suscripcion": sub.to_public(), "actualizada": existing is not None,
                "costo_fijo_mensual_total": float(finance.monthly_fixed(subs))}

    def _t_cancelar_suscripcion(self, args: dict[str, Any]) -> dict[str, Any]:
        sub = self.ledger.find_subscription(str(args.get("nombre", "")))
        if sub is None:
            names = [s.name for s in self.ledger.repo.list_subscriptions(self.ledger.profile.user_id)]
            return {"error": "No encontré esa suscripción", "suscripciones_activas": names}
        sub.active = False
        self.ledger.repo.save_subscription(sub)
        return {"cancelada": sub.name, "ahorro_mensual": float(sub.monthly_cost)}

    def _t_listar_suscripciones(self, args: dict[str, Any]) -> dict[str, Any]:
        subs = self.ledger.repo.list_subscriptions(self.ledger.profile.user_id)
        return {"suscripciones": [s.to_public() for s in subs],
                "costo_fijo_mensual_total": float(finance.monthly_fixed(subs))}

    def _t_consultar_gastos(self, args: dict[str, Any]) -> dict[str, Any]:
        start, end = self._date(args.get("desde")), self._date(args.get("hasta"))
        if end < start:
            start, end = end, start
        if (end - start).days > 400:
            raise ToolInputError("Rango demasiado amplio (máximo ~13 meses)")
        txs = self.ledger.repo.list_transactions(self.ledger.profile.user_id, start.isoformat(), end.isoformat())
        payload = finance.summary_payload(self.ledger.profile, txs, start, end, args.get("categoria"))
        payload["promedio_mensual_aprendido"] = self.ledger.profile.stats.get("monthly_avg", {})
        return payload

    def _t_evaluar_salida_o_compra(self, args: dict[str, Any]) -> dict[str, Any]:
        cost = self._amount(args["costo_estimado"]) if args.get("costo_estimado") else None
        subs = self.ledger.repo.list_subscriptions(self.ledger.profile.user_id)
        return finance.affordability(self.ledger.profile, self.ledger.month_transactions(), subs,
                                     self.ledger.today, cost)

    def _t_planificar_mes(self, args: dict[str, Any]) -> dict[str, Any]:
        events = []
        for e in args.get("eventos") or []:
            event = {"descripcion": str(e.get("descripcion", ""))[:80], "monto": self._amount(e.get("monto"))}
            if e.get("fecha"):
                event["fecha"] = self._date(e["fecha"]).isoformat()
            events.append(event)
        cash = self._amount(args["saldo_actual"], allow_zero=True) if args.get("saldo_actual") is not None else None
        daily = self._amount(args["gasto_diario_estimado"]) if args.get("gasto_diario_estimado") else None
        buffer = Decimal(str(args.get("colchon_pct", 10) or 0))
        if not 0 <= buffer <= 50:
            raise ToolInputError("El colchón debe estar entre 0 y 50%")
        subs = self.ledger.repo.list_subscriptions(self.ledger.profile.user_id)
        plan = finance.plan_month(self.ledger.profile, self.ledger.month_transactions(), subs, self.ledger.today,
                                  events, cash, daily, buffer)
        if plan["estado"] == "ok":
            self.notes.append(finance.plan_text(plan))
            plan["nota"] = ("El desglose con todas las cifras ya se le envió al usuario. No repitas cifras ni "
                            "inventes otras; da solo un veredicto corto y 1 a 3 consejos prácticos. Este cálculo "
                            "NO reserva la meta de ahorro: no digas que la protege.")
        return plan

    def _t_actualizar_perfil(self, args: dict[str, Any]) -> dict[str, Any]:
        p = self.ledger.profile
        # Un 0 o un valor vacío significa "no lo dijo": no se toca ese dato.
        if args.get("ingreso_mensual"):
            p.monthly_income = self._amount(args["ingreso_mensual"])
        if args.get("dia_de_pago"):
            day = int(args["dia_de_pago"])
            if not 1 <= day <= 31:
                raise ToolInputError("Día de pago inválido")
            p.payday = day
        if args.get("moneda"):
            code = str(args["moneda"]).upper().strip()
            if len(code) != 3 or not code.isalpha():
                raise ToolInputError("Moneda inválida (código ISO de 3 letras)")
            p.currency = code
        if args.get("meta_ahorro_pct") is not None:
            pct = Decimal(str(args["meta_ahorro_pct"]))
            if not 0 <= pct <= 90:
                raise ToolInputError("La meta de ahorro debe estar entre 0 y 90%")
            p.savings_goal_pct = pct
        if args.get("zona_horaria"):
            try:
                ZoneInfo(args["zona_horaria"])
            except (ZoneInfoNotFoundError, ValueError):
                raise ToolInputError("Zona horaria inválida")
            p.timezone = args["zona_horaria"]
        self.ledger.repo.save_profile(p)
        return {"perfil": {"ingreso_mensual": float(p.monthly_income) if p.monthly_income else None,
                           "dia_de_pago": p.payday, "moneda": p.currency,
                           "meta_ahorro_pct": float(p.savings_goal_pct), "zona_horaria": p.timezone}}

    def _t_fijar_presupuesto(self, args: dict[str, Any]) -> dict[str, Any]:
        category = args.get("categoria")
        if category not in categories.CATEGORIES:
            raise ToolInputError("Categoría inválida")
        amount = self._amount(args.get("monto"), allow_zero=True)
        p = self.ledger.profile
        if amount == 0:
            p.budgets.pop(category, None)
        else:
            p.budgets[category] = amount
        self.ledger.repo.save_profile(p)
        spent = finance.totals(self.ledger.month_transactions())["by_category"].get(category, Decimal(0))
        return {"presupuestos": {k: float(v) for k, v in p.budgets.items()},
                "gastado_este_mes_en_categoria": float(spent)}
