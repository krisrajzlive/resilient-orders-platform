"""Grounded answers over search results, with validated citations and model fallback."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence

from pydantic import BaseModel, Field

from .search import CatalogSearch, SearchHit

log = logging.getLogger(__name__)

_PROMPT = """You are a shopping assistant. Answer the customer's question using ONLY the products
listed below. If none fit, say so. Cite the product ids you relied on.

Products:
{context}

Question: {question}"""


class CatalogAnswer(BaseModel):
    answer: str = Field(description="Short helpful answer grounded in the listed products.")
    cited_product_ids: list[str] = Field(description="Ids of the products the answer relies on.")


@dataclass(frozen=True)
class AskResult:
    answer: str
    cited_product_ids: list[str]
    hits: list[SearchHit]
    model_used: str            # chat model id, or "extractive" when every model failed
    mode_served: str           # search mode that supplied the context
    degraded: bool


def _context(hits: Sequence[SearchHit]) -> str:
    return "\n".join(f"[{h.product_id}] {h.name} (${h.price:.2f}): {h.description}" for h in hits)


class CatalogRag:
    def __init__(self, search: CatalogSearch, chat_models: Sequence[tuple[str, object]], k: int = 5):
        self.search = search
        self.chat_models = list(chat_models)
        self.k = k

    def ask(self, question: str) -> AskResult:
        result = self.search.search(question, limit=self.k)
        if not result.hits:
            return AskResult("No matching products found.", [], [], "none", result.mode_served, result.degraded)

        prompt = _PROMPT.format(context=_context(result.hits), question=question)
        allowed = {h.product_id for h in result.hits}
        for model_id, model in self.chat_models:
            try:
                out: CatalogAnswer = model.with_structured_output(CatalogAnswer).invoke(prompt)
            except Exception as exc:
                log.warning("chat model %s failed (%s); trying next", model_id, type(exc).__name__)
                continue
            # Drop any citation the model invented: only retrieved ids are valid.
            cited = [pid for pid in out.cited_product_ids if pid in allowed]
            return AskResult(out.answer, cited, result.hits, model_id, result.mode_served, result.degraded)

        top = result.hits[0]
        return AskResult(f"Top match: {top.name} (${top.price:.2f}).", [top.product_id], result.hits,
                         "extractive", result.mode_served, True)
