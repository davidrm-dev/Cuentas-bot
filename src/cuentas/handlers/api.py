"""API de la Mini App (dashboard). Autenticada con el initData firmado por Telegram."""
from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from .. import accounts, categories, config, finance
from ..finance import month_bounds
from ..ledger import Ledger
from ..models import Profile, money
from ..repository import Repository
from ..webapp_auth import InvalidInitData, validate_init_data

log = logging.getLogger()
log.setLevel(logging.INFO)
_repo: Repository | None = None
HISTORY_MONTHS = 6


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _response(status: int, body: Any) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body, ensure_ascii=False, default=str),
    }


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    global _repo
    _repo = _repo or Repository()
    try:
        profile = _authenticate(_repo, event)
        return _route(_repo, profile, event)
    except HttpError as exc:
        return _response(exc.status, {"error": str(exc)})
    except Exception:  # noqa: BLE001
        log.exception("Error en API")
        return _response(500, {"error": "Error interno"})


def _authenticate(repo: Repository, event: dict[str, Any]) -> Profile:
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    auth = headers.get("authorization", "")
    if not auth.startswith("tma "):
        raise HttpError(401, "Falta autorización")
    try:
        user = validate_init_data(auth[4:], config.bot_token())
    except InvalidInitData as exc:
        raise HttpError(401, str(exc))
    profile = repo.get_profile(int(user["id"]))
    if profile is None:
        raise HttpError(404, "Primero escríbele /start al bot")
    return profile


def _body(event: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        raise HttpError(400, "JSON inválido")


def _route(repo: Repository, profile: Profile, event: dict[str, Any]) -> dict[str, Any]:
    route = event.get("routeKey", "")
    params = event.get("pathParameters") or {}
    ledger = Ledger(repo, profile)

    if route == "GET /api/dashboard":
        qs = event.get("queryStringParameters") or {}
        month = ledger.today
        if qs.get("month"):
            if not re.fullmatch(r"\d{4}-\d{2}", qs["month"]):
                raise HttpError(400, "Mes inválido (YYYY-MM)")
            month = date.fromisoformat(qs["month"] + "-01")
        start, end = month_bounds(month)
        history_start = month_bounds(start - timedelta(days=31 * (HISTORY_MONTHS - 1)))[0]
        history = repo.list_transactions(profile.user_id, history_start.isoformat(), end.isoformat())
        month_txs = [t for t in history if start.isoformat() <= t.date <= end.isoformat()]
        subs = repo.list_subscriptions(profile.user_id)
        debts = repo.list_debts(profile.user_id, include_closed=True)
        pockets = repo.list_pockets(profile.user_id)
        return _response(200, finance.dashboard(profile, month_txs, history, subs, month, ledger.today, debts,
                                                pockets))

    if route in ("PATCH /api/transactions/{id}", "DELETE /api/transactions/{id}"):
        tx = repo.find_transaction(profile.user_id, params.get("id", ""))
        if tx is None:
            raise HttpError(404, "Movimiento no encontrado")
        if route.startswith("DELETE"):
            ledger.delete(tx)
            return _response(200, {"deleted": tx.id})
        body = _body(event)
        if "category" in body:
            if not categories.is_valid(body["category"]):
                raise HttpError(400, "Categoría inválida")
            ledger.recategorize(tx, body["category"])
        if "amount" in body:
            ledger.change_amount(tx, _positive(body["amount"]))
        if "account" in body:
            if body["account"] and not accounts.is_valid(body["account"]):
                raise HttpError(400, "Cuenta inválida")
            ledger.change_account(tx, body["account"] or "")
        return _response(200, tx.to_public())

    if route == "PUT /api/budgets/{category}":
        category = params.get("category", "")
        if category not in categories.CATEGORIES:
            raise HttpError(400, "Categoría inválida")
        amount = _positive(_body(event).get("amount", 0), allow_zero=True)
        if amount == 0:
            profile.budgets.pop(category, None)
        else:
            profile.budgets[category] = amount
        repo.save_profile(profile)
        return _response(200, {"budgets": {k: float(v) for k, v in profile.budgets.items()}})

    if route == "PUT /api/profile":
        body = _body(event)
        if "monthly_income" in body:
            profile.monthly_income = _positive(body["monthly_income"]) if body["monthly_income"] else None
        if "savings_goal_pct" in body:
            pct = Decimal(str(body["savings_goal_pct"]))
            if not 0 <= pct <= 90:
                raise HttpError(400, "Meta de ahorro fuera de rango")
            profile.savings_goal_pct = pct
        if "payday" in body:
            payday = body["payday"]
            if payday is not None and not (isinstance(payday, int) and 1 <= payday <= 31):
                raise HttpError(400, "Día de pago inválido")
            profile.payday = payday
        repo.save_profile(profile)
        return _response(200, {"ok": True})

    raise HttpError(404, "Ruta no encontrada")


def _positive(value: Any, allow_zero: bool = False) -> Decimal:
    try:
        amount = money(value)
    except (InvalidOperation, ValueError, TypeError):
        raise HttpError(400, "Monto inválido")
    if amount < 0 or (amount == 0 and not allow_zero):
        raise HttpError(400, "El monto debe ser positivo")
    return amount
