"""Entidades del dominio. Los montos siempre son Decimal (DynamoDB no acepta float)."""
from __future__ import annotations

import calendar
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from . import config

CENT = Decimal("0.01")


def money(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        d = value
    else:
        d = Decimal(str(value).replace(",", "."))
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Transaction:
    user_id: int
    kind: str  # "expense" | "income"
    amount: Decimal
    category: str
    description: str
    date: str  # YYYY-MM-DD en la zona horaria del usuario
    merchant: str = ""
    necessity: str = "discrecional"  # "esencial" | "discrecional"
    source: str = "text"  # "text" | "voice" | "subscription" | "webapp"
    currency: str = ""
    id: str = field(default_factory=new_id)
    created_at: str = field(default_factory=utcnow_iso)

    @property
    def sk(self) -> str:
        return f"TX#{self.date}#{self.id}"

    def to_item(self) -> dict[str, Any]:
        item = asdict(self)
        item.update(PK=f"USER#{self.user_id}", SK=self.sk, type="tx")
        return item

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> "Transaction":
        return cls(
            user_id=int(item["user_id"]),
            kind=item["kind"],
            amount=money(item["amount"]),
            category=item["category"],
            description=item.get("description", ""),
            date=item["date"],
            merchant=item.get("merchant", ""),
            necessity=item.get("necessity", "discrecional"),
            source=item.get("source", "text"),
            currency=item.get("currency", ""),
            id=item["id"],
            created_at=item.get("created_at", ""),
        )

    def to_public(self) -> dict[str, Any]:
        data = asdict(self)
        data["amount"] = float(self.amount)
        data.pop("user_id")
        return data


CYCLES = ("weekly", "monthly", "yearly")


def add_cycle(d: date, cycle: str, anchor_day: int | None = None) -> date:
    """Siguiente fecha de cobro conservando el día ancla (ej. día 31 -> fin de mes)."""
    if cycle == "weekly":
        return date.fromordinal(d.toordinal() + 7)
    months = 12 if cycle == "yearly" else 1
    year, month = divmod(d.month - 1 + months, 12)
    year += d.year
    month += 1
    day = min(anchor_day or d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


@dataclass
class Subscription:
    user_id: int
    name: str
    amount: Decimal
    cycle: str  # weekly | monthly | yearly
    next_charge: str  # YYYY-MM-DD
    category: str = "suscripciones"
    anchor_day: int | None = None
    active: bool = True
    id: str = field(default_factory=new_id)
    created_at: str = field(default_factory=utcnow_iso)

    @property
    def sk(self) -> str:
        return f"SUB#{self.id}"

    @property
    def monthly_cost(self) -> Decimal:
        if self.cycle == "weekly":
            return money(self.amount * Decimal(52) / Decimal(12))
        if self.cycle == "yearly":
            return money(self.amount / Decimal(12))
        return self.amount

    def advance(self) -> None:
        current = date.fromisoformat(self.next_charge)
        self.next_charge = add_cycle(current, self.cycle, self.anchor_day).isoformat()

    def to_item(self) -> dict[str, Any]:
        item = asdict(self)
        item.update(PK=f"USER#{self.user_id}", SK=self.sk, type="sub")
        if self.active:
            # Índice para que el scheduler encuentre cobros del día sin escanear la tabla.
            item.update(GSI1PK="SUBDUE", GSI1SK=f"{self.next_charge}#{self.user_id}#{self.id}")
        return item

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> "Subscription":
        anchor = item.get("anchor_day")
        return cls(
            user_id=int(item["user_id"]),
            name=item["name"],
            amount=money(item["amount"]),
            cycle=item.get("cycle", "monthly"),
            next_charge=item["next_charge"],
            category=item.get("category", "suscripciones"),
            anchor_day=int(anchor) if anchor is not None else None,
            active=bool(item.get("active", True)),
            id=item["id"],
            created_at=item.get("created_at", ""),
        )

    def to_public(self) -> dict[str, Any]:
        data = asdict(self)
        data["amount"] = float(self.amount)
        data["monthly_cost"] = float(self.monthly_cost)
        data.pop("user_id")
        return data


@dataclass
class Profile:
    user_id: int
    chat_id: int
    first_name: str = ""
    currency: str = field(default_factory=lambda: config.DEFAULT_CURRENCY)
    timezone: str = field(default_factory=lambda: config.DEFAULT_TIMEZONE)
    monthly_income: Decimal | None = None  # declarado por el usuario
    payday: int | None = None  # día del mes en que cobra
    savings_goal_pct: Decimal = Decimal("10")
    budgets: dict[str, Decimal] = field(default_factory=dict)  # categoria -> tope mensual
    # Lo que el bot aprende del usuario (ver learning.py)
    merchant_categories: dict[str, str] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    insights: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utcnow_iso)

    def to_item(self) -> dict[str, Any]:
        item = asdict(self)
        item.update(PK=f"USER#{self.user_id}", SK="PROFILE", type="profile",
                    GSI1PK="USER", GSI1SK=str(self.user_id))
        if self.monthly_income is None:
            item.pop("monthly_income")
        if self.payday is None:
            item.pop("payday")
        return item

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> "Profile":
        income = item.get("monthly_income")
        payday = item.get("payday")
        return cls(
            user_id=int(item["user_id"]),
            chat_id=int(item["chat_id"]),
            first_name=item.get("first_name", ""),
            currency=item.get("currency", config.DEFAULT_CURRENCY),
            timezone=item.get("timezone", config.DEFAULT_TIMEZONE),
            monthly_income=money(income) if income is not None else None,
            payday=int(payday) if payday is not None else None,
            savings_goal_pct=Decimal(str(item.get("savings_goal_pct", "10"))),
            budgets={k: money(v) for k, v in (item.get("budgets") or {}).items()},
            merchant_categories=dict(item.get("merchant_categories") or {}),
            stats=_plain(item.get("stats") or {}),
            insights=list(item.get("insights") or []),
            created_at=item.get("created_at", ""),
        )


def _plain(value: Any) -> Any:
    """Convierte Decimals anidados (como los devuelve DynamoDB) a int/float."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def to_dynamo(value: Any) -> Any:
    """Convierte floats a Decimal antes de escribir en DynamoDB."""
    if isinstance(value, float):
        return Decimal(str(round(value, 6)))
    if isinstance(value, dict):
        return {k: to_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_dynamo(v) for v in value]
    return value
