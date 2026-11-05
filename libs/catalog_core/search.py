"""Hybrid catalog search with graceful degradation.

Serving order: hybrid (vector + keyword, fused) -> keyword only -> cached popular items.
Every result says which mode actually served it, so callers and chaos experiments can
verify the fallback path worked.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Protocol, Sequence

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchHit:
    product_id: str
    name: str
    category: str
    description: str
    price: float
    score: float


@dataclass(frozen=True)
class SearchResult:
    hits: list[SearchHit]
    mode_served: str          # hybrid | vector | keyword | cached
    degraded: bool
    reason: str | None = None


class Embedder(Protocol):
    def embed_query(self, text: str) -> list[float]: ...


class Store(Protocol):
    def vector_search(self, embedding: Sequence[float], limit: int) -> list[SearchHit]: ...
    def keyword_search(self, query: str, limit: int) -> list[SearchHit]: ...


def fuse_rrf(rankings: Sequence[Sequence[SearchHit]], k: int = 60, limit: int = 10) -> list[SearchHit]:
    """Reciprocal rank fusion: merge ranked lists without comparing incompatible scores."""
    scores: dict[str, float] = {}
    first: dict[str, SearchHit] = {}
    for ranking in rankings:
        for rank, hit in enumerate(ranking, start=1):
            scores[hit.product_id] = scores.get(hit.product_id, 0.0) + 1.0 / (k + rank)
            first.setdefault(hit.product_id, hit)
    ordered = sorted(scores, key=lambda pid: scores[pid], reverse=True)[:limit]
    return [replace(first[pid], score=scores[pid]) for pid in ordered]


class CatalogSearch:
    def __init__(self, store: Store, embedder: Embedder, popular: list[SearchHit] | None = None,
                 candidate_pool: int = 20):
        self.store = store
        self.embedder = embedder
        self.popular = popular or []
        self.candidate_pool = candidate_pool

    def search(self, query: str, mode: str = "hybrid", limit: int = 5) -> SearchResult:
        if mode not in {"hybrid", "vector", "keyword"}:
            raise ValueError(f"unknown mode {mode!r}")

        if mode == "keyword":
            return SearchResult(self.store.keyword_search(query, limit), "keyword", degraded=False)

        try:
            embedding = self.embedder.embed_query(query)
            vector_hits = self.store.vector_search(embedding, self.candidate_pool)
        except Exception as exc:  # embedding model or vector backend unavailable
            log.warning("vector path failed (%s); falling back to keyword", type(exc).__name__)
            return self._keyword_fallback(query, limit, reason=f"vector_unavailable:{type(exc).__name__}")

        if mode == "vector":
            return SearchResult(vector_hits[:limit], "vector", degraded=False)

        try:
            keyword_hits = self.store.keyword_search(query, self.candidate_pool)
        except Exception as exc:
            log.warning("keyword leg failed (%s); serving vector only", type(exc).__name__)
            return SearchResult(vector_hits[:limit], "vector", degraded=True,
                                reason=f"keyword_unavailable:{type(exc).__name__}")
        return SearchResult(fuse_rrf([vector_hits, keyword_hits], limit=limit), "hybrid", degraded=False)

    def _keyword_fallback(self, query: str, limit: int, reason: str) -> SearchResult:
        try:
            return SearchResult(self.store.keyword_search(query, limit), "keyword", True, reason)
        except Exception as exc:
            log.error("keyword fallback failed (%s); serving cached popular items", type(exc).__name__)
            return SearchResult(self.popular[:limit], "cached", True, f"{reason}|keyword_unavailable")
