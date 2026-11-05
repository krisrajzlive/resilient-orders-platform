"""Command line: python -m resilience_agents "<request>" --profile udacity-lab --outputs deploy-outputs.local.json"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import boto3

from catalog_core.config import load_settings
from catalog_core.models import build_chat_models

from .audit import AuditLog
from .aws_tools import AwsTools
from .dr.tools import RealDrTools
from .graph import Deps
from .llm import StructuredLLM
from .runner import new_run_id, run

ROOT = Path(__file__).resolve().parents[3]


def prompt_for_approval(payload: dict) -> dict:
    plan = payload["plan"]
    if payload["action"] == "run_dr_drills":
        print("\n=== Approval required: run disaster-recovery drills (changes real infrastructure) ===")
        print(f"Hypothesis : {plan['hypothesis']}")
        print(f"Drills     : {', '.join(plan['drills'])}")
        print(f"Targets    : {', '.join(f'{d} <= {s}s' for d, s in plan['targets'].items())}")
        print(f"Estimated  : ${plan['estimatedCostUsd']:.2f} for the drills; billing right now: "
              f"{plan['billingNow']['items']} item(s), ${plan['billingNow']['perHour']:.3f}/hour")
    else:
        print("\n=== Approval required: run chaos experiment ===")
        print(f"Hypothesis : {plan['hypothesis']}")
        print(f"Scenarios  : {', '.join(plan['scenarios'])}")
        print(f"Executions : {len(plan['scenarios'])} (max {plan['maxExecutions']}), est. cost ${plan['estimatedCostUsd']:.2f}")
    answer = input("Approve? [y/N] ").strip().lower()
    return {"approved": answer in {"y", "yes"}, "note": "approved interactively" if answer in {"y", "yes"} else "declined"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("request")
    ap.add_argument("--profile", default="udacity-lab")
    ap.add_argument("--outputs", default=str(ROOT / "deploy-outputs.local.json"))
    ap.add_argument("--yes", action="store_true", help="auto-approve plans (non-interactive runs)")
    ap.add_argument("--no-llm", action="store_true", help="rules only, no model calls")
    a = ap.parse_args()

    outputs = next(iter(json.loads(Path(a.outputs).read_text(encoding="utf-8")).values()))
    session = boto3.Session(profile_name=a.profile, region_name="us-east-1")
    run_id = new_run_id()
    out_dir = ROOT / "docs" / "results" / "agent-runs"
    llm = None if a.no_llm else StructuredLLM(build_chat_models(load_settings()))
    deps = Deps(aws=AwsTools(session, outputs["StateMachineArn"]), llm=llm,
                audit=AuditLog(out_dir / f"{run_id}.audit.jsonl"), report_dir=out_dir, dr=RealDrTools(session))
    approve = (lambda p: {"approved": True, "note": "auto-approved with --yes"}) if a.yes else prompt_for_approval

    result = run(a.request, deps, approve, db_path=ROOT / ".agent-runs" / "checkpoints.sqlite", run_id=run_id)
    print(f"\nroute={result.get('route')} status={result.get('status')} models={sorted(set(result.get('models_used', [])))}")
    if result.get("report_path"):
        print(f"report: {result['report_path']}")


if __name__ == "__main__":
    main()
