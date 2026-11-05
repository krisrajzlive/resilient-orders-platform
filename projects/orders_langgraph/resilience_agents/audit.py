"""Append-only audit trail: every tool call, approval and model choice in a run."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

_ACCOUNT = re.compile(r"\b\d{12}\b")


class AuditLog:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.events: list[dict] = []
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, run_id: str, kind: str, **fields) -> dict:
        event = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "runId": run_id, "kind": kind, **fields}
        self.events.append(event)
        if self.path:
            # Redact at write time: ARNs in tool arguments carry the account id, and these files are shareable.
            line = _ACCOUNT.sub("<account>", json.dumps(event, default=str))
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        return event
