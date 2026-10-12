"""Parser de respaldo para cuando el modelo no está disponible.

Entiende frases simples como "gasté 12.500 en uber" o "me pagaron 2000000 de salario".
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from . import categories
from .models import money

_INCOME = re.compile(r"\b(me pagaron|cobre|recibi|ingreso|me depositaron|me transfirieron|gane)\b")
_EXPENSE = re.compile(r"\b(gaste|pague|compre|me cobraron|gasto|pago|compra)\b")
_AMOUNT = re.compile(r"(\d{1,3}(?:[.\s]\d{3})+(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)\s*(mil|k|lucas?)?")


@dataclass
class QuickEntry:
    kind: str
    amount: Decimal
    category: str
    description: str


def parse_amount(raw: str, suffix: str | None = None) -> Decimal:
    raw = raw.replace(" ", "")
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?", raw):
        raw = raw.replace(".", "").replace(",", ".")  # 12.500,50 -> 12500.50
    else:
        raw = raw.replace(",", ".")
    value = Decimal(raw)
    if suffix in ("mil", "k", "luca", "lucas"):
        value *= 1000
    return money(value)


def quick_parse(text: str, learned: dict[str, str] | None = None) -> QuickEntry | None:
    norm = categories.normalize(text)
    is_income = bool(_INCOME.search(norm))
    if not is_income and not _EXPENSE.search(norm):
        return None
    match = _AMOUNT.search(norm)
    if not match:
        return None
    amount = parse_amount(match.group(1), match.group(2))
    if amount <= 0:
        return None
    rest = re.sub(r"\s+", " ", (norm[:match.start()] + " " + norm[match.end():])).strip()
    rest = re.sub(r"^(gaste|pague|compre|me cobraron|me pagaron|cobre|recibi|gane)\s+", "", rest)
    rest = re.sub(r"^(en|de|por|del)\s+", "", rest).strip() or text.strip()
    if is_income:
        category = "salario" if "salario" in norm or "sueldo" in norm or "nomina" in norm else "otros_ingresos"
    else:
        category = categories.guess(rest, learned)
    return QuickEntry("income" if is_income else "expense", amount, category, rest[:120])
