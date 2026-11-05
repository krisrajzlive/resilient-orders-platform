"""Run the order-saga ASL in Step Functions Local with mocked service integrations.

Free and fast: nothing touches AWS. Mock responses are keyed by state name and invocation
number, so retries, catches and compensation paths can be driven deterministically.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parents[1]
ASL = ROOT / "workflows" / "order_saga.asl.json"
IMAGE = "amazon/aws-stepfunctions-local"
PORT = 8083
CONTAINER = "orders-sfn-local-tests"
MOCK_PATH_IN_CONTAINER = "/home/StepFunctionsLocal/MockConfigFile.json"

SUBSTITUTIONS = {
    "AssessRiskArn": "arn:aws:lambda:us-east-1:123456789012:function:AssessRisk",
    "ReserveStockArn": "arn:aws:lambda:us-east-1:123456789012:function:ReserveStock",
    "ChargePaymentArn": "arn:aws:lambda:us-east-1:123456789012:function:ChargePayment",
    "CreateShipmentArn": "arn:aws:lambda:us-east-1:123456789012:function:CreateShipment",
    "RefundPaymentArn": "arn:aws:lambda:us-east-1:123456789012:function:RefundPayment",
    "ReleaseStockArn": "arn:aws:lambda:us-east-1:123456789012:function:ReleaseStock",
    "OrderEventsTopicArn": "arn:aws:sns:us-east-1:123456789012:order-events",
    "AlertTopicArn": "arn:aws:sns:us-east-1:123456789012:order-alerts",
    "ReviewQueueUrl": "https://sqs.us-east-1.amazonaws.com/123456789012/order-review",
}


def definition(fast: bool = True) -> str:
    text = ASL.read_text(encoding="utf-8")
    for k, v in SUBSTITUTIONS.items():
        text = text.replace("${" + k + "}", v)
    spec = json.loads(text)
    if fast:  # keep retry semantics (counts, ordering) but not the real waiting time
        for state in spec["States"].values():
            for r in state.get("Retry", []):
                r["IntervalSeconds"], r["BackoffRate"] = 1, 1
    return json.dumps(spec)


def docker_ready() -> bool:
    try:
        if subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode != 0:
            return False
        return bool(subprocess.run(["docker", "images", "-q", IMAGE], capture_output=True, text=True).stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


def start_container(mock_config: Path) -> None:
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    subprocess.run(
        ["docker", "run", "-d", "--name", CONTAINER, "-p", f"127.0.0.1:{PORT}:8083",
         "-v", f"{mock_config}:{MOCK_PATH_IN_CONTAINER}:ro",
         "-e", f"SFN_MOCK_CONFIG={MOCK_PATH_IN_CONTAINER}", IMAGE],
        check=True, capture_output=True)


def stop_container() -> None:
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)


def client():
    # Step Functions Local ignores credentials but boto3 insists on some: placeholders via the environment.
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "local")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "local")
    return boto3.client(
        "stepfunctions", endpoint_url=f"http://127.0.0.1:{PORT}", region_name="us-east-1",
        config=Config(retries={"max_attempts": 1}))


def wait_until_up(sfn, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            sfn.list_state_machines()
            return
        except Exception:
            time.sleep(1)
    raise RuntimeError("Step Functions Local did not start")


def create_machine(sfn, name: str = "OrderSaga") -> str:
    return sfn.create_state_machine(
        name=name, definition=definition(), roleArn="arn:aws:iam::123456789012:role/DummyRole")["stateMachineArn"]


class Result:
    def __init__(self, status: str, output: dict | None, error: str | None, history: list[dict]):
        self.status, self.output, self.error, self.history = status, output, error, history
        self.visited = [
            e["stateEnteredEventDetails"]["name"] for e in history if "stateEnteredEventDetails" in e]

    @property
    def outcome(self) -> str | None:
        return (self.output or {}).get("outcome", {}).get("status")

    def entered(self, state: str) -> int:
        return self.visited.count(state)

    def task_failures(self, state: str) -> int:
        """Number of failed attempts of a Task state (counts retries)."""
        count, current = 0, None
        for e in self.history:
            if "stateEnteredEventDetails" in e:
                current = e["stateEnteredEventDetails"]["name"]
            if e["type"] in ("TaskFailed", "LambdaFunctionFailed") and current == state:
                count += 1
        return count


def run_case(sfn, machine_arn: str, case: str, payload: dict | None = None, timeout: float = 60) -> Result:
    payload = payload or {"orderId": f"o-{uuid.uuid4().hex[:6]}", "customerId": "c-1", "amount": 120.0,
                          "items": [{"sku": "P001", "qty": 1}]}
    execution = sfn.start_execution(
        stateMachineArn=f"{machine_arn}#{case}", name=f"{case}-{uuid.uuid4().hex[:6]}",
        input=json.dumps(payload))["executionArn"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = sfn.describe_execution(executionArn=execution)
        if d["status"] != "RUNNING":
            history = sfn.get_execution_history(executionArn=execution, maxResults=500)["events"]
            # Local does not always populate `error` on describe_execution; the history has it.
            failed = next((e["executionFailedEventDetails"] for e in history if "executionFailedEventDetails" in e), {})
            return Result(d["status"], json.loads(d["output"]) if d.get("output") else None,
                          d.get("error") or failed.get("error"), history)
        time.sleep(0.5)
    raise TimeoutError(f"{case} still running after {timeout}s")
