"""Supervisor graph: route a request to the chaos, triage, drift or disaster-recovery agent.

Safety properties (enforced in code, covered by tests):
  * the chaos and DR agents cannot start anything until a human approves the plan (graph interrupt);
  * the DR agent refuses to plan when the database is not deployed, and always ends with a billing check;
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
from .dr.engine import DRILLS, TARGETS
from .dr.tools import EST_COST_USD
from .llm import (
    DrPlan, DriftReport, ExperimentPlan, LLMUnavailable, Narrative, Proposal, Route, StructuredLLM,
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
    "dr": {"dr_preflight", "run_drill", "cost_check"},
}

DRILL_DESCRIPTIONS = {
    "az_failover": "Force the Multi-AZ database to fail over to its standby while writes continue; measure the outage and lost writes.",
    "backup_restore": "Back up the live database, restore it to a temporary instance, and verify the data and vector search.",
    "region_restore": "Copy the backup to the second region, restore there, and verify the data (simulates losing a region).",
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
    drills: list[dict]
    cost_after: dict
    models_used: Annotated[list[str], operator.add]
    report_path: str
    status: str


@dataclass
class Deps:
    aws: Any                       # AwsTools or a stub with the same methods
    llm: StructuredLLM | None
    audit: AuditLog
    report_dir: Path
    dr: Any = None                 # RealDrTools or a stub: preflight(), run_drill(name), cost_check()


def _registry(deps: Deps, agent: str, run_id: str) -> ToolRegistry:
    a = deps.aws
    dr_tools = [] if deps.dr is None else [
        Tool("dr_preflight", "Check the DR database is deployed and healthy; list what is billing.", deps.dr.preflight),
        Tool("run_drill", "Run one disaster-recovery drill (changes real infrastructure).", deps.dr.run_drill, writes=True),
        Tool("cost_check", "List resources that bill by the hour in both regions.", deps.dr.cost_check),
    ]
    tools = [*dr_tools,
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
    "dr": r"disaster|\bdr\b|failover|fail over|restore|backup|\brto\b|\brpo\b|region loss|lose a region|multi-az",
    "chaos": r"chaos|fault|failure|fail|experiment|resilien|inject|break|outage test",
    "drift": r"drift|config|complian|polic|audit|misconfig|best practice",
    "triage": r"health|event|incident|alarm|alert|status|triage|outage",
}


def _keyword_route(text: str) -> str:
    for agent in ("dr", "chaos", "drift", "triage"):
        if re.search(_KEYWORDS[agent], text.lower()):
            return agent
    return "triage"


def build_graph(deps: Deps, checkpointer):
    # ---- routing -----------------------------------------------------------------------
    def route_request(state: AgentState) -> dict:
        out, model = _ask(deps, state, Route,
                          "Pick the agent for this operations request. chaos = run failure experiments against "
                          "the order workflow; triage = assess AWS events and alarms; drift = audit configuration "
                          "against best practice; dr = disaster-recovery drills on the database (failover, "
                          f"backup restore, region loss).\nRequest: {state['request']}")
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
            deps.audit.record(state["run_id"], "scenario_result", scenario=name, passed=v.passed,
                              failures=v.failures, status=summary.get("status"), outcome=summary.get("outcome"))
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

    # ---- disaster-recovery agent ------------------------------------------------------------
    def dr_plan(state: AgentState) -> dict:
        if deps.dr is None:
            pre = {"ok": False, "reason": "disaster-recovery tooling is not configured", "hint": "",
                   "billable": {"billable": [], "perHour": 0}}
        else:
            pre = _registry(deps, "dr", state["run_id"]).call("dr_preflight")
        if not pre["ok"]:
            deps.audit.record(state["run_id"], "dr_blocked", reason=pre["reason"])
            return {"plan": {"hypothesis": "", "source": "none", "drills": [],
                             "blocked": {"reason": pre["reason"], "hint": pre.get("hint", "")}},
                    "status": "blocked"}
        catalog = "\n".join(f"- {n}: {d}" for n, d in DRILL_DESCRIPTIONS.items())
        out, model = _ask(deps, state, DrPlan,
                          "Plan disaster-recovery drills for a PostgreSQL order database (Multi-AZ, with backups copied "
                          f"to a second region). Choose drills ONLY from this catalog:\n{catalog}\n"
                          f"Operator request: {state['request']}")
        chosen = [d for d in (out.drills if out else []) if d in DRILLS]            # drop invented names
        if not chosen:
            text = state["request"].lower()
            chosen = [d for d, pat in (("az_failover", r"failover|fail over|\baz\b|zone|multi-az"),
                                       ("backup_restore", r"backup|restore|rpo"),
                                       ("region_restore", r"region")) if re.search(pat, text)] or list(DRILLS)
        drills = [d for d in DRILLS if d in chosen]       # canonical order: region restore reuses the restore drill's backup
        billable = pre["billable"]
        plan = {"hypothesis": out.hypothesis if out else "The database survives losing a zone and can be restored, in the same region and in another, within the targets.",
                "rationale": out.rationale if out else "Rule-based plan (no model reachable).",
                "drills": drills, "source": model or "rules", "targets": {d: TARGETS[d] for d in drills},
                "estimatedCostUsd": round(sum(EST_COST_USD[d] for d in drills), 2),
                "billingNow": {"items": len(billable["billable"]), "perHour": billable["perHour"]},
                "database": pre.get("database")}
        deps.audit.record(state["run_id"], "plan", **{k: v for k, v in plan.items() if k != "rationale"})
        return {"plan": plan, "models_used": [model] if model else []}

    def dr_approve(state: AgentState) -> dict:
        decision = interrupt({"type": "approval_required", "action": "run_dr_drills", "plan": state["plan"]})
        approved = bool(decision.get("approved")) if isinstance(decision, dict) else bool(decision)
        note = decision.get("note", "") if isinstance(decision, dict) else ""
        deps.audit.record(state["run_id"], "approval", approved=approved, note=note, drills=state["plan"]["drills"])
        return {"approved": approved, "approval_note": note}

    def dr_execute(state: AgentState) -> dict:
        reg = _registry(deps, "dr", state["run_id"])
        results: list[dict] = []
        try:
            for name in state["plan"]["drills"]:
                try:
                    outcome = reg.call("run_drill", approved=state["approved"], name=name)
                except ToolDenied:
                    raise
                except Exception as exc:        # an unexpected error: stop, so a broken setup is not repeated at a cost
                    outcome = {"drill": name, "passed": False, "failures": [f"drill aborted: {type(exc).__name__}"],
                               "metrics": {}, "target_seconds": TARGETS.get(name)}
                    results.append(outcome)
                    deps.audit.record(state["run_id"], "drill_result", **outcome)
                    break
                results.append(outcome)
                # Written to the audit file the moment the drill ends: a run that is stopped or killed later
                # (the final report is only written at the end) must not lose results already measured.
                deps.audit.record(state["run_id"], "drill_result", **outcome)
        finally:
            cost_after = reg.call("cost_check")      # always prove what is still billing
        return {"drills": results, "cost_after": cost_after}

    def dr_report(state: AgentState) -> dict:
        if state.get("status") == "blocked":
            path = report.write_report(deps.report_dir, state["run_id"], report.render_dr(state))
            return {"report_path": str(path), "status": "blocked"}
        facts = "\n".join(f"- {d['drill']}: {'passed' if d['passed'] else 'FAILED'} metrics={d['metrics']} problems={d['failures']}"
                          for d in state["drills"])
        out, model = _ask(deps, state, Narrative,
                          "Summarize these disaster-recovery drill results for an engineer in 2-3 sentences and give up to "
                          f"3 concrete recommendations. Hypothesis: {state['plan']['hypothesis']}\nResults:\n{facts}")
        failed = [d for d in state["drills"] if not d["passed"]]
        narrative = {"summary": out.summary if out else f"{len(state['drills']) - len(failed)} of {len(state['drills'])} drills met their targets.",
                     "recommendations": out.recommendations if out else
                     [f"Investigate {d['drill']}: {'; '.join(d['failures'])}" for d in failed] or ["All drills met their targets; repeat them after any infrastructure change."]}
        new = {**state, "narrative": narrative}
        path = report.write_report(deps.report_dir, state["run_id"], report.render_dr(new))
        return {"narrative": narrative, "report_path": str(path), "status": "completed",
                "models_used": [model] if model else []}

    g = StateGraph(AgentState)
    for name, fn in [("route_request", route_request), ("chaos_plan", chaos_plan), ("chaos_approve", chaos_approve),
                     ("chaos_execute", chaos_execute), ("chaos_report", chaos_report), ("cancelled", cancelled),
                     ("triage_run", triage_run), ("drift_run", drift_run),
                     ("dr_plan", dr_plan), ("dr_approve", dr_approve), ("dr_execute", dr_execute), ("dr_report", dr_report)]:
        g.add_node(name, fn)
    g.add_edge(START, "route_request")
    g.add_conditional_edges("route_request", lambda s: {"chaos": "chaos_plan", "triage": "triage_run",
                                                         "drift": "drift_run", "dr": "dr_plan"}[s["route"]])
    g.add_edge("chaos_plan", "chaos_approve")
    g.add_conditional_edges("chaos_approve", lambda s: "chaos_execute" if s["approved"] else "cancelled")
    g.add_edge("chaos_execute", "chaos_report")
    g.add_conditional_edges("dr_plan", lambda s: "dr_report" if s.get("status") == "blocked" else "dr_approve")
    g.add_conditional_edges("dr_approve", lambda s: "dr_execute" if s["approved"] else "cancelled")
    g.add_edge("dr_execute", "dr_report")
    for end in ("chaos_report", "cancelled", "triage_run", "drift_run", "dr_report"):
        g.add_edge(end, END)
    return g.compile(checkpointer=checkpointer)
