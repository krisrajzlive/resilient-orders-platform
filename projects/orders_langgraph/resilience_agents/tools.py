"""Tool registry: the only way an agent touches the outside world.

Three rules are enforced here, not left to prompts:
  1. an agent can call only the tools on its allowlist;
  2. a tool that changes state (`writes=True`) needs an explicit human approval flag;
  3. every call, denial and error is written to the audit log.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .audit import AuditLog


class ToolDenied(Exception):
    pass


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    fn: Callable[..., Any]
    writes: bool = False


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool], allowed: Iterable[str], audit: AuditLog,
                 run_id: str, agent: str) -> None:
        self._tools = {t.name: t for t in tools}
        self.allowed = frozenset(allowed)
        self.audit, self.run_id, self.agent = audit, run_id, agent

    def call(self, name: str, *, approved: bool = False, **kwargs) -> Any:
        tool = self._tools.get(name)
        if tool is None or name not in self.allowed:
            self.audit.record(self.run_id, "tool_denied", agent=self.agent, tool=name, reason="not on allowlist")
            raise ToolDenied(f"{self.agent} may not call {name!r}")
        if tool.writes and not approved:
            self.audit.record(self.run_id, "tool_denied", agent=self.agent, tool=name, reason="approval required")
            raise ToolDenied(f"{name!r} changes state and needs human approval")
        try:
            result = tool.fn(**kwargs)
        except Exception as exc:
            self.audit.record(self.run_id, "tool_error", agent=self.agent, tool=name, args=kwargs,
                              error=type(exc).__name__)
            raise
        self.audit.record(self.run_id, "tool_call", agent=self.agent, tool=name, args=kwargs,
                          writes=tool.writes, approved=approved if tool.writes else None)
        return result
