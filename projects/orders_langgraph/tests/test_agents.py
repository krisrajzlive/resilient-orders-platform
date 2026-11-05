"""Agent behaviour tests with a fake model and fake AWS. No network, no cost."""
from __future__ import annotations

import sqlite3

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from resilience_agents import report
from resilience_agents.audit import AuditLog
from resilience_agents.graph import Deps, build_graph, ALLOWED
from resilience_agents.llm import (
    DriftReport, ExperimentPlan, LLMUnavailable, Narrative, Proposal, Route, StructuredLLM, TriageReport,
)
from resilience_agents.runner import run
from resilience_agents.scenarios import CATALOG, evaluate
from resilience_agents.tools import Tool, ToolDenied, ToolRegistry

APPROVE = lambda payload: {"approved": True, "note": "ok"}      # noqa: E731
DENY = lambda payload: {"approved": False, "note": "no"}        # noqa: E731

GOOD = {  # what a healthy saga returns per scenario
    "happy_path": dict(status="SUCCEEDED", outcome="CONFIRMED", states=["ReserveStock", "ChargePayment", "CreateShipment"]),
    "payment_flaky": dict(status="SUCCEEDED", outcome="CONFIRMED", states=["ReserveStock", "ChargePayment", "CreateShipment"]),
    "payment_down": dict(status="SUCCEEDED", outcome="REJECTED", states=["ReserveStock", "ChargePayment", "ReleaseStock"]),
    "shipping_down": dict(status="SUCCEEDED", outcome="REJECTED",
                          states=["ReserveStock", "ChargePayment", "CreateShipment", "RefundPayment", "ReleaseStock"]),
}


class FakeAws:
    def __init__(self, broken: set[str] | None = None):
        self.started: list[str] = []
        self.broken = broken or set()
        self._by_arn: dict[str, str] = {}

    def describe_stack(self):
        return [{"type": "AWS::Lambda::Function", "logicalId": "ChargePayment", "physicalId": "fn"},
                {"type": "AWS::DynamoDB::Table", "logicalId": "OrderEffects", "physicalId": "t"}]

    def get_alarm_states(self):
        return [{"alarm": "a", "state": "OK", "metric": "ExecutionsFailed", "hasActions": True}]

    def get_health_events(self):
        return {"source": "simulated", "events": [
            {"service": "LAMBDA", "statusCode": "open", "description": "Elevated errors"},
            {"service": "RDS", "statusCode": "upcoming", "description": "Version end of support"}]}

    def check_policies(self):
        return [{"id": "DDB_NO_POINT_IN_TIME_RECOVERY", "resource": "OrderEffects", "severity": "medium",
                 "detail": "PITR off", "remediation": "enable PITR"},
                {"id": "SNS_UNENCRYPTED", "resource": "Alerts", "severity": "low", "detail": "no KMS", "remediation": "add key"}]

    def start_saga(self, scenario):
        self.started.append(scenario)
        arn = f"arn-{scenario}"
        self._by_arn[arn] = scenario
        return {"executionArn": arn, "orderId": f"o-{scenario}"}

    def wait_saga(self, execution_arn, timeout=120):
        name = self._by_arn[execution_arn]
        summary = dict(GOOD.get(name, GOOD["payment_down"]))
        if name in self.broken:
            summary = dict(status="SUCCEEDED", outcome="CONFIRMED", states=["ReserveStock", "ChargePayment", "CreateShipment"])
        return {**summary, "error": None, "seconds": 1.0}


class ScriptedLLM(StructuredLLM):
    def __init__(self, answers: dict | None = None, down: bool = False):
        super().__init__([])
        self.answers, self.down, self.prompts = answers or {}, down, []

    def invoke(self, schema, prompt):
        self.prompts.append(prompt)
        if self.down or schema not in self.answers:
            raise LLMUnavailable("scripted outage")
        return self.answers[schema], "scripted-model"


def deps(tmp_path, llm=None, aws=None):
    return Deps(aws=aws or FakeAws(), llm=llm, audit=AuditLog(tmp_path / "audit.jsonl"), report_dir=tmp_path / "reports")


# -- routing ------------------------------------------------------------------------------

@pytest.mark.parametrize("request_text,expected", [
    ("inject payment failures and see what happens", "chaos"),
    ("audit our configuration for best practice", "drift"),
    ("are there any AWS health events affecting us?", "triage"),
    ("hello", "triage"),
])
def test_keyword_routing_when_no_model(tmp_path, request_text, expected):
    assert run(request_text, deps(tmp_path), DENY)["route"] == expected


