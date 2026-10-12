"""Herramientas que el modelo puede invocar y su ejecución contra el libro contable."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import accounts, categories, finance
from .finance import Alert
from .ledger import Ledger
from .models import CYCLES, Debt, Subscription, Transaction, money, new_id
from .parsing import parse_screen_amount

ALL_CATEGORIES = list(categories.CATEGORIES) + list(categories.INCOME_CATEGORIES)
ACCOUNT_KEYS = list(accounts.ACCOUNTS)
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
                            "cuenta": {"type": "string", "enum": ACCOUNT_KEYS,
                                       "description": "Cuenta o medio de pago si lo mencionó (nequi, efectivo...)"},
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
        "name": "registrar_prestamo",
        "description": (
            "Registra dinero prestado. tipo 'me_deben' cuando el usuario le prestó a alguien ('le presté 50 mil a "
            "Juan'); 'debo' cuando alguien le prestó o le debe a alguien ('le debo 100 mil a mi papá'). Si dice "
            "cuándo se debe pagar, inclúyelo como fecha_limite para recordárselo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tipo": {"type": "string", "enum": ["me_deben", "debo"]},
                "persona": {"type": "string", "description": "Nombre de la persona, como la mencionó"},
                "monto": {"type": "number"},
                "fecha_limite": {**_DATE, "description": "Fecha acordada para pagar (YYYY-MM-DD), si la dijo"},
                "nota": {"type": "string"},
                "movimiento_hoy": {"type": "boolean", "description": (
                    "true si el dinero se entregó/recibió ahora (afecta su saldo); false si es una deuda que ya "
                    "existía de antes")},
            },
            "required": ["tipo", "persona", "monto"],
        },
    },
    {
        "name": "registrar_abono",
        "description": (
            "Registra un pago de un préstamo: cuando alguien le paga al usuario ('Juan me pagó 20 mil') o el usuario "
            "paga una deuda ('le pagué 50 mil a mi papá'). Si no dice monto y dice que pagó todo, usa todo=true."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "persona": {"type": "string"},
                "tipo": {"type": "string", "enum": ["me_deben", "debo"]},
                "monto": {"type": "number"},
                "todo": {"type": "boolean"},
            },
            "required": ["persona"],
        },
    },
    {
        "name": "listar_prestamos",
        "description": "Lista lo que le deben al usuario y lo que él debe, con saldos y fechas límite.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "fijar_gasto_fijo",
        "description": (
            "Aparta cada mes dinero para un gasto fijo sin fecha exacta (gasolina, mercado, transporte, arriendo "
            "compartido...) para que el usuario no se lo gaste en otra cosa. Monto 0 para quitarlo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Ej. gasolina"},
                "monto_mensual": {"type": "number"},
                "categoria": {"type": "string", "enum": list(categories.CATEGORIES)},
            },
            "required": ["nombre", "monto_mensual"],
        },
    },
    {
        "name": "actualizar_saldo_cuenta",
        "description": "Guarda cuánto dinero tiene el usuario en una cuenta ('tengo 300 mil en Nequi'). Desde ahí cada gasto o ingreso con esa cuenta actualiza el saldo.",
        "input_schema": {
            "type": "object",
            "properties": {"cuenta": {"type": "string", "enum": ACCOUNT_KEYS}, "saldo": {"type": "number"}},
            "required": ["cuenta", "saldo"],
        },
    },
    {
        "name": "transferir_entre_cuentas",
        "description": "Mueve dinero entre cuentas propias ('pasé 100 mil de Bancolombia a Nequi', 'saqué 50 mil del cajero'). No es gasto.",
        "input_schema": {
            "type": "object",
            "properties": {"desde": {"type": "string", "enum": ACCOUNT_KEYS},
                           "hacia": {"type": "string", "enum": ACCOUNT_KEYS}, "monto": {"type": "number"}},
            "required": ["desde", "hacia", "monto"],
        },
    },
    {
        "name": "consultar_cuentas",
        "description": "Saldos por cuenta y cuánto se ha gastado este mes desde cada cuenta o medio de pago.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "importar_pantallazo",
        "description": (
            "Importa lo que se ve en un pantallazo de una app financiera (Nequi, Daviplata, Nu, Bancolombia, "
            "PayPal...): la app, el saldo visible y los movimientos visibles. Solo para imágenes; no inventes datos "
            "que no se lean claramente. El sistema descarta los movimientos que ya estaban registrados."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "enum": ACCOUNT_KEYS + ["desconocida"]},
                "saldo_texto": {"type": "string",
                                "description": "Saldo copiado EXACTAMENTE como se ve, con sus puntos y comas (ej. '$ 187.450,00')"},
                "movimientos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "fecha": {**_DATE, "description": "YYYY-MM-DD; usa el año actual si no aparece"},
                            "descripcion": {"type": "string"},
                            "monto_texto": {"type": "string", "description": (
                                "Monto copiado EXACTAMENTE como aparece en la pantalla, con puntos, comas y "
                                "decimales, sin convertirlo (ej. '-$21.500,00', '$ 7.958'). El sistema lo interpreta")},
                            "tipo": {"type": "string", "enum": ["gasto", "ingreso"]},
                            "categoria": {"type": "string", "enum": ALL_CATEGORIES},
                        },
                        "required": ["descripcion", "monto_texto", "tipo"],
                    },
                },
            },
            "required": ["app"],
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
                "cuenta_principal": {"type": "string", "enum": ACCOUNT_KEYS,
                                     "description": "Cuenta que usa por defecto cuando no dice cuál"},
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
    import_id: str | None = None  # importación de pantallazo hecha en este turno (para el botón deshacer)

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
            account = item.get("cuenta") if accounts.is_valid(item.get("cuenta")) else ""
            if not account:
                account = accounts.detect(f"{description} {merchant}")[0] or ""
            tx = Transaction(
                user_id=profile.user_id, kind=kind, amount=self._amount(item.get("monto")),
                category=category, description=description, merchant=merchant, date=tx_date.isoformat(),
                necessity=item.get("necesidad") or categories.default_necessity(category), source=self.source,
                account=account,
            )
            self.alerts.extend(self.ledger.record(tx))
            self.recorded.append(tx)
            saved.append({"id": tx.id, "tipo": item.get("tipo"), "monto": float(tx.amount),
                          "categoria": tx.category, "fecha": tx.date, "cuenta": tx.account or None})
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
        debts = self.ledger.repo.list_debts(self.ledger.profile.user_id, include_closed=True)
        return finance.affordability(self.ledger.profile, self.ledger.month_transactions(), subs,
                                     self.ledger.today, cost, debts)

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
        debts = self.ledger.repo.list_debts(self.ledger.profile.user_id, include_closed=True)
        plan = finance.plan_month(self.ledger.profile, self.ledger.month_transactions(), subs, self.ledger.today,
                                  events, cash, daily, buffer, debts)
        if plan["estado"] != "ok":
            return plan
        self.notes.append(finance.plan_text(plan))
        # Al modelo solo le llega lo cualitativo: así no puede citar mal (ni inventar) ninguna cifra.
        return {
            "estado": "ok",
            "desglose_enviado": True,
            "necesita_prestamo": plan["monto_a_pedir"] > 0,
            "recortar_no_esenciales_ayuda": plan["ahorro_si_recorta_no_esenciales"] > 0,
            "eventos": [e["descripcion"] for e in plan["eventos"]],
            "eventos_despues_del_pago": plan.get("eventos_despues_del_pago", []),
            "prestamo_vs_ingreso_pct": plan.get("prestamo_vs_ingreso_pct"),
            "nota": ("El usuario ya recibió el desglose con todas las cifras y el monto exacto a pedir. NO escribas "
                     "montos. Da un veredicto corto y 1 a 3 consejos prácticos (cómo abaratar los eventos, qué "
                     "recortar, cómo y cuándo devolver). Este cálculo no reserva su meta de ahorro."),
        }

    def _find_debt(self, person: str, direction: str | None) -> Debt | None:
        target = categories.normalize(person)
        debts = [d for d in self.ledger.repo.list_debts(self.ledger.profile.user_id)
                 if direction in (None, d.direction)]
        exact = [d for d in debts if categories.normalize(d.person) == target]
        partial = exact or [d for d in debts if target in categories.normalize(d.person)
                            or categories.normalize(d.person) in target]
        return sorted(partial, key=lambda d: d.date)[0] if partial else None

    def _t_registrar_prestamo(self, args: dict[str, Any]) -> dict[str, Any]:
        direction = args.get("tipo")
        if direction not in ("me_deben", "debo"):
            raise ToolInputError("tipo debe ser 'me_deben' o 'debo'")
        person = str(args.get("persona") or "").strip()[:60]
        if not person:
            raise ToolInputError("Falta la persona")
        due = self._date(args["fecha_limite"]).isoformat() if args.get("fecha_limite") else None
        debt = Debt(user_id=self.ledger.profile.user_id, direction=direction, person=person,
                    amount=self._amount(args.get("monto")), date=self.ledger.today.isoformat(), due_date=due,
                    note=str(args.get("nota") or "")[:200], moved_money=args.get("movimiento_hoy", True) is not False)
        self.ledger.repo.save_debt(debt)
        same = [d for d in self.ledger.repo.list_debts(self.ledger.profile.user_id)
                if d.direction == direction and categories.normalize(d.person) == categories.normalize(person)]
        return {"registrado": debt.to_public(), "total_con_esa_persona": float(sum(d.balance for d in same)),
                "recordatorio": "Se le recordará antes de la fecha límite" if due else
                "Sin fecha límite: pregúntale si quiere que se lo recuerdes en una fecha"}

    def _t_registrar_abono(self, args: dict[str, Any]) -> dict[str, Any]:
        debt = self._find_debt(str(args.get("persona", "")), args.get("tipo"))
        if debt is None:
            open_debts = [f"{d.person} ({d.direction})" for d in self.ledger.repo.list_debts(self.ledger.profile.user_id)]
            return {"error": "No encontré un préstamo abierto con esa persona", "prestamos_abiertos": open_debts}
        amount = debt.balance if args.get("todo") or not args.get("monto") else self._amount(args["monto"])
        applied = debt.add_payment(amount, self.ledger.today.isoformat())
        self.ledger.repo.save_debt(debt)
        result = {"persona": debt.person, "tipo": debt.direction, "abono": float(applied),
                  "saldo_pendiente": float(debt.balance), "pagado_completo": debt.closed}
        if amount > applied:
            result["nota"] = f"El abono superaba la deuda; solo se aplicaron {float(applied)}"
        return result

    def _t_listar_prestamos(self, args: dict[str, Any]) -> dict[str, Any]:
        debts = self.ledger.repo.list_debts(self.ledger.profile.user_id)
        return {
            "me_deben": [d.to_public() for d in debts if d.direction == "me_deben"],
            "debo": [d.to_public() for d in debts if d.direction == "debo"],
            "total_me_deben": float(sum((d.balance for d in debts if d.direction == "me_deben"), Decimal(0))),
            "total_debo": float(sum((d.balance for d in debts if d.direction == "debo"), Decimal(0))),
        }

    def _t_fijar_gasto_fijo(self, args: dict[str, Any]) -> dict[str, Any]:
        name = categories.normalize(str(args.get("nombre") or ""))[:40]
        if not name:
            raise ToolInputError("Falta el nombre del gasto fijo")
        amount = self._amount(args.get("monto_mensual"), allow_zero=True)
        p = self.ledger.profile
        if amount == 0:
            p.reserves.pop(name, None)
        else:
            category = args.get("categoria") if args.get("categoria") in categories.CATEGORIES else categories.guess(name)
            p.reserves[name] = {"monto": amount, "categoria": category}
        self.ledger.repo.save_profile(p)
        pending = finance.reserves_remaining(p, self.ledger.month_transactions())
        return {"gastos_fijos": [{"nombre": r["nombre"], "mensual": float(r["monto"]), "gastado_este_mes": float(r["gastado"]),
                                  "por_gastar": float(r["pendiente"])} for r in pending]}

    def _account(self, key: Any) -> str:
        if not accounts.is_valid(key):
            raise ToolInputError(f"Cuenta desconocida: {key!r}")
        return str(key)

    def _t_actualizar_saldo_cuenta(self, args: dict[str, Any]) -> dict[str, Any]:
        account = self._account(args.get("cuenta"))
        self.ledger.set_balance(account, self._amount(args.get("saldo"), allow_zero=True))
        return {"cuenta": accounts.label(account), "saldo": float(self.ledger.profile.accounts[account]["saldo"]),
                "nota": "Desde ahora los movimientos con esta cuenta actualizan su saldo"}

    def _t_transferir_entre_cuentas(self, args: dict[str, Any]) -> dict[str, Any]:
        source, target = self._account(args.get("desde")), self._account(args.get("hacia"))
        if source == target:
            raise ToolInputError("Las cuentas de origen y destino son la misma")
        self.ledger.transfer(source, target, self._amount(args.get("monto")))
        accs = self.ledger.profile.accounts
        return {"transferido": float(self._amount(args.get("monto"))),
                "saldos": {accounts.label(k): float(accs[k]["saldo"]) for k in (source, target) if k in accs}}

    def _t_consultar_cuentas(self, args: dict[str, Any]) -> dict[str, Any]:
        return finance.accounts_summary(self.ledger.profile, self.ledger.month_transactions())

    def _t_importar_pantallazo(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.source != "image":
            raise ToolInputError("Esta herramienta solo se usa con pantallazos")
        ledger, profile = self.ledger, self.ledger.profile
        cur = profile.currency
        account = args.get("app") if accounts.is_valid(args.get("app")) else ""
        oldest = ledger.today - timedelta(days=35)
        existing = ledger.repo.list_transactions(profile.user_id, (oldest - timedelta(days=1)).isoformat(),
                                                 ledger.today.isoformat())
        new, dupes, old, unreadable = [], 0, 0, 0
        linked: set[str] = set()
        items = []
        for item in args.get("movimientos") or []:
            # El modelo copia el texto de la pantalla; la conversión la hace el código (formato colombiano).
            amount = parse_screen_amount(str(item.get("monto_texto", ""))) if item.get("monto_texto") else None
            if amount is None and item.get("monto") is not None:
                amount = self._amount(item["monto"])
            if not amount:
                unreadable += 1
                continue
            items.append((item, amount))
        typical = sorted(a for _, a in items)[len(items) // 2] if items else Decimal(0)
        suspicious: list[Transaction] = []
        for item, amount in items:
            when = self._date(item["fecha"]) if item.get("fecha") else ledger.today
            when = min(when, ledger.today)
            if when < oldest:
                old += 1
                continue
            kind = "income" if item.get("tipo") == "ingreso" else "expense"
            if any(t.amount == amount and t.kind == kind and abs((date.fromisoformat(t.date) - when).days) <= 1
                   and (not t.account or not account or t.account == account) for t in existing + new):
                dupes += 1
                continue
            # Lo que el usuario contó por voz/texto días después ("ayer pagué...") es el mismo movimiento:
            # se enlaza con su fecha y cuenta reales en vez de duplicarlo.
            manual = next((t for t in existing if t.source != "image" and t.id not in linked and t.amount == amount
                           and t.kind == kind and 0 <= (date.fromisoformat(t.date) - when).days <= 10
                           and (not t.account or not account or t.account == account)), None)
            if manual is not None:
                ledger.repo.delete_transaction(manual)
                manual.date, manual.account = when.isoformat(), manual.account or account
                manual.merchant = manual.merchant or str(item.get("descripcion") or "")[:40]
                ledger.repo.add_transaction(manual)
                linked.add(manual.id)
                continue
            description = str(item.get("descripcion") or "").strip()[:200]
            category = item.get("categoria")
            if not categories.is_valid(category):
                category = categories.guess(description, profile.merchant_categories)
            if kind == "income" and category not in categories.INCOME_CATEGORIES:
                category = "otros_ingresos"
            if kind == "expense" and category in categories.INCOME_CATEGORIES:
                category = "otros"
            tx = Transaction(user_id=profile.user_id, kind=kind, amount=amount, category=category,
                             description=description, merchant=description[:40], date=when.isoformat(),
                             necessity=categories.default_necessity(category), source="image", account=account)
            self.alerts.extend(ledger.record(tx))
            self.recorded.append(tx)
            new.append(tx)
            if amount >= 1_000_000 and typical and amount >= typical * 20:
                suspicious.append(tx)
        balance_before = profile.accounts.get(account, {}).get("saldo") if account else None
        saldo = parse_screen_amount(str(args["saldo_texto"])) if args.get("saldo_texto") else None
        if saldo is None and args.get("saldo") is not None:
            saldo = self._amount(args["saldo"], allow_zero=True)
        if account and saldo is not None:
            ledger.set_balance(account, saldo)
        self.import_id = new_id()
        ledger.repo.save_import(profile.user_id, self.import_id, {
            "refs": [f"{t.date}:{t.id}" for t in new], "account": account,
            "balance_before": balance_before, "had_balance": balance_before is not None})

        name = accounts.label(account) if account else "una app que no reconocí"
        lines = [f"📸 <b>Leí tu pantallazo de {name}</b>"]
        if account and saldo is not None:
            lines.append(f"Saldo actualizado: <b>{finance.fmt(profile.accounts[account]['saldo'], cur)}</b>")
        if new:
            lines.append(f"Registré {len(new)} movimiento(s) nuevo(s):")
            lines += [f"• {t.date[8:]}/{t.date[5:7]} {'+' if t.kind == 'income' else '−'}{finance.fmt(t.amount, cur)} "
                      f"{t.description} · {categories.label(t.category)}" for t in new[:15]]
        else:
            lines.append("No había movimientos nuevos para registrar.")
        if dupes:
            lines.append(f"Omití {dupes} que ya tenías registrados.")
        if linked:
            lines.append(f"Ajusté {len(linked)} que me habías contado antes a su fecha y cuenta reales.")
        if old:
            lines.append(f"Omití {old} de hace más de un mes.")
        if unreadable:
            lines.append(f"No pude leer el monto de {unreadable}.")
        if suspicious:
            lines.append("⚠️ Revisa estos montos, parecen muy altos: " + ", ".join(
                f"{t.description} {finance.fmt(t.amount, cur)}" for t in suspicious)
                + ". Si están mal, toca «Deshacer importación».")
        self.notes.append("\n".join(lines))
        return {"app": account or "desconocida", "registrados": len(new), "duplicados_omitidos": dupes + len(linked),
                "antiguos_omitidos": old, "saldo_actualizado": bool(account and saldo is not None),
                "nota": "El usuario ya recibió el detalle. Responde en 1-2 líneas sin repetir cifras; si algo "
                        "no se veía claro, pídele que lo confirme."}

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
        if args.get("cuenta_principal"):
            p.default_account = self._account(args["cuenta_principal"])
        self.ledger.repo.save_profile(p)
        return {"perfil": {"cuenta_principal": p.default_account or None, "ingreso_mensual": float(p.monthly_income) if p.monthly_income else None,
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
