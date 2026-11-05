"""Order effect store: idempotency records and per-step attempt counters.

DynamoDB in AWS (single table, pk=orderId, sk=key, TTL), in-memory for tests.
"""
from __future__ import annotations

import time
from typing import Protocol

TTL_SECONDS = 7 * 24 * 3600


class OrderStore(Protocol):
    def next_attempt(self, order_id: str, step: str) -> int: ...
    def get(self, order_id: str, key: str) -> str | None: ...
    def put(self, order_id: str, key: str, value: str) -> None: ...
    def delete(self, order_id: str, key: str) -> None: ...


class MemoryStore:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}
        self.attempts: dict[tuple[str, str], int] = {}

    def next_attempt(self, order_id: str, step: str) -> int:
        k = (order_id, step)
        self.attempts[k] = self.attempts.get(k, 0) + 1
        return self.attempts[k]

    def get(self, order_id: str, key: str) -> str | None:
        return self.items.get((order_id, key))

    def put(self, order_id: str, key: str, value: str) -> None:
        self.items[(order_id, key)] = value

    def delete(self, order_id: str, key: str) -> None:
        self.items.pop((order_id, key), None)


class DynamoStore:
    def __init__(self, table_name: str, client=None) -> None:
        import boto3
        self.table = table_name
        self.ddb = client or boto3.client("dynamodb")

    @staticmethod
    def _key(order_id: str, sk: str) -> dict:
        return {"orderId": {"S": order_id}, "sk": {"S": sk}}

    def next_attempt(self, order_id: str, step: str) -> int:
        r = self.ddb.update_item(
            TableName=self.table, Key=self._key(order_id, f"attempt#{step}"),
            UpdateExpression="ADD attempts :one SET expiresAt = :ttl",
            ExpressionAttributeValues={":one": {"N": "1"}, ":ttl": {"N": str(int(time.time()) + TTL_SECONDS)}},
            ReturnValues="UPDATED_NEW")
        return int(r["Attributes"]["attempts"]["N"])

    def get(self, order_id: str, key: str) -> str | None:
        r = self.ddb.get_item(TableName=self.table, Key=self._key(order_id, key), ConsistentRead=True)
        return r.get("Item", {}).get("value", {}).get("S")

    def put(self, order_id: str, key: str, value: str) -> None:
        self.ddb.put_item(TableName=self.table, Item={
            **self._key(order_id, key), "value": {"S": value},
            "expiresAt": {"N": str(int(time.time()) + TTL_SECONDS)}})

    def delete(self, order_id: str, key: str) -> None:
        self.ddb.delete_item(TableName=self.table, Key=self._key(order_id, key))