def test_model_routing_is_used_and_recorded(tmp_path):
    d = deps(tmp_path, ScriptedLLM({Route: Route(agent="drift", reason="config question")}))
    r = run("anything", d, DENY)
    assert r["route"] == "drift"
    assert any(e["kind"] == "route" and e["source"] == "scripted-model" for e in d.audit.events)


# -- chaos: approval gate ------------------------------------------------------------------

def plan_llm(scenarios):
    return ScriptedLLM({
        Route: Route(agent="chaos", reason="x"),
        ExperimentPlan: ExperimentPlan(hypothesis="orders stay consistent", scenarios=scenarios, rationale="r"),
        Narrative: Narrative(summary="All good.", recommendations=["add alarms"])})


def test_nothing_runs_until_a_human_approves(tmp_path):
    aws = FakeAws()
    d = deps(tmp_path, plan_llm(["payment_flaky"]), aws)
    r = run("break payments", d, DENY)
    assert r["status"] == "cancelled" and aws.started == []
    assert any(e["kind"] == "cancelled" for e in d.audit.events)


def test_approved_plan_runs_baseline_first_and_audits_in_order(tmp_path):
    aws = FakeAws()
    d = deps(tmp_path, plan_llm(["payment_flaky", "shipping_down"]), aws)
    r = run("break payments", d, APPROVE)
    assert r["status"] == "completed" and aws.started == ["happy_path", "payment_flaky", "shipping_down"]
    kinds = [e["kind"] for e in d.audit.events]
    assert kinds.index("approval") < kinds.index("tool_call")        # approval is recorded before any action
    starts = [e for e in d.audit.events if e.get("tool") == "start_saga"]
    assert starts and all(e["approved"] is True for e in starts)


def test_invented_scenarios_are_dropped_and_plan_is_capped(tmp_path):
    names = ["delete_database", "payment_flaky", "payment_down", "inventory_down", "shipping_down",
             "refund_and_shipping_down"]
    aws = FakeAws()
    run("go", deps(tmp_path, plan_llm(names), aws), APPROVE)
    assert "delete_database" not in aws.started and len(aws.started) <= 5 and aws.started[0] == "happy_path"


def test_broken_invariant_is_reported_as_a_failure(tmp_path):
    aws = FakeAws(broken={"payment_down"})          # system wrongly confirms an order whose payment is down
    r = run("break payments", deps(tmp_path, plan_llm(["payment_down"]), aws), APPROVE)
    failed = [v for v in r["verdicts"] if not v["passed"]]
    assert [v["scenario"] for v in failed] == ["payment_down"]
    text = open(r["report_path"], encoding="utf-8").read()
    assert "FAIL" in text and "1/2 scenarios" in text


def test_chaos_still_works_when_every_model_is_down(tmp_path):
    aws = FakeAws()
    r = run("inject payment failures", deps(tmp_path, ScriptedLLM(down=True), aws), APPROVE)
    assert r["status"] == "completed" and r["plan"]["source"] == "rules"
    assert aws.started[0] == "happy_path" and "payment_flaky" in aws.started
    assert r["models_used"] == []


# -- resumable approval across processes -------------------------------------------------

def test_approval_survives_a_restart_via_the_sqlite_checkpoint(tmp_path):
    db = tmp_path / "cp.sqlite"
    aws = FakeAws()
    d = deps(tmp_path, plan_llm(["payment_flaky"]), aws)
    config = {"configurable": {"thread_id": "t-1"}}

    first = build_graph(d, SqliteSaver(sqlite3.connect(str(db), check_same_thread=False)))
    paused = first.invoke({"request": "break payments", "run_id": "t-1"}, config)
    assert paused["__interrupt__"][0].value["plan"]["scenarios"][:2] == ["happy_path", "payment_flaky"]
    assert aws.started == []

    second = build_graph(d, SqliteSaver(sqlite3.connect(str(db), check_same_thread=False)))   # "new process"
    done = second.invoke(Command(resume={"approved": True, "note": "after restart"}), config)
    assert done["status"] == "completed" and aws.started == ["happy_path", "payment_flaky"]


# -- tool registry rules -------------------------------------------------------------------

def registry(agent="triage"):
    audit = AuditLog()
    a = FakeAws()
    tools = [Tool("describe_stack", "", a.describe_stack), Tool("start_saga", "", a.start_saga, writes=True)]
    return ToolRegistry(tools, ALLOWED[agent] & {"describe_stack", "start_saga"}, audit, "r1", agent), audit, a


