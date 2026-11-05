"""Run fault-injection scenarios against the deployed order-saga state machine.

Reads the stack outputs written by `cdk deploy --outputs-file`. Results written with --json
contain only statuses and state names (no account ids or ARNs).

Usage: python scripts/run_saga_scenarios.py --profile udacity-lab --outputs deploy-outputs.local.json
"""
from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path

import boto3

BASE = {"customerId": "c-1", "items": [{"sku": "P001", "qty": 1}]}

SCENARIOS = {
    "happy_path": {"amount": 120.0},
    "payment_flaky_recovers": {"amount": 120.0, "faults": {"ChargePayment": "transient"}},
    "shipping_broken_compensates": {"amount": 120.0, "faults": {"CreateShipment": "permanent"}},
    "refund_broken_escalates": {"amount": 120.0, "faults": {"CreateShipment": "permanent", "RefundPayment": "permanent"}},
    "high_value_needs_review": {"amount": 9000.0},
}


def wait(sfn, arn: str, sqs=None, queue_url: str | None = None, timeout: int = 150) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = sfn.describe_execution(executionArn=arn)
        if d["status"] != "RUNNING":
            return d
        if sqs and queue_url:  # act as the human reviewer: approve anything waiting
            for m in sqs.receive_message(QueueUrl=queue_url, WaitTimeSeconds=2, MaxNumberOfMessages=5).get("Messages", []):
                body = json.loads(m["Body"])
                sfn.send_task_success(taskToken=body["taskToken"], output=json.dumps({"approved": True}))
                sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=m["ReceiptHandle"])
                print(f"    reviewer approved order {body['orderId']} ({', '.join(body['reasons'])})")
        time.sleep(1)
    raise TimeoutError(arn)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--outputs", required=True)
    ap.add_argument("--json")
    ap.add_argument("--only", nargs="*", help="scenario names to run")
    a = ap.parse_args()

    out = next(iter(json.loads(Path(a.outputs).read_text(encoding="utf-8")).values()))
    s = boto3.Session(profile_name=a.profile, region_name="us-east-1")
    sfn, sqs = s.client("stepfunctions"), s.client("sqs")
    results = {}
    for name, extra in SCENARIOS.items():
        if a.only and name not in a.only:
            continue
        order = {"orderId": f"{name[:12]}-{uuid.uuid4().hex[:6]}", **BASE, **extra}
        started = time.time()
        arn = sfn.start_execution(stateMachineArn=out["StateMachineArn"], name=order["orderId"],
                                  input=json.dumps(order))["executionArn"]
        d = wait(sfn, arn, sqs, out["ReviewQueueUrl"])
        events = sfn.get_execution_history(executionArn=arn, maxResults=1000)["events"]
        states = [e["stateEnteredEventDetails"]["name"] for e in events if "stateEnteredEventDetails" in e]
        output = json.loads(d["output"]) if d.get("output") else {}
        failed = next((e["executionFailedEventDetails"].get("error") for e in events if "executionFailedEventDetails" in e), None)
        results[name] = {
            "status": d["status"], "outcome": output.get("outcome", {}).get("status"), "error": d.get("error") or failed,
            "risk": output.get("risk", {}), "seconds": round(time.time() - started, 1), "states": states,
        }
        r = results[name]
        print(f"{name:30} {r['status']:10} outcome={r['outcome']} error={r['error']} risk={r['risk'].get('level')}/{r['risk'].get('source')} {r['seconds']}s")
        print("    " + " > ".join(states))
    if a.json:
        Path(a.json).write_text(json.dumps(results, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
