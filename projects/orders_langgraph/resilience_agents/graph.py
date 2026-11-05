"""Supervisor graph: route a request to the chaos, triage or drift agent.

Safety properties (enforced in code, covered by tests):
  * the chaos agent cannot start any execution until a human approves the plan (graph interrupt);
  * each agent has its own tool allowlist, and write tools need an approval flag;
  * the model proposes, deterministic invariants judge, and every step is audited;
  * if no model is reachable every agent still produces a (rule-based) result.
"""
from __future__ import annotations

import operator
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from . import report
from .audit import AuditLog
from .llm import (
    DriftReport, ExperimentPlan, LLMUnavailable, Narrative, Proposal, Route, StructuredLLM,
    TriageItem, TriageReport,
)
from .scenarios import CATALOG, evaluate
from .tools import Tool, ToolDenied, ToolRegistry

MAX_EXECUTIONS = 5
EST_COST_PER_EXECUTION_USD = 0.01          # deliberately generous: real cost is a fraction of a cent

ALLOWED = {
    "chaos": {"describe_stack", "get_alarm_states", "start_saga", "wait_saga"},
    "triage": {"describe_stack", "get_alarm_states", "get_health_events"},
    "drift": {"describe_stack", "get_alarm_states", "check_policies"},
}


# Facts the resource inventory cannot show. Without them a model infers from names alone and errs
# (an earlier triage run concluded Bedrock was unused because no Bedrock resource is in the stack).
ARCHITECTURE = (
    "System: an order saga on AWS Step Functions (Standard). Lambda functions ReserveStock, ChargePayment, "
    "CreateShipment, RefundPayment, ReleaseStock keep state in DynamoDB. The AssessRisk Lambda calls Amazon Bedrock "
    "(Claude Haiku, falling back to Nova) through LangChain. High-risk orders wait on an SQS review queue. "
    "SNS topics carry order events and alerts. There is no RDS, ECS or VPC in this stack yet."
)
CDK_HINT = ("Use current aws-cdk-lib v2 Python names (for DynamoDB use point_in_time_recovery_specification="
            "dynamodb.PointInTimeRecoverySpecification(point_in_time_recovery_enabled=True), not the deprecated "
            "point_in_time_recovery).")


class AgentState(TypedDict, total=False):
    request: str
    run_id: str
    route: str
    plan: dict
    approved: bool
    approval_note: str
    executions: list[dict]
    verdicts: list[dict]
    findings: list[dict]
    health: dict
    narrative: dict
    models_used: Annotated[list[str], operator.add]
    report_path: str
    status: str


@dataclass
class Deps:
    aws: Any                       # AwsTools or a stub with the same methods
    llm: StructuredLLM | None
    audit: AuditLog
    report_dir: Path


def _registry(deps: Deps, agent: str, run_id: str) -> ToolRegistry:
    a = deps.aws
    tools = [
        Tool("describe_stack", "List resources in the order-saga stack.", a.describe_stack),
        Tool("get_alarm_states", "Read CloudWatch alarm states.", a.get_alarm_states),
        Tool("get_health_events", "Read AWS Health events (simulated if unavailable).", a.get_health_events),
        Tool("check_policies", "Run read-only configuration policy checks.", a.check_policies),
        Tool("start_saga", "Start one order-saga execution for a catalogued scenario.", a.start_saga, writes=True),
        Tool("wait_saga", "Wait for an execution and summarize it.", a.wait_saga),
    ]
    return ToolRegistry(tools, ALLOWED[agent], deps.audit, run_id, agent)


def _ask(deps: Deps, state: AgentState, schema, prompt: str):
    """Returns (parsed | None, model_id | None). Never raises when models are down."""
    if deps.llm is None:
        return None, None
    try:
        out, model_id = deps.llm.invoke(schema, prompt)
        deps.audit.record(state["run_id"], "model_call", schema=schema.__name__, model=model_id)
        return out, model_id
    except LLMUnavailable as exc:
        deps.audit.record(state["run_id"], "model_unavailable", schema=schema.__name__, detail=str(exc)[:200])
        return None, None


_KEYWORDS = {
    "chaos": r"chaos|fault|failure|fail|experiment|resilien|inject|break|outage test",
    "drift": r"drift|config|complian|polic|audit|misconfig|best practice",
    "triage": r"health|event|incident|alarm|alert|status|triage|outage",
}


def _keyword_route(text: str) -> str:
    for agent in ("chaos", "drift", "triage"):
        if re.search(_KEYWORDS[agent], text.lower()):
            return agent
    return "triage"


