from __future__ import annotations

import pytest

from orders_sf import handlers, risk
from orders_sf.errors import PermanentError, TransientError
from orders_sf.store import MemoryStore


@pytest.fixture(autouse=True)
def store(monkeypatch):
    s = MemoryStore()
    monkeypatch.setattr(handlers, "_store", s)
    return s


def order(**kw):
    return {"orderId": "o-1", "customerId": "c-1", "amount": 120.0, "items": [{"sku": "P001", "qty": 1}], **kw}


def test_forward_step_is_idempotent(store):
    first = handlers.reserve_stock(order())
    again = handlers.reserve_stock(order())
    assert first["replayed"] is False and again["replayed"] is True
    assert first["id"] == again["id"] == "res-o-1"
    assert store.attempts[("o-1", "ReserveStock")] == 1      # the replay did not count as an attempt


def test_transient_fault_fails_twice_then_succeeds():
    e = order(faults={"ChargePayment": "transient"})
    for _ in range(2):
        with pytest.raises(TransientError):
            handlers.charge_payment(e)
    assert handlers.charge_payment(e)["attempt"] == 3


def test_permanent_fault_never_succeeds():
    e = order(faults={"CreateShipment": "permanent"})
    for _ in range(4):
        with pytest.raises(PermanentError):
            handlers.create_shipment(e)


def test_error_class_names_match_what_the_workflow_catches():
    assert TransientError.__name__ == "TransientError" and PermanentError.__name__ == "PermanentError"


def test_compensation_undoes_recorded_effects(store):
    handlers.reserve_stock(order())
    handlers.charge_payment(order())
    handlers.refund_payment(order())
    handlers.release_stock(order())
    assert store.items == {}


def test_compensation_supports_fault_injection():
    with pytest.raises(PermanentError):
        handlers.refund_payment(order(faults={"RefundPayment": "permanent"}))


# -- risk ---------------------------------------------------------------------------------

class FakeModel:
    def __init__(self, level=None, error=None):
        self.level, self.error = level, error

    def with_structured_output(self, schema):
        return self

    def invoke(self, prompt):
        if self.error:
            raise self.error
        return risk.RiskAssessment(level=self.level, reasons=["model reason"])


def test_rules_force_high_for_large_orders_even_if_model_says_low():
    out = risk.assess(order(amount=9000), [("m", FakeModel("low"))])
    assert out["level"] == "high" and out["source"] == "model:m"
    assert any("order value" in r for r in out["reasons"])


def test_model_can_raise_but_not_lower_risk():
    assert risk.assess(order(amount=50), [("m", FakeModel("high"))])["level"] == "high"
    assert risk.assess(order(amount=1500), [("m", FakeModel("low"))])["level"] == "medium"


def test_falls_back_to_next_model_then_to_rules():
    chain = [("a", FakeModel(error=RuntimeError("throttled"))), ("b", FakeModel("low"))]
    assert risk.assess(order(), chain)["source"] == "model:b"
    down = [("a", FakeModel(error=RuntimeError("x"))), ("b", FakeModel(error=RuntimeError("y")))]
    out = risk.assess(order(amount=7000), down)
    assert out["source"] == "rules-fallback" and out["level"] == "high"


def test_assess_risk_handler_uses_injected_models(monkeypatch):
    monkeypatch.setattr(handlers, "_models", [("m", FakeModel("medium"))])
    assert handlers.assess_risk(order())["level"] == "medium"
