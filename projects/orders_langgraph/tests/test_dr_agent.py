"""DR agent behaviour: approval gate, blocked state, safe failure, billing check. Fake model, fake DR tools."""
from __future__ import annotations

import pytest

from resilience_agents.audit import AuditLog
from resilience_agents.graph import ALLOWED, Deps
from resilience_agents.llm import DrPlan, LLMUnavailable, Narrative, Route
from resilience_agents.runner import run
from resilience_agents.tools import Tool, ToolDenied, ToolRegistry
from test_agents import APPROVE, DENY, FakeAws, ScriptedLLM

NOTHING_BILLING = {"billable": [], "perHour": 0.0, "skipped": []}
DB_BILLING = {"billable": [{"service": "RDS instance", "resource": "orders-db", "per_hour": 0.032}], "perHour": 0.032, "skipped": []}


def result(name, passed=True, **metrics):
    return {"drill": name, "passed": passed, "failures": [] if passed else ["problem"],
            "metrics": {"rto_seconds": 60.0, "lost_writes": 0, **metrics}, "target_seconds": 180}


class FakeDr:
    def __init__(self, *, ok=True, reason="", billing_after=None, fail_on=None, raise_on=None):
        self.ok, self.reason = ok, reason
        self.billing_after = billing_after or NOTHING_BILLING
        self.fail_on, self.raise_on = fail_on, raise_on
        self.ran: list[str] = []
        self.cost_checks = 0

    def preflight(self):
        self.cost_check()                  # the real pre-flight also looks at what is billing
        out = {"ok": self.ok, "reason": self.reason, "billable": DB_BILLING if self.ok else NOTHING_BILLING,
               "hint": "cdk deploy OrdersData -c dr=true"}
        if self.ok:
            out["database"] = {"status": "available", "multiAz": True, "az": "us-east-1a"}
        return out

    def run_drill(self, name):
        if name == self.raise_on:
            raise RuntimeError("boom")
        if name == getattr(self, "interrupt_on", None):
            raise KeyboardInterrupt()          # the process is stopped mid-run
        self.ran.append(name)
        return result(name, passed=name != self.fail_on)

    def cost_check(self):
        self.cost_checks += 1
        return self.billing_after


def deps(tmp_path, dr, llm=None):
    return Deps(aws=FakeAws(), llm=llm, audit=AuditLog(tmp_path / "audit.jsonl"),
                report_dir=tmp_path / "reports", dr=dr)


def dr_llm(drills):
    return ScriptedLLM({Route: Route(agent="dr", reason="x"),
                        DrPlan: DrPlan(drills=drills, hypothesis="recovers", rationale="r"),
                        Narrative: Narrative(summary="All good.", recommendations=["repeat monthly"])})


@pytest.mark.parametrize("text,expected", [
    ("run a disaster recovery drill", "dr"), ("test the database failover", "dr"),
    ("can we restore from backup in another region?", "dr"), ("what are our RTO and RPO?", "dr"),
    ("inject payment failures", "chaos"), ("audit our configuration", "drift"), ("any health events?", "triage"),
])
def test_requests_route_to_the_right_agent(tmp_path, text, expected):
    assert run(text, deps(tmp_path, FakeDr()), DENY)["route"] == expected


# -- blocked ---------------------------------------------------------------------------------------

def test_a_missing_database_blocks_the_run_without_asking_for_approval(tmp_path):
    dr = FakeDr(ok=False, reason="the DR database stack is not deployed")
    asked = []
    d = deps(tmp_path, dr)
    r = run("run a disaster recovery drill", d, lambda payload: asked.append(payload) or APPROVE(payload))
    assert r["status"] == "blocked" and asked == [] and dr.ran == []
    text = open(r["report_path"], encoding="utf-8").read()
    assert "Not run" in text and "not deployed" in text and "nothing was changed" in text
    assert any(e["kind"] == "dr_blocked" for e in d.audit.events)


def test_the_dr_route_without_any_dr_tooling_is_blocked_not_crashed(tmp_path):
    r = run("disaster recovery drill", deps(tmp_path, None), APPROVE)
    assert r["status"] == "blocked" and "not configured" in open(r["report_path"], encoding="utf-8").read()


# -- approval gate ------------------------------------------------------------------------------------

def test_nothing_runs_until_a_human_approves(tmp_path):
    dr = FakeDr()
    r = run("run a dr drill", deps(tmp_path, dr, dr_llm(["az_failover"])), DENY)
    assert r["status"] == "cancelled" and dr.ran == [] and dr.cost_checks == 1      # only the pre-flight billing look


def test_the_approval_request_shows_drills_targets_cost_and_what_is_billing_now(tmp_path):
    seen = {}
    run("run a dr drill", deps(tmp_path, FakeDr(), dr_llm(["az_failover", "backup_restore"])),
        lambda p: seen.update(p) or DENY(p))
    plan = seen["plan"]
    assert seen["action"] == "run_dr_drills" and plan["drills"] == ["az_failover", "backup_restore"]
    assert plan["targets"]["az_failover"] == 180 and plan["estimatedCostUsd"] == pytest.approx(0.06)
    assert plan["billingNow"] == {"items": 1, "perHour": 0.032}


