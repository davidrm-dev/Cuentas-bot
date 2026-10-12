"""Catálogo de categorías y clasificador por palabras clave (respaldo del LLM)."""
from __future__ import annotations

import re
import unicodedata

# clave -> (nombre visible, emoji, naturaleza por defecto)
CATEGORIES: dict[str, tuple[str, str, str]] = {
    "vivienda": ("Vivienda", "🏠", "esencial"),
    "servicios": ("Servicios (luz, agua, internet)", "💡", "esencial"),
    "supermercado": ("Supermercado", "🛒", "esencial"),
    "restaurantes": ("Restaurantes y cafés", "🍽️", "discrecional"),
    "transporte": ("Transporte", "🚌", "esencial"),
    "salud": ("Salud", "🩺", "esencial"),
    "educacion": ("Educación", "📚", "esencial"),
    "ocio": ("Ocio y salidas", "🎉", "discrecional"),
    "compras": ("Compras y ropa", "🛍️", "discrecional"),
    "suscripciones": ("Suscripciones", "🔁", "discrecional"),
    "deudas": ("Deudas y créditos", "💳", "esencial"),
    "mascotas": ("Mascotas", "🐾", "esencial"),
    "regalos": ("Regalos y donaciones", "🎁", "discrecional"),
    "viajes": ("Viajes", "✈️", "discrecional"),
    "ahorro": ("Ahorro e inversión", "🏦", "esencial"),
    "otros": ("Otros", "📦", "discrecional"),
}

INCOME_CATEGORIES: dict[str, tuple[str, str]] = {
    "salario": ("Salario", "💼"),
    "freelance": ("Trabajos extra", "🧾"),
    "inversiones": ("Rendimientos", "📈"),
    "otros_ingresos": ("Otros ingresos", "💰"),
}

# Categorías que cuentan como "salir" para la pregunta "¿debería salir?".
OUTING_CATEGORIES = ("ocio", "restaurantes")

_KEYWORDS: dict[str, tuple[str, ...]] = {
    "vivienda": ("arriendo", "alquiler", "renta", "hipoteca", "administracion", "condominio"),
    "servicios": ("luz", "agua", "gas", "internet", "electricidad", "telefono", "celular", "plan movil"),
    "supermercado": ("super", "supermercado", "mercado", "despensa", "verduras", "frutas", "carniceria", "panaderia"),
    "restaurantes": ("restaurante", "almuerzo", "cena", "desayuno", "cafe", "comida", "pizza", "hamburguesa",
                     "sushi", "rappi", "domicilio", "delivery", "starbucks", "mcdonald", "kfc"),
    "transporte": ("uber", "taxi", "didi", "cabify", "bus", "metro", "gasolina", "nafta", "combustible",
                   "peaje", "parqueadero", "estacionamiento", "transporte", "pasaje"),
    "salud": ("farmacia", "medico", "doctor", "medicina", "dentista", "seguro medico", "eps", "laboratorio"),
    "educacion": ("curso", "colegio", "universidad", "matricula", "libro", "clases"),
    "ocio": ("cine", "fiesta", "bar", "cerveza", "trago", "concierto", "discoteca", "juego", "salida", "boliche"),
    "compras": ("ropa", "zapatos", "amazon", "mercadolibre", "tienda", "camisa", "pantalon", "regalo para mi"),
    "suscripciones": ("netflix", "spotify", "disney", "hbo", "max", "youtube premium", "icloud", "chatgpt",
                      "prime video", "gimnasio", "gym", "suscripcion"),
    "deudas": ("tarjeta", "credito", "prestamo", "cuota", "deuda"),
    "mascotas": ("veterinario", "perro", "gato", "mascota", "concentrado"),
    "regalos": ("regalo", "donacion", "cumpleanos"),
    "viajes": ("hotel", "vuelo", "airbnb", "viaje", "avion"),
    "ahorro": ("ahorro", "inversion", "cdt", "plazo fijo"),
}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text).strip()


def is_valid(category: str | None) -> bool:
    return bool(category) and (category in CATEGORIES or category in INCOME_CATEGORIES)


def label(category: str) -> str:
    if category in CATEGORIES:
        name, emoji, _ = CATEGORIES[category]
    elif category in INCOME_CATEGORIES:
        name, emoji = INCOME_CATEGORIES[category]
    else:
        return category
    return f"{emoji} {name}"


def default_necessity(category: str) -> str:
    return CATEGORIES.get(category, ("", "", "discrecional"))[2]


def guess(description: str, learned: dict[str, str] | None = None) -> str:
    """Clasifica por lo aprendido del usuario primero y luego por palabras clave."""
    text = normalize(description)
    if learned:
        # Lo aprendido gana: el usuario ya corrigió o confirmó ese comercio.
        for merchant, category in sorted(learned.items(), key=lambda kv: -len(kv[0])):
            if merchant and re.search(rf"\b{re.escape(merchant)}\b", text):
                return category
    for category, words in _KEYWORDS.items():
        for word in words:
            if re.search(rf"\b{re.escape(word)}\b", text):
                return category
    return "otros"
