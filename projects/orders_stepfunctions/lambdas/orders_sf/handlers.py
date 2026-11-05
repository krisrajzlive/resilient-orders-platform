"""Lambda entry points, one per workflow step.

Every step is idempotent (a retry after success returns the recorded result) and supports
fault injection through `event["faults"][<Step>]` = "transient" | "permanent", so the
workflow's retry and compensation paths can be exercised on demand in a real account.
"""
from __future__ import annotations

import os

from . import risk
from .errors import PermanentError, TransientError
from .store import DynamoStore, OrderStore

TRANSIENT_FAILURES = 2          # a "transient" fault fails this many attempts, then succeeds

_store: OrderStore | None = None
_models: list | None = None


def get_store() -> OrderStore:
    global _store
    if _store is None:
        _store = DynamoStore(os.environ["ORDERS_TABLE"])
    return _store


def get_models() -> list:
    global _models
    if _models is None:
        _models = risk.default_models()
    return _models


def _inject_fault(event: dict, step: str, store: OrderStore) -> int:
    attempt = store.next_attempt(event["orderId"], step)
    plan = (event.get("faults") or {}).get(step, "none")
    if plan == "permanent":
        raise PermanentError(f"{step} failed permanently")
    if plan == "transient" and attempt <= TRANSIENT_FAILURES:
        raise TransientError(f"{step} transient failure on attempt {attempt}")
    return attempt


def _effect(event: dict, step: str, key: str, make) -> dict:
    """Run a forward step once: return the recorded effect if it already happened."""
    store = get_store()
    existing = store.get(event["orderId"], key)
    if existing:
        return {"id": existing, "replayed": True}
    attempt = _inject_fault(event, step, store)
    value = make(event)
    store.put(event["orderId"], key, value)
    return {"id": value, "attempt": attempt, "replayed": False}


def _undo(event: dict, step: str, key: str) -> dict:
    store = get_store()
    attempt = _inject_fault(event, step, store)
    store.delete(event["orderId"], key)
    return {"undone": key, "attempt": attempt}


def assess_risk(event: dict, context=None) -> dict:
    return risk.assess(event, get_models())


def reserve_stock(event: dict, context=None) -> dict:
    return _effect(event, "ReserveStock", "reservation", lambda e: f"res-{e['orderId']}")


def charge_payment(event: dict, context=None) -> dict:
    return _effect(event, "ChargePayment", "payment", lambda e: f"pay-{e['orderId']}")


def create_shipment(event: dict, context=None) -> dict:
    return _effect(event, "CreateShipment", "shipment", lambda e: f"shp-{e['orderId']}")


def refund_payment(event: dict, context=None) -> dict:
    return _undo(event, "RefundPayment", "payment")


def release_stock(event: dict, context=None) -> dict:
    return _undo(event, "ReleaseStock", "reservation")
