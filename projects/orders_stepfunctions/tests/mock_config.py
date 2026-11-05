"""Mock responses for Step Functions Local, keyed by state name and invocation number.

Invocation keys: "0" is the first call of that state, "0-1" the first two, "2" the third.
"""
from __future__ import annotations

import json
from pathlib import Path


def ret(payload: dict) -> dict:
    return {"Return": {"StatusCode": 200, "Payload": payload}}


def throw(error: str) -> dict:
    return {"Throw": {"Error": error, "Cause": f"mocked {error}"}}


RISK_LOW = {"0": ret({"level": "low", "reasons": ["within normal limits"], "source": "model:test"})}
RISK_HIGH = {"0": ret({"level": "high", "reasons": ["order value 9000.00 at or above 5000"], "source": "model:test"})}
STEP_OK = {"0": ret({"id": "x-1", "replayed": False})}
UNDO_OK = {"0": ret({"undone": "x", "attempt": 1})}
SNS_OK = {"0": {"Return": {"MessageId": "m-1"}}}
PERMANENT = {"0": throw("PermanentError")}
TRANSIENT_FOREVER = {"0-3": throw("TransientError")}          # first call + 3 retries
TRANSIENT_THEN_OK = {"0-1": throw("TransientError"), "2": ret({"id": "x-1", "replayed": False})}

RESPONSES = {
    "RiskLow": RISK_LOW, "RiskHigh": RISK_HIGH, "RiskDown": {"0-3": throw("Lambda.ServiceException")},
    "StepOk": STEP_OK, "UndoOk": UNDO_OK, "SnsOk": SNS_OK, "Permanent": PERMANENT,
    "TransientForever": TRANSIENT_FOREVER, "TransientThenOk": TRANSIENT_THEN_OK,
    "ReviewApproved": {"0": {"Return": {"approved": True}}},
    "ReviewDenied": {"0": {"Return": {"approved": False}}},
    "ReviewTimeout": {"0": {"Throw": {"Error": "States.Timeout", "Cause": "no reviewer responded"}}},
}

HAPPY = {"AssessRisk": "RiskLow", "ReserveStock": "StepOk", "ChargePayment": "StepOk",
         "CreateShipment": "StepOk", "NotifyConfirmed": "SnsOk"}

TEST_CASES = {
    "HappyPath": HAPPY,
    "ChargeTransientRecovers": {**HAPPY, "ChargePayment": "TransientThenOk"},
    "ChargeRetriesExhausted": {**HAPPY, "ChargePayment": "TransientForever", "ReleaseStock": "UndoOk", "NotifyRejected": "SnsOk"},
    "ChargePermanentFailure": {**HAPPY, "ChargePayment": "Permanent", "ReleaseStock": "UndoOk", "NotifyRejected": "SnsOk"},
    "ShipmentPermanentFailure": {**HAPPY, "CreateShipment": "Permanent", "RefundPayment": "UndoOk",
                                 "ReleaseStock": "UndoOk", "NotifyRejected": "SnsOk"},
    "ReserveStockFails": {**HAPPY, "ReserveStock": "Permanent", "NotifyRejected": "SnsOk"},
    "RefundFails": {**HAPPY, "CreateShipment": "Permanent", "RefundPayment": "Permanent", "ManualReview": "SnsOk"},
    "ReleaseFailsAfterPaymentFailure": {**HAPPY, "ChargePayment": "Permanent", "ReleaseStock": "Permanent", "ManualReview": "SnsOk"},
    "HighRiskApproved": {**HAPPY, "AssessRisk": "RiskHigh", "HumanReview": "ReviewApproved"},
    "HighRiskDenied": {**HAPPY, "AssessRisk": "RiskHigh", "HumanReview": "ReviewDenied", "NotifyRejected": "SnsOk"},
    "HighRiskReviewTimeout": {**HAPPY, "AssessRisk": "RiskHigh", "HumanReview": "ReviewTimeout", "NotifyRejected": "SnsOk"},
    "RiskServiceDown": {**HAPPY, "AssessRisk": "RiskDown"},
}


def write(path: Path) -> Path:
    config = {
        "StateMachines": {"OrderSaga": {"TestCases": TEST_CASES}},
        "MockedResponses": RESPONSES,
    }
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path
