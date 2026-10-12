from __future__ import annotations

import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("CUENTAS_BOT_TELEGRAM_TOKEN", "123:TEST")

from cuentas.models import Debt, Profile, Subscription, Transaction  # noqa: E402


class FakeRepo:
    """Implementación en memoria con la misma interfaz que Repository."""

    def __init__(self) -> None:
        self.profiles: dict[int, Profile] = {}
        self.txs: dict[tuple[int, str], Transaction] = {}
        self.subs: dict[tuple[int, str], Subscription] = {}
        self.debts: dict[tuple[int, str], Debt] = {}
        self.imports: dict[tuple[int, str], dict[str, Any]] = {}
        self.messages: list[tuple[int, str, str]] = []
        self.alerts: set[tuple[int, str]] = set()

    def get_profile(self, user_id: int) -> Profile | None:
        return self.profiles.get(user_id)

    def save_profile(self, profile: Profile) -> None:
        self.profiles[profile.user_id] = profile

    def list_users(self):
        return list(self.profiles.values())

    def add_transaction(self, tx: Transaction) -> None:
        self.txs[(tx.user_id, tx.sk)] = tx

    def get_transaction(self, user_id: int, tx_date: str, tx_id: str):
        return self.txs.get((user_id, f"TX#{tx_date}#{tx_id}"))

    def find_transaction(self, user_id: int, tx_id: str):
        return next((t for (u, _), t in self.txs.items() if u == user_id and t.id == tx_id), None)

    def delete_transaction(self, tx: Transaction) -> None:
        self.txs.pop((tx.user_id, tx.sk), None)

    def list_transactions(self, user_id: int, start: str, end: str) -> list[Transaction]:
        return sorted((t for (u, _), t in self.txs.items() if u == user_id and start <= t.date <= end),
                      key=lambda t: t.sk)

    def last_transaction(self, user_id: int):
        mine = [t for (u, _), t in self.txs.items() if u == user_id]
        return max(mine, key=lambda t: t.created_at) if mine else None

    def save_subscription(self, sub: Subscription) -> None:
        self.subs[(sub.user_id, sub.id)] = sub

    def list_subscriptions(self, user_id: int, include_inactive: bool = False):
        return [s for (u, _), s in self.subs.items() if u == user_id and (include_inactive or s.active)]

    def due_subscriptions(self, until: str):
        return [s for s in self.subs.values() if s.active and s.next_charge <= until]

    def save_debt(self, debt: Debt) -> None:
        self.debts[(debt.user_id, debt.id)] = debt

    def list_debts(self, user_id: int, include_closed: bool = False):
        return [d for (u, _), d in self.debts.items() if u == user_id and (include_closed or not d.closed)]

    def get_debt(self, user_id: int, debt_id: str):
        return self.debts.get((user_id, debt_id))

    def save_import(self, user_id: int, import_id: str, data: dict[str, Any], ttl_days: int = 7) -> None:
        self.imports[(user_id, import_id)] = data

    def get_import(self, user_id: int, import_id: str):
        return self.imports.get((user_id, import_id))

    def delete_import(self, user_id: int, import_id: str) -> None:
        self.imports.pop((user_id, import_id), None)

    def add_message(self, user_id: int, role: str, text: str, ttl_days: int = 7) -> None:
        self.messages.append((user_id, role, text))

    def recent_messages(self, user_id: int, limit: int):
        return [{"role": r, "text": t} for u, r, t in self.messages if u == user_id][-limit:]

    def claim_alert(self, user_id: int, key: str, ttl_days: int = 35) -> bool:
        if (user_id, key) in self.alerts:
            return False
        self.alerts.add((user_id, key))
        return True


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, chat_id, text, keyboard=None, reply_to=None):
        self.sent.append({"chat_id": chat_id, "text": text, "keyboard": keyboard})

    def edit(self, chat_id, message_id, text, keyboard=None):
        self.sent.append({"chat_id": chat_id, "text": text, "keyboard": keyboard, "edit": True})

    def typing(self, chat_id, action="typing"):
        pass

    def answer_callback(self, callback_id, text=None):
        pass

    def set_menu_button(self, chat_id, url):
        pass


@pytest.fixture
def repo() -> FakeRepo:
    return FakeRepo()


@pytest.fixture
def tg() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
def profile(repo: FakeRepo) -> Profile:
    p = Profile(user_id=1, chat_id=1, first_name="Ana", currency="COP", timezone="America/Bogota")
    repo.save_profile(p)
    return p


@pytest.fixture
def now() -> datetime:
    # 15 de octubre de 2026, 15:00 en Bogotá
    return datetime(2026, 10, 15, 20, 0, tzinfo=ZoneInfo("UTC"))
