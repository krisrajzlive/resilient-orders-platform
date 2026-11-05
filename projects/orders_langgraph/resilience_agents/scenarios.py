"""Chaos scenarios the agent may run against the order saga, with the invariants each must hold.

The catalog is the allowlist: the model can only choose from these names, and each outcome is
judged by deterministic invariants, not by the model.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    faults: dict[str, str]
    expect_status: str
    expect_outcome: str | None = None
    expect_error: str | None = None
    include: tuple[str, ...] = ()           # states that must appear, in this order
    exclude: tuple[str, ...] = ()           # states that must not appear
    amount: float = 120.0


CATALOG: dict[str, Scenario] = {s.name: s for s in [
    Scenario("happy_path", "No faults: baseline order is confirmed.", {}, "SUCCEEDED", "CONFIRMED",
             include=("ReserveStock", "ChargePayment", "CreateShipment"), exclude=("ReleaseStock", "RefundPayment")),
    Scenario("payment_flaky", "Payment fails twice then recovers: retries must absorb it.",
             {"ChargePayment": "transient"}, "SUCCEEDED", "CONFIRMED",
             include=("ChargePayment", "CreateShipment"), exclude=("ReleaseStock",)),
    Scenario("payment_down", "Payment permanently unavailable: stock must be released.",
             {"ChargePayment": "permanent"}, "SUCCEEDED", "REJECTED",
             include=("ChargePayment", "ReleaseStock"), exclude=("RefundPayment", "CreateShipment")),
    Scenario("inventory_down", "Stock reservation permanently fails: nothing to undo.",
             {"ReserveStock": "permanent"}, "SUCCEEDED", "REJECTED",
             exclude=("ChargePayment", "RefundPayment", "ReleaseStock")),
    Scenario("shipping_down", "Shipping permanently fails: refund then release, in that order.",
             {"CreateShipment": "permanent"}, "SUCCEEDED", "REJECTED",
             include=("CreateShipment", "RefundPayment", "ReleaseStock")),
    Scenario("refund_and_shipping_down", "Shipping and refund both fail: must escalate, never silently end.",
             {"CreateShipment": "permanent", "RefundPayment": "permanent"}, "FAILED",
             expect_error="CompensationFailed", include=("RefundPayment", "ManualReview"),
             exclude=("ReleaseStock",)),
]}


def build_order(s: Scenario) -> dict:
    return {"orderId": f"agent-{s.name[:10]}-{uuid.uuid4().hex[:6]}", "customerId": "c-agent",
            "items": [{"sku": "P001", "qty": 1}], "amount": s.amount, **({"faults": s.faults} if s.faults else {})}


@dataclass
class Verdict:
    scenario: str
    passed: bool
    failures: list[str] = field(default_factory=list)


def _is_subsequence(wanted: tuple[str, ...], states: list[str]) -> bool:
    it = iter(states)
    return all(any(w == s for s in it) for w in wanted)


def evaluate(s: Scenario, summary: dict) -> Verdict:
    failures = []
    if summary.get("status") != s.expect_status:
        failures.append(f"status {summary.get('status')} != {s.expect_status}")
    if s.expect_outcome and summary.get("outcome") != s.expect_outcome:
        failures.append(f"outcome {summary.get('outcome')} != {s.expect_outcome}")
    if s.expect_error and summary.get("error") != s.expect_error:
        failures.append(f"error {summary.get('error')} != {s.expect_error}")
    states = summary.get("states", [])
    if s.include and not _is_subsequence(s.include, states):
        failures.append(f"expected states in order {list(s.include)}")
    for bad in s.exclude:
        if bad in states:
            failures.append(f"unexpected state {bad}")
    return Verdict(s.name, not failures, failures)