def build_graph(deps: Deps, checkpointer):
    # ---- routing -----------------------------------------------------------------------
    def route_request(state: AgentState) -> dict:
        out, model = _ask(deps, state, Route,
                          "Pick the agent for this operations request. chaos = run failure experiments against "
                          "the order workflow; triage = assess AWS events and alarms; drift = audit configuration "
                          f"against best practice.\nRequest: {state['request']}")
        route = out.agent if out else _keyword_route(state["request"])
        deps.audit.record(state["run_id"], "route", route=route, source=model or "keywords")
        return {"route": route, "models_used": [model] if model else []}

    # ---- chaos agent -------------------------------------------------------------------
    def chaos_plan(state: AgentState) -> dict:
        catalog = "\n".join(f"- {s.name}: {s.description}" for s in CATALOG.values())
        out, model = _ask(deps, state, ExperimentPlan,
                          f"Plan a small chaos experiment. {ARCHITECTURE}\n"
                          f"Choose scenarios ONLY from this catalog:\n{catalog}\n"
                          f"At most {MAX_EXECUTIONS}. Always include the happy_path baseline.\n"
                          f"Operator request: {state['request']}")
        chosen = [s for s in (out.scenarios if out else []) if s in CATALOG]      # drop invented names
        if not chosen:
            chosen = [s for s, pat in (("payment_flaky", r"payment|charge|card"), ("payment_down", r"payment|charge"),
                                       ("shipping_down", r"ship|deliver"), ("inventory_down", r"stock|inventor"),
                                       ("refund_and_shipping_down", r"refund|compensat"))
                      if re.search(pat, state["request"].lower())] or ["payment_flaky", "shipping_down"]
        scenarios = list(dict.fromkeys(["happy_path", *chosen]))[:MAX_EXECUTIONS]
        plan = {"hypothesis": out.hypothesis if out else "Injected failures are retried or compensated; no order is left inconsistent.",
                "rationale": out.rationale if out else "Rule-based plan (no model reachable).",
                "scenarios": scenarios, "source": model or "rules", "maxExecutions": MAX_EXECUTIONS,
                "estimatedCostUsd": round(len(scenarios) * EST_COST_PER_EXECUTION_USD, 2)}
        deps.audit.record(state["run_id"], "plan", **plan)
        return {"plan": plan, "models_used": [model] if model else []}

    def chaos_approve(state: AgentState) -> dict:
        decision = interrupt({"type": "approval_required", "action": "run_chaos_experiment", "plan": state["plan"]})
        approved = bool(decision.get("approved")) if isinstance(decision, dict) else bool(decision)
        note = decision.get("note", "") if isinstance(decision, dict) else ""
        deps.audit.record(state["run_id"], "approval", approved=approved, note=note, scenarios=state["plan"]["scenarios"])
        return {"approved": approved, "approval_note": note}

    def chaos_execute(state: AgentState) -> dict:
        reg = _registry(deps, "chaos", state["run_id"])
        executions, verdicts = [], []
        for name in state["plan"]["scenarios"][:MAX_EXECUTIONS]:
            try:
                started = reg.call("start_saga", approved=state["approved"], scenario=name)
                summary = reg.call("wait_saga", execution_arn=started["executionArn"])
            except ToolDenied:
                raise
            except Exception as exc:
                summary = {"status": "ERROR", "error": type(exc).__name__, "states": []}
            executions.append({"scenario": name, **summary})
            v = evaluate(CATALOG[name], summary)
            verdicts.append({"scenario": v.scenario, "passed": v.passed, "failures": v.failures})
        alarms = reg.call("get_alarm_states")
        return {"executions": executions, "verdicts": verdicts,
                "narrative": {"summary": "", "recommendations": [], "alarms": alarms}}

    def chaos_report(state: AgentState) -> dict:
        ok = [v for v in state["verdicts"] if v["passed"]]
        facts = "\n".join(f"- {e['scenario']}: status={e.get('status')} outcome={e.get('outcome')} error={e.get('error')} "
                          f"passed={next(v['passed'] for v in state['verdicts'] if v['scenario'] == e['scenario'])}"
                          for e in state["executions"])
        out, model = _ask(deps, state, Narrative,
                          "Summarize this chaos experiment for an engineer in 2-3 sentences and give up to 3 concrete "
                          f"recommendations. Hypothesis: {state['plan']['hypothesis']}\nResults:\n{facts}")
        narrative = {"summary": out.summary if out else f"{len(ok)} of {len(state['verdicts'])} scenarios held their invariants.",
                     "recommendations": out.recommendations if out else
                     [f"Investigate {v['scenario']}: {'; '.join(v['failures'])}" for v in state["verdicts"] if not v["passed"]]
                     or ["No invariant was broken; extend the catalog with a new failure."]}
        new = {**state, "narrative": narrative}
        path = report.write_report(deps.report_dir, state["run_id"], report.render_chaos(new))
        return {"narrative": narrative, "report_path": str(path), "status": "completed",
                "models_used": [model] if model else []}

    def cancelled(state: AgentState) -> dict:
        deps.audit.record(state["run_id"], "cancelled", reason="approval denied")
        return {"status": "cancelled"}

    # ---- triage agent ------------------------------------------------------------------
    def triage_run(state: AgentState) -> dict:
        reg = _registry(deps, "triage", state["run_id"])
        health, resources = reg.call("get_health_events"), reg.call("describe_stack")
        alarms = reg.call("get_alarm_states")
        kinds = sorted({r["type"].split("::")[1] + "/" + r["logicalId"] for r in resources if "Policy" not in r["type"] and "Role" not in r["type"]})
        events = "\n".join(f"- [{e['statusCode']}] {e['service']}: {e['description']}" for e in health["events"])
        out, model = _ask(deps, state, TriageReport,
                          "Triage these AWS events for an order platform. Map each to the affected resources from the list "
                          "(use [] if none) and give severity (info/low/medium/high) and one recommended action.\n"
                          f"{ARCHITECTURE}\nResources: {', '.join(kinds)}\nEvents:\n{events}\nAlarms: {alarms}")
        if out:
            narrative = out.model_dump()
        else:
            hits = {"LAMBDA": "Lambda", "DYNAMODB": "DynamoDB", "BEDROCK": "AssessRisk", "SNS": "SNS", "SQS": "SQS"}
            items = []
            for e in health["events"]:
                key = hits.get(e["service"], "")
                affected = [k for k in kinds if key and key.lower() in k.lower()]
                sev = ("medium" if e["statusCode"] == "open" else "info") if affected else "info"
                items.append(TriageItem(event=f"{e['service']}: {e['description'][:90]}", affected_resources=affected,
                                        severity=sev, recommended_action="Monitor" if affected else "No action for this stack").model_dump())
            narrative = {"summary": f"{len(items)} events reviewed (rule-based, no model reachable).", "items": items}
        new = {**state, "health": health, "narrative": narrative}
        path = report.write_report(deps.report_dir, state["run_id"], report.render_triage(new))
        return {"health": health, "narrative": narrative, "report_path": str(path), "status": "completed",
                "models_used": [model] if model else []}

    # ---- drift agent -------------------------------------------------------------------
    def drift_run(state: AgentState) -> dict:
        reg = _registry(deps, "drift", state["run_id"])
        findings = reg.call("check_policies")
        listing = "\n".join(f"- {f['id']} ({f['severity']}) on {f['resource']}: {f['detail']}" for f in findings)
        out, model = _ask(deps, state, DriftReport,
                          "These configuration findings were found on a Step Functions + Lambda + DynamoDB order workflow "
                          "built with Python CDK. Explain each briefly and propose a minimal CDK change per finding id, "
                          f"as code.\n{ARCHITECTURE}\n{CDK_HINT}\n" + listing)
        ids = {f["id"] for f in findings}
        if out:
            proposals = [p.model_dump() for p in out.proposals if p.finding_id in ids]      # drop invented ids
            summary = out.summary
        else:
            proposals = [Proposal(finding_id=f["id"], explanation=f["detail"], cdk_change=f"# {f['remediation']}").model_dump()
                         for f in findings]
            summary = f"{len(findings)} findings (rule-based, no model reachable)."
        narrative = {"summary": summary, "proposals": proposals}
        new = {**state, "findings": findings, "narrative": narrative}
        path = report.write_report(deps.report_dir, state["run_id"], report.render_drift(new))
        return {"findings": findings, "narrative": narrative, "report_path": str(path), "status": "completed",
                "models_used": [model] if model else []}

    g = StateGraph(AgentState)
    for name, fn in [("route_request", route_request), ("chaos_plan", chaos_plan), ("chaos_approve", chaos_approve),
                     ("chaos_execute", chaos_execute), ("chaos_report", chaos_report), ("cancelled", cancelled),
                     ("triage_run", triage_run), ("drift_run", drift_run)]:
        g.add_node(name, fn)
    g.add_edge(START, "route_request")
    g.add_conditional_edges("route_request", lambda s: {"chaos": "chaos_plan", "triage": "triage_run", "drift": "drift_run"}[s["route"]])
    g.add_edge("chaos_plan", "chaos_approve")
    g.add_conditional_edges("chaos_approve", lambda s: "chaos_execute" if s["approved"] else "cancelled")
    g.add_edge("chaos_execute", "chaos_report")
    for end in ("chaos_report", "cancelled", "triage_run", "drift_run"):
        g.add_edge(end, END)
    return g.compile(checkpointer=checkpointer)
