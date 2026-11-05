"""Behavioural tests of the order-saga workflow in Step Functions Local (mocked integrations)."""
from __future__ import annotations

import pytest

import mock_config
import sfn_harness as h

pytestmark = pytest.mark.sfn_local

if not h.docker_ready():
    pytest.skip("Docker or the amazon/aws-stepfunctions-local image is not available", allow_module_level=True)


@pytest.fixture(scope="module")
def sfn(tmp_path_factory):
    cfg = mock_config.write(tmp_path_factory.mktemp("sfn") / "MockConfigFile.json")
    h.start_container(cfg)
    client = h.client()
    try:
        h.wait_until_up(client)
        yield client
    finally:
        h.stop_container()


@pytest.fixture(scope="module")
def machine(sfn):
    return h.create_machine(sfn)


def run(sfn, machine, case):
    return h.run_case(sfn, machine, case)


def test_happy_path_confirms_without_compensation(sfn, machine):
    r = run(sfn, machine, "HappyPath")
    assert (r.status, r.outcome) == ("SUCCEEDED", "CONFIRMED")
    assert r.visited == ["AssessRisk", "RiskChoice", "ReserveStock", "ChargePayment", "CreateShipment",
                         "MarkConfirmed", "NotifyConfirmed", "OrderConfirmed"]


def test_transient_failure_is_retried_then_confirms(sfn, machine):
    r = run(sfn, machine, "ChargeTransientRecovers")
    assert (r.status, r.outcome) == ("SUCCEEDED", "CONFIRMED")
    assert r.task_failures("ChargePayment") == 2
    assert r.entered("ReleaseStock") == 0


def test_exhausted_retries_release_stock_and_reject(sfn, machine):
    r = run(sfn, machine, "ChargeRetriesExhausted")
    assert (r.status, r.outcome) == ("SUCCEEDED", "REJECTED")
    assert r.task_failures("ChargePayment") == 4            # first call + 3 retries
    assert r.entered("ReleaseStock") == 1 and r.entered("RefundPayment") == 0


def test_permanent_payment_failure_is_not_retried(sfn, machine):
    r = run(sfn, machine, "ChargePermanentFailure")
    assert (r.status, r.outcome) == ("SUCCEEDED", "REJECTED")
    assert r.task_failures("ChargePayment") == 1
    assert r.entered("ReleaseStock") == 1 and r.entered("RefundPayment") == 0


def test_shipping_failure_refunds_then_releases_in_reverse_order(sfn, machine):
    r = run(sfn, machine, "ShipmentPermanentFailure")
    assert (r.status, r.outcome) == ("SUCCEEDED", "REJECTED")
    order = [s for s in r.visited if s in ("ReserveStock", "ChargePayment", "CreateShipment", "RefundPayment", "ReleaseStock")]
    assert order == ["ReserveStock", "ChargePayment", "CreateShipment", "RefundPayment", "ReleaseStock"]


def test_first_step_failure_needs_no_compensation(sfn, machine):
    r = run(sfn, machine, "ReserveStockFails")
    assert (r.status, r.outcome) == ("SUCCEEDED", "REJECTED")
    assert r.entered("RefundPayment") == 0 and r.entered("ReleaseStock") == 0


def test_refund_failure_escalates_to_manual_review(sfn, machine):
    r = run(sfn, machine, "RefundFails")
    assert r.status == "FAILED" and r.error == "CompensationFailed"
    assert r.entered("ManualReview") == 1 and r.entered("ReleaseStock") == 0


def test_release_failure_escalates_to_manual_review(sfn, machine):
    r = run(sfn, machine, "ReleaseFailsAfterPaymentFailure")
    assert r.status == "FAILED" and r.error == "CompensationFailed"
    assert r.entered("ManualReview") == 1


def test_high_risk_order_waits_for_review_and_proceeds_when_approved(sfn, machine):
    r = run(sfn, machine, "HighRiskApproved")
    assert (r.status, r.outcome) == ("SUCCEEDED", "CONFIRMED")
    assert r.visited[:4] == ["AssessRisk", "RiskChoice", "HumanReview", "ReviewDecision"]


def test_high_risk_order_denied_is_rejected_without_side_effects(sfn, machine):
    r = run(sfn, machine, "HighRiskDenied")
    assert (r.status, r.outcome) == ("SUCCEEDED", "REJECTED")
    assert r.entered("ReserveStock") == 0


def test_review_timeout_rejects_the_order(sfn, machine):
    r = run(sfn, machine, "HighRiskReviewTimeout")
    assert (r.status, r.outcome) == ("SUCCEEDED", "REJECTED")
    assert r.entered("ReserveStock") == 0


def test_risk_service_outage_degrades_to_medium_risk_and_continues(sfn, machine):
    r = run(sfn, machine, "RiskServiceDown")
    assert (r.status, r.outcome) == ("SUCCEEDED", "CONFIRMED")
    assert r.entered("RiskFallback") == 1 and r.output["risk"]["source"] == "fallback"
