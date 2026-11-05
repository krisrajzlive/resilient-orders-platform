"""Structured LLM calls over a model fallback chain, plus the output schemas agents use."""
from __future__ import annotations

import logging
from typing import Literal, Sequence

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class LLMUnavailable(Exception):
    pass


class Route(BaseModel):
    agent: Literal["chaos", "triage", "drift"] = Field(
        description="chaos: run failure experiments. triage: assess AWS events/alarms. drift: audit configuration.")
    reason: str


class ExperimentPlan(BaseModel):
    hypothesis: str = Field(description="One sentence: what should remain true under these failures.")
    scenarios: list[str] = Field(description="Scenario names chosen ONLY from the provided catalog, at most 5.")
    rationale: str


class TriageItem(BaseModel):
    event: str
    affected_resources: list[str]
    severity: Literal["info", "low", "medium", "high"]
    recommended_action: str


class TriageReport(BaseModel):
    summary: str
    items: list[TriageItem]


class Proposal(BaseModel):
    finding_id: str
    explanation: str = Field(description="Why it matters for this system, in one or two sentences.")
    cdk_change: str = Field(description="A short Python CDK snippet or instruction that fixes it.")


class DriftReport(BaseModel):
    summary: str
    proposals: list[Proposal]


class Narrative(BaseModel):
    summary: str
    recommendations: list[str]


class StructuredLLM:
    """Try each chat model in order; remember which one answered."""

    def __init__(self, chain: Sequence[tuple[str, object]]) -> None:
        self.chain = list(chain)

    def invoke(self, schema: type[BaseModel], prompt: str) -> tuple[BaseModel, str]:
        errors = []
        for model_id, model in self.chain:
            try:
                return model.with_structured_output(schema).invoke(prompt), model_id
            except Exception as exc:
                log.warning("model %s failed: %s", model_id, type(exc).__name__)
                errors.append(f"{model_id}:{type(exc).__name__}")
        raise LLMUnavailable("; ".join(errors) or "no models configured")
