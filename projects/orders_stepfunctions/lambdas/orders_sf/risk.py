"""AI risk assessment (LangChain on Bedrock) with deterministic rules as guardrail and fallback.

Final level is the stricter of the model's judgement and the rules, so a model can raise
scrutiny but never lower it below what the rules require. If every model fails, the rules
alone decide and the result says so.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Literal, Sequence

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

HIGH_VALUE_THRESHOLD = 5000.0
MEDIUM_VALUE_THRESHOLD = 1000.0
ORDER = {"low": 0, "medium": 1, "high": 2}


class RiskAssessment(BaseModel):
    level: Literal["low", "medium", "high"] = Field(description="Overall order risk.")
    reasons: list[str] = Field(description="Short factual reasons, at most three.")


def rules_assessment(order: dict) -> RiskAssessment:
    amount = float(order.get("amount", 0))
    items = order.get("items") or []
    reasons, level = [], "low"
    if amount >= HIGH_VALUE_THRESHOLD:
        level, reasons = "high", [f"order value {amount:.2f} at or above {HIGH_VALUE_THRESHOLD:.0f}"]
    elif amount >= MEDIUM_VALUE_THRESHOLD:
        level, reasons = "medium", [f"order value {amount:.2f} at or above {MEDIUM_VALUE_THRESHOLD:.0f}"]
    if sum(int(i.get("qty", 1)) for i in items) >= 20:
        level = max(level, "medium", key=ORDER.get)
        reasons.append("unusually large quantity")
    return RiskAssessment(level=level, reasons=reasons or ["within normal limits"])


_PROMPT = """You review e-commerce orders for fraud or abuse risk. Judge only from the facts below.
Respond with a level (low, medium or high) and up to three short factual reasons.

Order: {order}"""


def assess(order: dict, models: Sequence[tuple[str, object]]) -> dict:
    rules = rules_assessment(order)
    prompt = _PROMPT.format(order=json.dumps({k: order.get(k) for k in ("customerId", "amount", "items")}))
    for model_id, model in models:
        try:
            out: RiskAssessment = model.with_structured_output(RiskAssessment).invoke(prompt)
        except Exception as exc:  # model unavailable or malformed output: try the next one
            log.warning("risk model %s failed: %s", model_id, type(exc).__name__)
            continue
        level = max(out.level, rules.level, key=ORDER.get)
        reasons = list(dict.fromkeys(rules.reasons + out.reasons))[:4]
        return {"level": level, "reasons": reasons, "source": f"model:{model_id}"}
    return {"level": rules.level, "reasons": rules.reasons, "source": "rules-fallback"}


def default_models() -> list[tuple[str, object]]:
    """Fallback chain from RISK_MODEL_IDS (comma separated). Built lazily inside the Lambda."""
    from langchain_aws import ChatBedrockConverse
    ids = [m.strip() for m in os.environ.get(
        "RISK_MODEL_IDS", "us.anthropic.claude-haiku-4-5-20251001-v1:0,amazon.nova-lite-v1:0").split(",") if m.strip()]
    region = os.environ.get("AWS_REGION", "us-east-1")
    return [(m, ChatBedrockConverse(model=m, region_name=region, temperature=0, max_tokens=300)) for m in ids]
