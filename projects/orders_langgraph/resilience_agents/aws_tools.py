"""AWS tool implementations for the deployed order-saga stack. Read-only except `start_saga`."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

from .scenarios import CATALOG, build_order

HEALTH_FIXTURE = Path(__file__).resolve().parents[1] / "data" / "health_events.json"
STACK_NAME = "OrdersStepFunctions"


@dataclass
class Finding:
    id: str
    resource: str
    severity: str            # low | medium | high
    detail: str
    remediation: str


class AwsTools:
    """Thin, testable wrapper. Pass a boto3 Session (or a stub exposing `.client(name)`)."""

    def __init__(self, session: Any, state_machine_arn: str, stack_name: str = STACK_NAME) -> None:
        self.s = session
        self.state_machine_arn = state_machine_arn
        self.stack_name = stack_name

    # -- read ---------------------------------------------------------------------------
    def describe_stack(self) -> list[dict]:
        r = self.s.client("cloudformation").describe_stack_resources(StackName=self.stack_name)
        return [{"type": x["ResourceType"], "logicalId": x["LogicalResourceId"],
                 "physicalId": x.get("PhysicalResourceId", "")} for x in r["StackResources"]]

    def get_alarm_states(self) -> list[dict]:
        r = self.s.client("cloudwatch").describe_alarms(AlarmNamePrefix=self.stack_name)
        return [{"alarm": a["AlarmName"], "state": a["StateValue"], "metric": a.get("MetricName"),
                 "hasActions": bool(a.get("AlarmActions"))} for a in r["MetricAlarms"]]

    def get_health_events(self) -> dict:
        """Live AWS Health when the account has Business support; otherwise a simulated feed."""
        try:
            r = self.s.client("health", region_name="us-east-1").describe_events(
                filter={"eventStatusCodes": ["open", "upcoming"]})
            return {"source": "aws-health", "events": r.get("events", [])}
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in {"SubscriptionRequiredException", "AccessDeniedException"}:
                raise
            return {"source": "simulated", "events": json.loads(HEALTH_FIXTURE.read_text(encoding="utf-8"))}

    # -- write (needs approval via the registry) -----------------------------------------
    def start_saga(self, scenario: str) -> dict:
        if scenario not in CATALOG:
            raise ValueError(f"unknown scenario {scenario!r}")
        order = build_order(CATALOG[scenario])
        arn = self.s.client("stepfunctions").start_execution(
            stateMachineArn=self.state_machine_arn, name=order["orderId"], input=json.dumps(order))["executionArn"]
        return {"executionArn": arn, "orderId": order["orderId"]}

    def wait_saga(self, execution_arn: str, timeout: int = 120) -> dict:
        sfn = self.s.client("stepfunctions")
        started, deadline = time.time(), time.time() + timeout
        while time.time() < deadline:
            d = sfn.describe_execution(executionArn=execution_arn)
            if d["status"] != "RUNNING":
                events = sfn.get_execution_history(executionArn=execution_arn, maxResults=1000)["events"]
                states = [e["stateEnteredEventDetails"]["name"] for e in events if "stateEnteredEventDetails" in e]
                failed = next((e["executionFailedEventDetails"].get("error") for e in events
                               if "executionFailedEventDetails" in e), None)
                out = json.loads(d["output"]) if d.get("output") else {}
                return {"status": d["status"], "outcome": out.get("outcome", {}).get("status"),
                        "error": d.get("error") or failed, "states": states,
                        "riskSource": out.get("risk", {}).get("source"), "seconds": round(time.time() - started, 1)}
            time.sleep(1)
        return {"status": "TIMED_OUT_WAITING", "states": [], "seconds": timeout}

    # -- configuration policy checks (read-only) ---------------------------------------------
    def check_policies(self) -> list[dict]:
        resources = self.describe_stack()
        by_type: dict[str, list[dict]] = {}
        for r in resources:
            by_type.setdefault(r["type"], []).append(r)
        findings: list[Finding] = []
        for check in (self._lambda_checks, self._dynamodb_checks, self._sns_checks, self._alarm_checks):
            try:
                findings.extend(check(by_type))
            except ClientError as exc:
                findings.append(Finding(f"CHECK_SKIPPED_{check.__name__}", "-", "low",
                                        f"check could not run: {exc.response['Error']['Code']}", "grant read access"))
        return [asdict(f) for f in findings]

    def _lambda_checks(self, by_type) -> list[Finding]:
        lam = self.s.client("lambda")
        no_tracing, no_reserved = [], []
        for r in by_type.get("AWS::Lambda::Function", []):
            name = r["physicalId"]
            cfg = lam.get_function_configuration(FunctionName=name)
            if cfg.get("TracingConfig", {}).get("Mode") != "Active":
                no_tracing.append(r["logicalId"])
            if "ReservedConcurrentExecutions" not in lam.get_function_concurrency(FunctionName=name):
                no_reserved.append(r["logicalId"])
        out = []
        if no_tracing:
            out.append(Finding("LAMBDA_TRACING_DISABLED", ", ".join(no_tracing), "low",
                               "X-Ray active tracing is off, so cross-service latency is hard to attribute.",
                               "Set tracing=lambda_.Tracing.ACTIVE on each function."))
        if no_reserved:
            out.append(Finding("LAMBDA_NO_RESERVED_CONCURRENCY", ", ".join(no_reserved), "low",
                               "No reserved concurrency: a traffic spike on one function can starve others.",
                               "Set reserved_concurrent_executions on the high-traffic functions."))
        return out

    def _dynamodb_checks(self, by_type) -> list[Finding]:
        ddb, out = self.s.client("dynamodb"), []
        for r in by_type.get("AWS::DynamoDB::Table", []):
            pitr = ddb.describe_continuous_backups(TableName=r["physicalId"])["ContinuousBackupsDescription"][
                "PointInTimeRecoveryDescription"]["PointInTimeRecoveryStatus"]
            if pitr != "ENABLED":
                out.append(Finding("DDB_NO_POINT_IN_TIME_RECOVERY", r["logicalId"], "medium",
                                   "Point-in-time recovery is off: a bad write cannot be rolled back.",
                                   "Pass point_in_time_recovery_specification to the Table construct."))
        return out

    def _sns_checks(self, by_type) -> list[Finding]:
        sns, out = self.s.client("sns"), []
        for r in by_type.get("AWS::SNS::Topic", []):
            attrs = sns.get_topic_attributes(TopicArn=r["physicalId"])["Attributes"]
            if not attrs.get("KmsMasterKeyId"):
                out.append(Finding("SNS_UNENCRYPTED", r["logicalId"], "low",
                                   "Topic is not encrypted with a KMS key.",
                                   "Pass master_key to the Topic (and grant the publishing roles)."))
        return out

    def _alarm_checks(self, by_type) -> list[Finding]:
        alarms = self.get_alarm_states()
        out = [Finding("ALARM_WITHOUT_ACTION", a["alarm"], "medium", "Alarm has no notification action.",
                       "Add an SNS action.") for a in alarms if not a["hasActions"]]
        metrics = {a["metric"] for a in alarms}
        missing = [m for m in ("ExecutionsFailed", "ExecutionsTimedOut") if m not in metrics]
        if missing:
            out.append(Finding("STATE_MACHINE_ALARM_GAP", "OrderSaga", "medium",
                               f"No alarm on {', '.join(missing)}: stuck or timed-out orders go unnoticed.",
                               "Add cloudwatch.Alarm for each metric with the alert topic as action."))
        return out