def test_agent_cannot_call_a_tool_outside_its_allowlist():
    reg, audit, aws = registry("triage")
    with pytest.raises(ToolDenied):
        reg.call("start_saga", approved=True, scenario="happy_path")
    assert aws.started == [] and audit.events[-1]["kind"] == "tool_denied"


def test_write_tool_needs_approval_even_when_allowlisted():
    reg, audit, aws = registry("chaos")
    with pytest.raises(ToolDenied):
        reg.call("start_saga", scenario="happy_path")
    assert aws.started == []
    reg.call("start_saga", approved=True, scenario="happy_path")
    assert aws.started == ["happy_path"]


def test_only_the_chaos_agent_may_write():
    assert "start_saga" in ALLOWED["chaos"]
    assert all("start_saga" not in tools for name, tools in ALLOWED.items() if name != "chaos")


# -- triage and drift ----------------------------------------------------------------------

def test_triage_without_a_model_maps_events_to_resources(tmp_path):
    r = run("any health events?", deps(tmp_path), DENY)
    items = {i["event"].split(":")[0]: i for i in r["narrative"]["items"]}
    assert items["LAMBDA"]["affected_resources"] == ["Lambda/ChargePayment"] and items["LAMBDA"]["severity"] == "medium"
    assert items["RDS"]["affected_resources"] == []
    assert "simulated feed" in open(r["report_path"], encoding="utf-8").read()


def test_triage_uses_model_output_when_available(tmp_path):
    out = TriageReport(summary="One event matters.", items=[])
    r = run("health events?", deps(tmp_path, ScriptedLLM({Route: Route(agent="triage", reason="x"), TriageReport: out})), DENY)
    assert r["narrative"]["summary"] == "One event matters."


def test_drift_drops_invented_finding_ids(tmp_path):
    proposals = [Proposal(finding_id="DDB_NO_POINT_IN_TIME_RECOVERY", explanation="e", cdk_change="pitr=True"),
                 Proposal(finding_id="MADE_UP", explanation="e", cdk_change="x")]
    llm = ScriptedLLM({Route: Route(agent="drift", reason="x"), DriftReport: DriftReport(summary="s", proposals=proposals)})
    r = run("audit config", deps(tmp_path, llm), DENY)
    assert [p["finding_id"] for p in r["narrative"]["proposals"]] == ["DDB_NO_POINT_IN_TIME_RECOVERY"]


def test_drift_without_a_model_proposes_from_rule_remediations(tmp_path):
    r = run("audit config drift", deps(tmp_path), DENY)
    assert {p["finding_id"] for p in r["narrative"]["proposals"]} == {"DDB_NO_POINT_IN_TIME_RECOVERY", "SNS_UNENCRYPTED"}
    assert "```python" in open(r["report_path"], encoding="utf-8").read()


# -- reports and invariants --------------------------------------------------------------------

def test_reports_redact_account_ids_and_arns():
    text = report.redact("arn:aws:sqs:us-east-1:123456789012:queue and https://sqs/123456789012/q")
    assert "123456789012" not in text and text.count("<account>") == 2


def test_audit_file_redacts_account_ids_but_memory_keeps_them(tmp_path):
    audit = AuditLog(tmp_path / "a.jsonl")
    audit.record("r", "tool_call", args={"executionArn": "arn:aws:states:us-east-1:123456789012:execution:x"})
    assert "123456789012" not in (tmp_path / "a.jsonl").read_text(encoding="utf-8")
    assert "123456789012" in audit.events[0]["args"]["executionArn"]


def test_invariant_evaluation_checks_order_not_just_presence():
    s = CATALOG["shipping_down"]
    good = dict(status="SUCCEEDED", outcome="REJECTED",
                states=["CreateShipment", "RefundPayment", "ReleaseStock"])
    wrong_order = {**good, "states": ["CreateShipment", "ReleaseStock", "RefundPayment"]}
    assert evaluate(s, good).passed and not evaluate(s, wrong_order).passed


def test_escalation_scenario_requires_failed_status_and_manual_review():
    s = CATALOG["refund_and_shipping_down"]
    assert evaluate(s, dict(status="FAILED", error="CompensationFailed", outcome=None,
                            states=["CreateShipment", "RefundPayment", "ManualReview"])).passed
    assert not evaluate(s, dict(status="SUCCEEDED", outcome="REJECTED", states=["RefundPayment"])).passed
