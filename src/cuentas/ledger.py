"""Operaciones del libro contable compartidas por el bot, la mini app y el scheduler."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from . import learning
from .finance import Alert, check_alerts, month_bounds, today_for
from .models import Profile, Subscription, Transaction, money
from .repository import Repository


class Ledger:
    def __init__(self, repo: Repository, profile: Profile, now: datetime | None = None):
        self.repo = repo
        self.profile = profile
        self.now = now or datetime.now(tz=ZoneInfo("UTC"))
        self.today = today_for(profile, self.now)

    @property
    def local_hour(self) -> int:
        return self.now.astimezone(ZoneInfo(self.profile.timezone)).hour

    def month_transactions(self, ref: date | None = None) -> list[Transaction]:
        start, end = month_bounds(ref or self.today)
        return self.repo.list_transactions(self.profile.user_id, start.isoformat(), end.isoformat())

    # ---- saldos por cuenta ------------------------------------------
    def _apply_balance(self, tx: Transaction, sign: int) -> None:
        """Ajusta el saldo de la cuenta del movimiento, si el usuario lleva saldo de esa cuenta."""
        if tx.kind == "transfer":  # sale de la cuenta origen y entra a la destino (guardada en merchant)
            moves = [(tx.account, -tx.amount), (tx.merchant, tx.amount)]
        else:
            moves = [(tx.account, tx.amount if tx.kind == "income" else -tx.amount)]
        for account, delta in moves:
            acc = self.profile.accounts.get(account)
            if acc is not None:
                acc["saldo"] = money(acc["saldo"] + sign * delta)

    def set_balance(self, account: str, balance: Decimal) -> None:
        self.profile.accounts[account] = {"saldo": money(balance), "actualizado": self.today.isoformat()}
        self.repo.save_profile(self.profile)

    def transfer(self, source: str, target: str, amount: Decimal, when: str | None = None,
                 source_kind: str = "text") -> Transaction:
        """Mover plata entre cuentas propias no es gasto: queda registrado y mueve los saldos que se lleven."""
        from .accounts import ACCOUNTS

        tx = Transaction(user_id=self.profile.user_id, kind="transfer", amount=amount, category="otros",
                         description=f"Transferencia a {ACCOUNTS.get(target, (target,))[0]}", merchant=target,
                         date=when or self.today.isoformat(), account=source, source=source_kind,
                         necessity="esencial")
        self.record(tx, alert=False)
        return tx

    def change_account(self, tx: Transaction, account: str) -> Transaction:
        self._apply_balance(tx, -1)
        tx.account = account
        self._apply_balance(tx, +1)
        self.repo.add_transaction(tx)
        self.repo.save_profile(self.profile)
        return tx

    def record(self, tx: Transaction, alert: bool = True) -> list[Alert]:
        """Guarda el movimiento, aprende de él y devuelve alertas nuevas (no repetidas)."""
        tx.currency = tx.currency or self.profile.currency
        if not tx.account and self.profile.default_account and tx.source != "subscription":
            tx.account = self.profile.default_account
        self.repo.add_transaction(tx)
        self._apply_balance(tx, +1)
        learning.observe_transaction(self.profile, tx, self.local_hour)
        self.repo.save_profile(self.profile)
        if not alert or tx.kind != "expense":
            return []
        tx_day = date.fromisoformat(tx.date)
        if month_bounds(tx_day) != month_bounds(self.today):
            return []  # gastos de meses pasados no disparan alertas del mes en curso
        subs = self.repo.list_subscriptions(self.profile.user_id)
        debts = self.repo.list_debts(self.profile.user_id, include_closed=True)
        pockets = self.repo.list_pockets(self.profile.user_id)
        alerts = check_alerts(self.profile, tx, self.month_transactions(), subs, self.today, debts, pockets)
        return [a for a in alerts if self.repo.claim_alert(self.profile.user_id, a.key)]

    def recategorize(self, tx: Transaction, category: str) -> Transaction:
        old = tx.category
        if old == category:
            return tx
        tx.category = category
        self.repo.add_transaction(tx)
        learning.observe_correction(self.profile, tx, old)
        self.repo.save_profile(self.profile)
        return tx

    def change_amount(self, tx: Transaction, amount: Decimal) -> Transaction:
        self._apply_balance(tx, -1)
        tx.amount = amount
        self._apply_balance(tx, +1)
        self.repo.add_transaction(tx)
        self.repo.save_profile(self.profile)
        return tx

    def delete(self, tx: Transaction) -> None:
        self.repo.delete_transaction(tx)
        self._apply_balance(tx, -1)
        self.repo.save_profile(self.profile)

    def find_subscription(self, name: str) -> Subscription | None:
        from .categories import normalize

        target = normalize(name)
        subs = self.repo.list_subscriptions(self.profile.user_id)
        exact = [s for s in subs if normalize(s.name) == target]
        if exact:
            return exact[0]
        partial = [s for s in subs if target in normalize(s.name) or normalize(s.name) in target]
        return partial[0] if len(partial) == 1 else None

    def charge_subscription(self, sub: Subscription) -> list[Alert]:
        """Registra el cobro de una suscripción y la mueve al siguiente ciclo."""
        tx = Transaction(
            user_id=sub.user_id, kind="expense", amount=sub.amount, category=sub.category,
            description=f"Suscripción: {sub.name}", merchant=sub.name, date=sub.next_charge,
            necessity="discrecional" if sub.category == "suscripciones" else "esencial",
            source="subscription",
        )
        alerts = self.record(tx)
        sub.advance()
        self.repo.save_subscription(sub)
        return alerts