def test_approved_drills_run_in_canonical_order_with_audited_approval_and_a_final_billing_check(tmp_path):
    dr = FakeDr()
    d = deps(tmp_path, dr, dr_llm(["region_restore", "az_failover", "backup_restore"]))      # model order is ignored
    r = run("run all drills", d, APPROVE)
    assert dr.ran == ["az_failover", "backup_restore", "region_restore"] and r["status"] == "completed"
    events = d.audit.events
    first_drill = next(i for i, e in enumerate(events) if e.get("tool") == "run_drill")
    assert next(i for i, e in enumerate(events) if e["kind"] == "approval") < first_drill     # approval precedes any change
    runs = [e for e in events if e.get("tool") == "run_drill"]
    assert len(runs) == 3 and all(e["approved"] is True for e in runs)
    assert dr.cost_checks == 2                                    # once in the pre-flight, once after the drills
    assert "3/3 drills met their targets" in open(r["report_path"], encoding="utf-8").read()


def test_invented_drill_names_are_dropped(tmp_path):
    dr = FakeDr()
    run("go", deps(tmp_path, dr, dr_llm(["drop_database", "az_failover"])), APPROVE)
    assert dr.ran == ["az_failover"]


# -- safety ------------------------------------------------------------------------------------------------

def test_an_unexpected_error_stops_further_drills_but_the_billing_check_still_runs(tmp_path):
    dr = FakeDr(raise_on="backup_restore")
    r = run("run all drills", deps(tmp_path, dr, dr_llm(["az_failover", "backup_restore", "region_restore"])), APPROVE)
    assert dr.ran == ["az_failover"] and dr.cost_checks == 2
    assert [d["drill"] for d in r["drills"]] == ["az_failover", "backup_restore"]
    assert "aborted" in r["drills"][1]["failures"][0] and r["drills"][1]["passed"] is False


def test_each_drill_result_is_saved_as_it_finishes_so_a_stopped_run_keeps_what_it_measured(tmp_path):
    dr = FakeDr()
    dr.interrupt_on = "backup_restore"
    d = deps(tmp_path, dr, dr_llm(["az_failover", "backup_restore", "region_restore"]))
    with pytest.raises(KeyboardInterrupt):
        run("run all drills", d, APPROVE)
    saved = [e for e in d.audit.events if e["kind"] == "drill_result"]
    assert [e["drill"] for e in saved] == ["az_failover"] and saved[0]["metrics"]["rto_seconds"] == 60.0
    on_disk = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert '"drill_result"' in on_disk and "az_failover" in on_disk            # already persisted, not just in memory


def test_an_aborted_drill_is_also_recorded(tmp_path):
    d = deps(tmp_path, FakeDr(raise_on="az_failover"), dr_llm(["az_failover"]))
    run("failover drill", d, APPROVE)
    saved = [e for e in d.audit.events if e["kind"] == "drill_result"]
    assert len(saved) == 1 and saved[0]["passed"] is False and "aborted" in saved[0]["failures"][0]


def test_a_failed_drill_is_reported_not_hidden(tmp_path):
    r = run("failover drill", deps(tmp_path, FakeDr(fail_on="az_failover"), dr_llm(["az_failover"])), APPROVE)
    text = open(r["report_path"], encoding="utf-8").read()
    assert "FAIL" in text and "0/1 drills met their targets" in text


def test_resources_still_billing_after_the_run_are_called_out(tmp_path):
    r = run("failover drill", deps(tmp_path, FakeDr(billing_after=DB_BILLING), dr_llm(["az_failover"])), APPROVE)
    text = open(r["report_path"], encoding="utf-8").read()
    assert "Still billing" in text and "orders-db" in text and "Destroy the DR stacks" in text


def test_a_clean_account_after_the_run_is_reported_as_clean(tmp_path):
    r = run("failover drill", deps(tmp_path, FakeDr(), dr_llm(["az_failover"])), APPROVE)
    assert "Nothing billable is running." in open(r["report_path"], encoding="utf-8").read()


def test_the_dr_agent_still_works_when_every_model_is_down(tmp_path):
    dr = FakeDr()
    r = run("test the database failover only", deps(tmp_path, dr, ScriptedLLM(down=True)), APPROVE)
    assert r["plan"]["source"] == "rules" and dr.ran == ["az_failover"] and r["models_used"] == []


def test_without_a_hint_the_rule_based_plan_covers_all_three_drills(tmp_path):
    dr = FakeDr()
    run("run a disaster recovery drill", deps(tmp_path, dr, ScriptedLLM(down=True)), APPROVE)
    assert dr.ran == ["az_failover", "backup_restore", "region_restore"]


# -- tool rules ------------------------------------------------------------------------------------------------

def registry(agent):
    audit, dr = AuditLog(), FakeDr()
    tools = [Tool("run_drill", "", dr.run_drill, writes=True), Tool("start_saga", "", FakeAws().start_saga, writes=True),
             Tool("cost_check", "", dr.cost_check)]
    return ToolRegistry(tools, ALLOWED[agent], audit, "r1", agent), dr


def test_only_the_dr_agent_may_run_drills_and_it_may_not_start_orders():
    reg, dr = registry("chaos")
    with pytest.raises(ToolDenied):
        reg.call("run_drill", approved=True, name="az_failover")
    reg, dr = registry("dr")
    with pytest.raises(ToolDenied):
        reg.call("start_saga", approved=True, scenario="happy_path")
    assert dr.ran == []


def test_running_a_drill_needs_approval_even_for_the_dr_agent():
    reg, dr = registry("dr")
    with pytest.raises(ToolDenied):
        reg.call("run_drill", name="az_failover")
    reg.call("run_drill", approved=True, name="az_failover")
    assert dr.ran == ["az_failover"]


def test_no_other_agent_can_run_drills():
    assert all("run_drill" not in tools for name, tools in ALLOWED.items() if name != "dr")
