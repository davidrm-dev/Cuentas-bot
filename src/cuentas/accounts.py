"""Cuentas y medios de pago (pensado para Colombia, más algunos internacionales)."""
from __future__ import annotations

import re

from .categories import normalize

# clave -> (nombre visible, emoji, tipo, alias que la gente usa al hablar)
ACCOUNTS: dict[str, tuple[str, str, str, tuple[str, ...]]] = {
    "efectivo": ("Efectivo", "💵", "efectivo", ("efectivo", "cash", "billete", "billetes", "plata fisica")),
    "nequi": ("Nequi", "💜", "billetera", ("nequi",)),
    "daviplata": ("Daviplata", "❤️", "billetera", ("daviplata", "davi plata")),
    "nu": ("Nu", "🟣", "banco digital", ("nu", "nubank", "tarjeta nu", "cajita nu")),
    "bancolombia": ("Bancolombia", "🟡", "banco", ("bancolombia", "banco de colombia")),
    "davivienda": ("Davivienda", "🏠", "banco", ("davivienda",)),
    "bogota": ("Banco de Bogotá", "🏦", "banco", ("banco de bogota",)),
    "bbva": ("BBVA", "🔷", "banco", ("bbva",)),
    "occidente": ("Banco de Occidente", "🏦", "banco", ("occidente", "banco de occidente")),
    "popular": ("Banco Popular", "🏦", "banco", ("banco popular",)),
    "avvillas": ("AV Villas", "🏦", "banco", ("av villas", "avvillas")),
    "colpatria": ("Scotiabank Colpatria", "🏦", "banco", ("colpatria", "scotiabank")),
    "cajasocial": ("Banco Caja Social", "🏦", "banco", ("caja social",)),
    "falabella": ("Banco Falabella", "🟢", "banco", ("banco falabella", "tarjeta cmr", "cmr")),
    "itau": ("Itaú", "🟠", "banco", ("itau",)),
    "lulo": ("Lulo Bank", "🍊", "banco digital", ("lulo bank",)),
    "rappipay": ("RappiPay", "🧡", "banco digital", ("rappipay", "rappi pay", "rappicuenta", "rappi card")),
    "dale": ("dale!", "🔵", "billetera", ("dale",)),
    "movii": ("MOVii", "🟪", "billetera", ("movii",)),
    "uala": ("Ualá", "💙", "banco digital", ("uala",)),
    "tarjeta_credito": ("Tarjeta de crédito", "💳", "credito", ("tarjeta de credito", "credito", "tc")),
    "paypal": ("PayPal", "🅿️", "internacional", ("paypal",)),
    "mercadopago": ("Mercado Pago", "🤝", "internacional", ("mercado pago", "mercadopago")),
    "wise": ("Wise", "🌍", "internacional", ("wise",)),
    "payoneer": ("Payoneer", "🌐", "internacional", ("payoneer",)),
    "binance": ("Binance", "🪙", "internacional", ("binance", "usdt")),
}

# "con nequi", "por daviplata", "desde bancolombia", "en efectivo", "de la nu", "con la tarjeta de crédito"
_PREFIX = r"(?:con|por|desde|en|de|del|a|al|pague con|pagado con)\s+(?:la\s+|el\s+|mi\s+)?(?:cuenta\s+(?:de\s+)?)?"
_ALIASES = sorted(((alias, key) for key, (*_, aliases) in ACCOUNTS.items() for alias in aliases),
                  key=lambda x: -len(x[0]))


def is_valid(key: str | None) -> bool:
    return bool(key) and key in ACCOUNTS


def label(key: str) -> str:
    if key not in ACCOUNTS:
        return key
    name, emoji, *_ = ACCOUNTS[key]
    return f"{emoji} {name}"


def detect(text: str) -> tuple[str | None, str]:
    """Busca una cuenta mencionada ("con nequi") y devuelve (clave, texto sin esa mención)."""
    norm = normalize(text)
    for alias, key in _ALIASES:
        # Alias cortos y ambiguos ("nu", "dale", "tc", "credito") solo cuentan con preposición delante.
        needs_prefix = len(alias) <= 4 or alias in ("credito", "dale", "occidente", "efectivo")
        pattern = rf"\b{_PREFIX}{re.escape(alias)}\b" if needs_prefix else rf"\b(?:{_PREFIX})?{re.escape(alias)}\b"
        match = re.search(pattern, norm)
        if match:
            cleaned = re.sub(r"\s+", " ", norm[:match.start()] + " " + norm[match.end():]).strip()
            return key, cleaned
    return None, norm
