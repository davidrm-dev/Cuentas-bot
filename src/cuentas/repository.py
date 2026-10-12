"""Acceso a DynamoDB (diseño de tabla única).

PK = USER#<id>
  SK = PROFILE                     perfil + lo aprendido
  SK = TX#<YYYY-MM-DD>#<id>        movimientos (gastos e ingresos)
  SK = SUB#<id>                    suscripciones
  SK = DEBT#<id>                   préstamos (me deben / debo)
  SK = SAVE#<id>                   bolsillos de ahorro
  SK = IMPORT#<id>                 movimientos importados de un pantallazo (TTL, para deshacer)
  SK = MSG#<iso>                   historial corto de conversación (TTL)
  SK = ALERT#<clave>               alertas ya enviadas, para no repetirlas (TTL)
GSI1:
  GSI1PK = USER     -> listar usuarios (scheduler)
  GSI1PK = SUBDUE   -> suscripciones activas ordenadas por próxima fecha de cobro
"""
from __future__ import annotations

import time
from typing import Any, Iterable

from boto3.dynamodb.conditions import Attr, Key

from . import config
from .models import Debt, Profile, SavingsPocket, Subscription, Transaction, to_dynamo


class Repository:
    def __init__(self, table: Any | None = None):
        if table is None:
            import boto3

            table = boto3.resource("dynamodb").Table(config.TABLE_NAME)
        self.table = table

    # ---- perfil -------------------------------------------------------
    def get_profile(self, user_id: int) -> Profile | None:
        item = self.table.get_item(Key={"PK": f"USER#{user_id}", "SK": "PROFILE"}).get("Item")
        return Profile.from_item(item) if item else None

    def save_profile(self, profile: Profile) -> None:
        self.table.put_item(Item=to_dynamo(profile.to_item()))

    def list_users(self) -> Iterable[Profile]:
        kwargs: dict[str, Any] = {"IndexName": "GSI1", "KeyConditionExpression": Key("GSI1PK").eq("USER")}
        for item in self._paginate(**kwargs):
            # El índice proyecta todo el ítem, así que no hace falta otra lectura.
            yield Profile.from_item(item)

    # ---- movimientos --------------------------------------------------
    def add_transaction(self, tx: Transaction) -> None:
        self.table.put_item(Item=to_dynamo(tx.to_item()))

    def get_transaction(self, user_id: int, tx_date: str, tx_id: str) -> Transaction | None:
        item = self.table.get_item(Key={"PK": f"USER#{user_id}", "SK": f"TX#{tx_date}#{tx_id}"}).get("Item")
        return Transaction.from_item(item) if item else None

    def find_transaction(self, user_id: int, tx_id: str) -> Transaction | None:
        """Busca por id sin conocer la fecha (usado por la mini app)."""
        for item in self._paginate(
            KeyConditionExpression=Key("PK").eq(f"USER#{user_id}") & Key("SK").begins_with("TX#"),
            FilterExpression=Attr("id").eq(tx_id),
            ScanIndexForward=False,
        ):
            return Transaction.from_item(item)
        return None

    def delete_transaction(self, tx: Transaction) -> None:
        self.table.delete_item(Key={"PK": f"USER#{tx.user_id}", "SK": tx.sk})

    def list_transactions(self, user_id: int, start: str, end: str) -> list[Transaction]:
        """Movimientos con fecha en [start, end] (ambas inclusive, YYYY-MM-DD)."""
        items = self._paginate(
            KeyConditionExpression=Key("PK").eq(f"USER#{user_id}")
            & Key("SK").between(f"TX#{start}", f"TX#{end}#~"),
        )
        return [Transaction.from_item(i) for i in items]

    def last_transaction(self, user_id: int) -> Transaction | None:
        # El orden por SK es por fecha del gasto; para "el último registrado" usamos created_at.
        recent = self.table.query(
            KeyConditionExpression=Key("PK").eq(f"USER#{user_id}") & Key("SK").begins_with("TX#"),
            ScanIndexForward=False,
            Limit=25,
        ).get("Items", [])
        if not recent:
            return None
        return Transaction.from_item(max(recent, key=lambda i: i.get("created_at", "")))

    # ---- suscripciones -----------------------------------------------
    def save_subscription(self, sub: Subscription) -> None:
        self.table.put_item(Item=to_dynamo(sub.to_item()))

    def list_subscriptions(self, user_id: int, include_inactive: bool = False) -> list[Subscription]:
        items = self._paginate(
            KeyConditionExpression=Key("PK").eq(f"USER#{user_id}") & Key("SK").begins_with("SUB#"),
        )
        subs = [Subscription.from_item(i) for i in items]
        return subs if include_inactive else [s for s in subs if s.active]

    def due_subscriptions(self, until: str) -> list[Subscription]:
        items = self._paginate(
            IndexName="GSI1",
            KeyConditionExpression=Key("GSI1PK").eq("SUBDUE") & Key("GSI1SK").lte(f"{until}#~"),
        )
        return [Subscription.from_item(i) for i in items]

    # ---- préstamos -----------------------------------------------------
    def save_debt(self, debt: Debt) -> None:
        self.table.put_item(Item=to_dynamo(debt.to_item()))

    def list_debts(self, user_id: int, include_closed: bool = False) -> list[Debt]:
        items = self._paginate(
            KeyConditionExpression=Key("PK").eq(f"USER#{user_id}") & Key("SK").begins_with("DEBT#"),
        )
        debts = [Debt.from_item(i) for i in items]
        return debts if include_closed else [d for d in debts if not d.closed]

    def get_debt(self, user_id: int, debt_id: str) -> Debt | None:
        item = self.table.get_item(Key={"PK": f"USER#{user_id}", "SK": f"DEBT#{debt_id}"}).get("Item")
        return Debt.from_item(item) if item else None

    # ---- ahorro -------------------------------------------------------
    def save_pocket(self, pocket: SavingsPocket) -> None:
        self.table.put_item(Item=to_dynamo(pocket.to_item()))

    def list_pockets(self, user_id: int) -> list[SavingsPocket]:
        items = self._paginate(
            KeyConditionExpression=Key("PK").eq(f"USER#{user_id}") & Key("SK").begins_with("SAVE#"),
        )
        return [SavingsPocket.from_item(i) for i in items]

    def delete_pocket(self, pocket: SavingsPocket) -> None:
        self.table.delete_item(Key={"PK": f"USER#{pocket.user_id}", "SK": pocket.sk})

    # ---- importaciones de pantallazos (para poder deshacerlas) -------
    def save_import(self, user_id: int, import_id: str, data: dict[str, Any], ttl_days: int = 7) -> None:
        self.table.put_item(Item=to_dynamo({"PK": f"USER#{user_id}", "SK": f"IMPORT#{import_id}", **data,
                                            "ttl": int(time.time() + ttl_days * 86400)}))

    def get_import(self, user_id: int, import_id: str) -> dict[str, Any] | None:
        return self.table.get_item(Key={"PK": f"USER#{user_id}", "SK": f"IMPORT#{import_id}"}).get("Item")

    def delete_import(self, user_id: int, import_id: str) -> None:
        self.table.delete_item(Key={"PK": f"USER#{user_id}", "SK": f"IMPORT#{import_id}"})

    # ---- historial de conversación -----------------------------------
    def add_message(self, user_id: int, role: str, text: str, ttl_days: int = 7) -> None:
        now = time.time()
        self.table.put_item(Item={
            "PK": f"USER#{user_id}",
            "SK": f"MSG#{now:.6f}",
            "role": role,
            "text": text[:2000],
            "ttl": int(now + ttl_days * 86400),
        })

    def recent_messages(self, user_id: int, limit: int) -> list[dict[str, str]]:
        items = self.table.query(
            KeyConditionExpression=Key("PK").eq(f"USER#{user_id}") & Key("SK").begins_with("MSG#"),
            ScanIndexForward=False,
            Limit=limit,
        ).get("Items", [])
        return [{"role": i["role"], "text": i["text"]} for i in reversed(items)]

    # ---- deduplicación de alertas ------------------------------------
    def claim_alert(self, user_id: int, key: str, ttl_days: int = 35) -> bool:
        """Devuelve True solo la primera vez que se reclama la clave (envío único)."""
        from botocore.exceptions import ClientError

        try:
            self.table.put_item(
                Item={"PK": f"USER#{user_id}", "SK": f"ALERT#{key}", "ttl": int(time.time() + ttl_days * 86400)},
                ConditionExpression="attribute_not_exists(SK)",
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    # ---- util ---------------------------------------------------------
    def _paginate(self, **kwargs: Any) -> Iterable[dict[str, Any]]:
        while True:
            page = self.table.query(**kwargs)
            yield from page.get("Items", [])
            if "LastEvaluatedKey" not in page:
                return
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
