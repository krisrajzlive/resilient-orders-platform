"""Run the supervisor graph: start, pause at approval interrupts, resume with a human decision."""
from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path
from typing import Callable

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from .graph import Deps, build_graph

Approver = Callable[[dict], dict]


def new_run_id() -> str:
    return time.strftime("run-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]


def make_checkpointer(db_path: Path | None):
    if db_path is None:
        return MemorySaver()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(str(db_path), check_same_thread=False))


def run(request: str, deps: Deps, approve: Approver, db_path: Path | None = None,
        run_id: str | None = None) -> dict:
    """Execute one request. `approve` receives the interrupt payload and returns {approved, note}."""
    run_id = run_id or new_run_id()
    graph = build_graph(deps, make_checkpointer(db_path))
    config = {"configurable": {"thread_id": run_id}}
    deps.audit.record(run_id, "run_started", request=request)
    result = graph.invoke({"request": request, "run_id": run_id}, config)
    while result.get("__interrupt__"):
        payload = result["__interrupt__"][0].value
        result = graph.invoke(Command(resume=approve(payload)), config)
    deps.audit.record(run_id, "run_finished", status=result.get("status"), report=result.get("report_path"))
    return result
