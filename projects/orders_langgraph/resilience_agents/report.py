"""Markdown reports. Account ids and ARNs are redacted before anything is written to disk."""
from __future__ import annotations

import re
from pathlib import Path

_ACCOUNT = re.compile(r"\b\d{12}\b")


def redact(text: str) -> str:
    return _ACCOUNT.sub("<account>", text)


def _table(headers: list[str], rows: list[list[str]]) -> str:
    head = "| " + " | ".join(headers) + " |\n|" + "|".join("---" for _ in headers) + "|\n"
    return head + "\n".join("| " + " | ".join(str(c).replace("|", "\\|") for c in r) + " |" for r in rows) + "\n"


def render_chaos(state: dict) -> str:
    plan, verdicts = state["plan"], {v["scenario"]: v for v in state.get("verdicts", [])}
    rows = []
    for e in state.get("executions", []):
        v = verdicts.get(e["scenario"], {})
        rows.append([e["scenario"], "PASS" if v.get("passed") else "FAIL", e.get("status"), e.get("outcome") or "-",
                     e.get("error") or "-", f"{e.get('seconds', '-')}s", "; ".join(v.get("failures", [])) or "-"])
    n = state.get("narrative", {})
    passed = sum(1 for v in verdicts.values() if v["passed"])
    return "\n".join([
        f"# Chaos experiment `{state['run_id']}`", "",
        f"**Request:** {state['request']}", "",
        f"**Hypothesis:** {plan['hypothesis']}", "",
        f"**Plan source:** {plan['source']} | **Approved by:** human ({state.get('approval_note') or 'no note'})", "",
        f"## Result: {passed}/{len(verdicts)} scenarios held their invariants", "",
        _table(["Scenario", "Verdict", "Execution", "Outcome", "Error", "Duration", "Broken invariants"], rows),
        "## Summary", "", n.get("summary", ""), "", "## Recommendations", "",
        *[f"- {r}" for r in n.get("recommendations", [])], "",
        f"Models used: {', '.join(dict.fromkeys(state.get('models_used', []))) or 'none (rules only)'}", ""])


def render_triage(state: dict) -> str:
    t = state["narrative"]
    rows = [[i["event"], ", ".join(i["affected_resources"]) or "-", i["severity"], i["recommended_action"]]
            for i in t["items"]]
    return "\n".join([
        f"# Event triage `{state['run_id']}`", "", f"**Request:** {state['request']}", "",
        f"**Event source:** {state['health']['source']}"
        + (" (AWS Health needs Business support; this is a simulated feed)" if state["health"]["source"] == "simulated" else ""),
        "", t["summary"], "", _table(["Event", "Affected resources", "Severity", "Recommended action"], rows),
        f"Models used: {', '.join(state.get('models_used', [])) or 'none (rules only)'}", ""])


def render_drift(state: dict) -> str:
    d = state["narrative"]
    findings = {f["id"]: f for f in state["findings"]}
    rows = [[f["id"], f["severity"], f["resource"], f["detail"]] for f in state["findings"]]
    out = [f"# Configuration drift `{state['run_id']}`", "", f"**Request:** {state['request']}", "",
           d["summary"], "", _table(["Rule", "Severity", "Resource", "Detail"], rows) if rows else "No findings.\n"]
    for p in d["proposals"]:
        out += [f"### Proposed fix: {p['finding_id']}", "", p["explanation"], "", "```python", p["cdk_change"], "```", ""]
        findings.pop(p["finding_id"], None)
    out.append(f"Models used: {', '.join(state.get('models_used', [])) or 'none (rules only)'}")
    return "\n".join(out) + "\n"


def write_report(report_dir: Path, run_id: str, body: str) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{run_id}.md"
    path.write_text(redact(body), encoding="utf-8")
    return path
