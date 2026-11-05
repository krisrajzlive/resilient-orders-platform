"""Saga API tests with fake Step Functions and SQS clients."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from services.catalog_api import saga
from services.catalog_api.app import Deps, create_app

SM_ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:OrderSaga"
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def entered(kind: str, name: str) -> dict:
    return {"type": f"{kind}StateEntered", "stateEnteredEventDetails": {"name": name}}


def task_ok(output: dict) -> dict:
    return {"type": "TaskSucceeded", "taskSucceededEventDetails": {"output": json.dumps(output)}}


RISK_OK = task_ok({"Payload": {"level": "low", "reasons": ["within normal limits"], "source": "model:haiku"}})
TASK_FAILED = {"type": "TaskFailed"}


def description(status="SUCCEEDED", output=None, seconds=12, error=None):
    d = {"status": status, "startDate": T0, "stopDate": T0 + timedelta(seconds=seconds)}
    if output is not None:
        d["output"] = json.dumps(output)
    if error:
        d["error"] = error
    return d


# -- summarize ------------------------------------------------------------------------------

def test_flaky_payment_counts_retries_and_reads_risk_and_outcome():
    events = [entered("Task", "AssessRisk"), RISK_OK, entered("Choice", "RiskChoice"), entered("Task", "ReserveStock"),
              entered("Task", "ChargePayment"), TASK_FAILED, TASK_FAILED, entered("Task", "CreateShipment"),
              entered("Pass", "MarkConfirmed"), entered("Succeed", "OrderConfirmed")]
    s = saga.summarize("o1", description(output={"outcome": {"status": "CONFIRMED"}}), events)
    assert s["retries"] == {"ChargePayment": 2} and s["outcome"] == "CONFIRMED" and s["finished"] is True
    assert s["risk"]["source"] == "model:haiku" and s["states"][0] == "AssessRisk" and s["seconds"] == 12.0


def test_high_risk_order_is_waiting_for_review_while_running():
    events = [entered("Task", "AssessRisk"), RISK_OK, entered("Choice", "RiskChoice"), entered("Task", "HumanReview")]
    s = saga.summarize("o2", {"status": "RUNNING", "startDate": datetime.now(timezone.utc)}, events)
    assert s["waiting_review"] is True and s["finished"] is False and s["current"] == "HumanReview"


def test_failed_execution_surfaces_the_error_name():
    events = [entered("Task", "RefundPayment"), entered("Task", "ManualReview"), entered("Fail", "CompensationFailed"),
              {"type": "ExecutionFailed", "executionFailedEventDetails": {"error": "CompensationFailed"}}]
    s = saga.summarize("o3", description("FAILED"), events)
    assert s["error"] == "CompensationFailed" and s["waiting_review"] is False and s["outcome"] is None


# -- gateway ------------------------------------------------------------------------------------

class NoSuchExecution(Exception):
    pass


NoSuchExecution.__name__ = "ExecutionDoesNotExist"


class FakeSfn:
    def __init__(self):
        self.started, self.task_success = [], []
        self.desc, self.events = description(), []

    def start_execution(self, **kw):
        self.started.append(kw)
        return {"executionArn": "x"}

    def describe_execution(self, executionArn):
        if executionArn.endswith(":missing"):
            raise NoSuchExecution()
        self.last_arn = executionArn
        return self.desc

    def get_execution_history(self, executionArn, maxResults):
        return {"events": self.events}

    def list_executions(self, stateMachineArn, maxResults):
        return {"executions": [{"name": "a", "status": "SUCCEEDED"}, {"name": "b", "status": "RUNNING"}]}

    def send_task_success(self, taskToken, output):
        self.task_success.append((taskToken, json.loads(output)))


class FakeSqs:
    def __init__(self, bodies):
        self.messages = [{"Body": json.dumps(b), "ReceiptHandle": f"h{i}"} for i, b in enumerate(bodies)]
        self.deleted, self.released = [], []

    def receive_message(self, **kw):
        return {"Messages": list(self.messages)}

    def delete_message(self, QueueUrl, ReceiptHandle):
        self.deleted.append(ReceiptHandle)
        self.messages = [m for m in self.messages if m["ReceiptHandle"] != ReceiptHandle]

    def change_message_visibility(self, QueueUrl, ReceiptHandle, VisibilityTimeout):
        self.released.append((ReceiptHandle, VisibilityTimeout))


class FakeSession:
    def __init__(self, sfn, sqs):
        self._c = {"stepfunctions": sfn, "sqs": sqs}

    def client(self, name):
        return self._c[name]


def gateway(sqs=None, limit=30):
    sfn = FakeSfn()
    return saga.SagaGateway(FakeSession(sfn, sqs or FakeSqs([])), SM_ARN, "https://queue", limit), sfn


def test_start_builds_an_order_with_faults_and_a_unique_name():
    g, sfn = gateway()
    name = g.start(250.0, {"ChargePayment": "transient"})
    sent = sfn.started[0]
    order = json.loads(sent["input"])
    assert sent["name"] == name == order["orderId"] and name.startswith("ui-")
    assert order["amount"] == 250.0 and order["faults"] == {"ChargePayment": "transient"}
    g2, sfn2 = gateway()
    g2.start(120.0, {})
    assert "faults" not in json.loads(sfn2.started[0]["input"])


def test_execution_arn_is_derived_from_the_state_machine():
    g, sfn = gateway()
    g.get("ui-1")
    assert sfn.last_arn == "arn:aws:states:us-east-1:123456789012:execution:OrderSaga:ui-1"


def test_start_is_rate_limited():
    g, _ = gateway(limit=2)
    g.start(1, {}), g.start(1, {})
    with pytest.raises(saga.RateLimited):
        g.start(1, {})


def test_review_delivers_to_the_matching_order_and_returns_others_to_the_queue():
    sqs = FakeSqs([{"orderId": "other", "taskToken": "tok-other"}, {"orderId": "ui-9", "taskToken": "tok-9"}])
    g, sfn = gateway(sqs)
    assert g.review("ui-9", True) is True
    assert sfn.task_success == [("tok-9", {"approved": True})]
    assert sqs.deleted == ["h1"] and sqs.released == [("h0", 0)]


def test_review_returns_false_when_nothing_is_waiting(monkeypatch):
    monkeypatch.setattr(saga.time, "time", iter([0, 0, 100, 100, 100]).__next__)      # skip the retry window
    g, sfn = gateway(FakeSqs([{"orderId": "other", "taskToken": "t"}]))
    assert g.review("ui-9", False) is False and sfn.task_success == []


# -- routes -----------------------------------------------------------------------------------------

class Stub(Deps):
    saga = None                                                    # type: ignore[assignment]
    store = type("S", (), {"count": lambda self: 32})()            # type: ignore[assignment]


def client(gw):
    deps = Stub(settings=None)
    deps.saga = gw
    return TestClient(create_app(deps))


def test_routes_return_503_when_the_workflow_is_not_connected():
    c = client(None)
    assert c.post("/api/saga/executions", json={}).status_code == 503
    assert c.get("/api/health").json()["sagaConnected"] is False


def test_start_validates_input():
    g, _ = gateway()
    c = client(g)
    assert c.post("/api/saga/executions", json={"amount": 0}).status_code == 422
    assert c.post("/api/saga/executions", json={"amount": 99999}).status_code == 422
    assert c.post("/api/saga/executions", json={"faults": {"DeleteEverything": "permanent"}}).status_code == 422
    assert c.post("/api/saga/executions", json={"faults": {"ChargePayment": "explode"}}).status_code == 422
    ok = c.post("/api/saga/executions", json={"amount": 9000, "faults": {"CreateShipment": "permanent"}})
    assert ok.status_code == 200 and ok.json()["name"].startswith("ui-")


def test_status_route_uses_camel_case_and_validates_the_name():
    g, sfn = gateway()
    sfn.events = [entered("Task", "ChargePayment"), TASK_FAILED]
    c = client(g)
    body = c.get("/api/saga/executions/ui-1").json()
    assert body["waitingReview"] is False and body["retries"] == {"ChargePayment": 1}
    assert c.get("/api/saga/executions/bad name!").status_code in (404, 422)
    assert c.get("/api/saga/executions/missing").status_code == 404


def test_rate_limit_surfaces_as_429_and_review_404_when_nothing_waits(monkeypatch):
    g, _ = gateway(limit=1)
    c = client(g)
    assert c.post("/api/saga/executions", json={}).status_code == 200
    assert c.post("/api/saga/executions", json={}).status_code == 429
    monkeypatch.setattr(saga.time, "time", iter([0, 0, 100, 100, 100]).__next__)
    assert c.post("/api/saga/executions/ui-1/review", json={"approved": True}).status_code == 404


def test_recent_executions_list():
    assert client(gateway()[0]).get("/api/saga/executions").json() == [
        {"name": "a", "status": "SUCCEEDED"}, {"name": "b", "status": "RUNNING"}]
