"""Run and watch the deployed Step Functions order saga from the console.

Local development API: it uses the caller's AWS profile, starts at most MAX_STARTS_PER_HOUR
executions per process (each costs a fraction of a cent plus one model call), and only accepts
the fault-injection shape the workflow understands.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import Field

from .common import Camel

STEPS = ("ReserveStock", "ChargePayment", "CreateShipment", "RefundPayment", "ReleaseStock")
MAX_STARTS_PER_HOUR = 30
NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

Step = Literal["ReserveStock", "ChargePayment", "CreateShipment", "RefundPayment", "ReleaseStock"]
Fault = Literal["transient", "permanent"]


class RateLimited(Exception):
    pass


class StartIn(Camel):
    amount: float = Field(120.0, gt=0, le=50_000)
    faults: dict[Step, Fault] = Field(default_factory=dict)


class StartOut(Camel):
    name: str


class ReviewIn(Camel):
    approved: bool


class ReviewOut(Camel):
    delivered: bool


class RiskOut(Camel):
    level: str | None = None
    reasons: list[str] = Field(default_factory=list)
    source: str | None = None


class StatusOut(Camel):
    name: str
    status: str                       # RUNNING | SUCCEEDED | FAILED | TIMED_OUT | ABORTED
    finished: bool
    outcome: str | None = None        # CONFIRMED | REJECTED
    error: str | None = None
    states: list[str]
    current: str | None = None
    retries: dict[str, int]
    risk: RiskOut | None = None
    waiting_review: bool
    seconds: float


class ExecutionRow(Camel):
    name: str
    status: str


def _parse(value: str | None) -> Any:
    try:
        return json.loads(value) if value else None
    except json.JSONDecodeError:
        return None


def summarize(name: str, description: dict, events: list[dict]) -> dict:
    """Reduce an execution description and its history to what the console draws."""
    states: list[str] = []
    retries: dict[str, int] = {}
    current: str | None = None
    risk: dict | None = None
    for e in events:
        entered = e.get("stateEnteredEventDetails")
        if entered:
            current = entered["name"]
            states.append(current)
        if e["type"] in ("TaskFailed", "LambdaFunctionFailed") and current:
            retries[current] = retries.get(current, 0) + 1      # failed attempts of the current state
        if e["type"] == "TaskSucceeded" and current == "AssessRisk":
            payload = (_parse(e.get("taskSucceededEventDetails", {}).get("output")) or {}).get("Payload")
            if isinstance(payload, dict):
                risk = payload
    status = description["status"]
    output = _parse(description.get("output")) or {}
    failed = next((e["executionFailedEventDetails"].get("error") for e in events
                   if "executionFailedEventDetails" in e), None)
    if output.get("risk"):
        risk = output["risk"]
    start = description["startDate"]
    stop = description.get("stopDate") or datetime.now(timezone.utc)
    return {
        "name": name, "status": status, "finished": status != "RUNNING",
        "outcome": output.get("outcome", {}).get("status"),
        "error": description.get("error") or failed,
        "states": states, "current": current, "retries": retries,
        "risk": {"level": risk.get("level"), "reasons": risk.get("reasons", []), "source": risk.get("source")} if risk else None,
        "waiting_review": status == "RUNNING" and current == "HumanReview",
        "seconds": round((stop - start).total_seconds(), 1),
    }


class SagaGateway:
    """Talks to Step Functions and the review queue. Pass a boto3 Session or any object with `.client()`."""

    def __init__(self, session: Any, state_machine_arn: str, review_queue_url: str,
                 max_starts_per_hour: int = MAX_STARTS_PER_HOUR) -> None:
        self.sfn = session.client("stepfunctions")
        self.sqs = session.client("sqs")
        self.state_machine_arn = state_machine_arn
        self.queue_url = review_queue_url
        self._starts: deque[float] = deque()
        self._limit = max_starts_per_hour

    def _execution_arn(self, name: str) -> str:
        return self.state_machine_arn.replace(":stateMachine:", ":execution:") + f":{name}"

    def start(self, amount: float, faults: dict[str, str]) -> str:
        now = time.time()
        while self._starts and now - self._starts[0] > 3600:
            self._starts.popleft()
        if len(self._starts) >= self._limit:
            raise RateLimited(f"at most {self._limit} executions per hour from this console")
        name = f"ui-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        order = {"orderId": name, "customerId": "c-console", "amount": amount,
                 "items": [{"sku": "P001", "qty": 1}], **({"faults": faults} if faults else {})}
        self.sfn.start_execution(stateMachineArn=self.state_machine_arn, name=name, input=json.dumps(order))
        self._starts.append(now)
        return name

    def get(self, name: str) -> dict:
        arn = self._execution_arn(name)
        description = self.sfn.describe_execution(executionArn=arn)
        events = self.sfn.get_execution_history(executionArn=arn, maxResults=1000)["events"]
        return summarize(name, description, events)

    def list_recent(self, limit: int = 10) -> list[dict]:
        rows = self.sfn.list_executions(stateMachineArn=self.state_machine_arn, maxResults=limit)["executions"]
        return [{"name": r["name"], "status": r["status"]} for r in rows]

    def review(self, name: str, approved: bool) -> bool:
        """Deliver the reviewer's decision for one order. Other orders' messages go back to the queue."""
        deadline = time.time() + 8
        while time.time() < deadline:
            messages = self.sqs.receive_message(
                QueueUrl=self.queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=2).get("Messages", [])
            for m in messages:
                body = _parse(m["Body"]) or {}
                if body.get("orderId") == name and body.get("taskToken"):
                    self.sfn.send_task_success(taskToken=body["taskToken"],
                                               output=json.dumps({"approved": approved}))
                    self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=m["ReceiptHandle"])
                    return True
                self.sqs.change_message_visibility(
                    QueueUrl=self.queue_url, ReceiptHandle=m["ReceiptHandle"], VisibilityTimeout=0)
        return False


def register(app: FastAPI, saga_or_none) -> None:
    """Add the saga routes. `saga_or_none` is a zero-argument callable returning a gateway or None."""

    def gateway() -> SagaGateway:
        g = saga_or_none()
        if g is None:
            raise HTTPException(503, "AWS workflow is not connected (deploy the stack and set SAGA_OUTPUTS)")
        return g

    def checked(name: str) -> str:
        if not NAME_PATTERN.match(name):
            raise HTTPException(422, "invalid execution name")
        return name

    @app.post("/api/saga/executions", response_model=StartOut, response_model_by_alias=True)
    def start(body: StartIn):
        try:
            return StartOut(name=gateway().start(body.amount, dict(body.faults)))
        except RateLimited as exc:
            raise HTTPException(429, str(exc)) from exc

    @app.get("/api/saga/executions", response_model=list[ExecutionRow], response_model_by_alias=True)
    def recent(limit: int = 10):
        return [ExecutionRow(**r) for r in gateway().list_recent(max(1, min(limit, 25)))]

    @app.get("/api/saga/executions/{name}", response_model=StatusOut, response_model_by_alias=True)
    def status(name: str):
        try:
            return StatusOut(**gateway().get(checked(name)))
        except HTTPException:
            raise
        except Exception as exc:
            if type(exc).__name__ == "ExecutionDoesNotExist":
                raise HTTPException(404, "execution not found") from exc
            raise

    @app.post("/api/saga/executions/{name}/review", response_model=ReviewOut, response_model_by_alias=True)
    def review(name: str, body: ReviewIn):
        delivered = gateway().review(checked(name), body.approved)
        if not delivered:
            raise HTTPException(404, "no review is waiting for this order")
        return ReviewOut(delivered=True)
