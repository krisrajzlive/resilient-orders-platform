"""Unit tests for fusion, degradation and RAG fallbacks. No AWS or database needed."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from catalog_core.evaluate import score
from catalog_core.rag import CatalogAnswer, CatalogRag
from catalog_core.search import CatalogSearch, SearchHit, fuse_rrf

DATA = Path(__file__).resolve().parents[1] / "data"


def hit(pid: str, score_: float = 1.0) -> SearchHit:
    return SearchHit(pid, f"name-{pid}", "cat", "desc", 10.0, score_)


class FakeStore:
    def __init__(self, vector=None, keyword=None, vector_error=None, keyword_error=None):
        self.vector, self.keyword = vector or [], keyword or []
        self.vector_error, self.keyword_error = vector_error, keyword_error

    def vector_search(self, embedding, limit):
        if self.vector_error:
            raise self.vector_error
        return self.vector[:limit]

    def keyword_search(self, query, limit):
        if self.keyword_error:
            raise self.keyword_error
        return self.keyword[:limit]


class GoodEmbedder:
    def embed_query(self, text):
        return [0.1, 0.2]


class DownEmbedder:
    def embed_query(self, text):
        raise RuntimeError("bedrock unavailable")


def test_fusion_prefers_items_found_by_both_lists():
    fused = fuse_rrf([[hit("A"), hit("B")], [hit("C"), hit("B")]], limit=3)
    assert fused[0].product_id == "B"
    assert {h.product_id for h in fused} == {"A", "B", "C"}


def test_hybrid_serves_fused_results():
    s = CatalogSearch(FakeStore(vector=[hit("A")], keyword=[hit("B")]), GoodEmbedder())
    r = s.search("q")
    assert r.mode_served == "hybrid" and not r.degraded


def test_embedding_outage_degrades_to_keyword():
    s = CatalogSearch(FakeStore(keyword=[hit("B")]), DownEmbedder())
    r = s.search("q")
    assert (r.mode_served, r.degraded) == ("keyword", True)
    assert r.reason.startswith("vector_unavailable")
    assert [h.product_id for h in r.hits] == ["B"]


def test_total_outage_serves_cached_popular_items():
    s = CatalogSearch(FakeStore(vector_error=RuntimeError("x"), keyword_error=RuntimeError("y")),
                      GoodEmbedder(), popular=[hit("TOP")])
    r = s.search("q")
    assert (r.mode_served, r.degraded) == ("cached", True)
    assert r.hits[0].product_id == "TOP"


def test_keyword_leg_failure_serves_vector_only():
    s = CatalogSearch(FakeStore(vector=[hit("A")], keyword_error=RuntimeError("x")), GoodEmbedder())
    r = s.search("q")
    assert (r.mode_served, r.degraded) == ("vector", True)


def test_unknown_mode_rejected():
    with pytest.raises(ValueError):
        CatalogSearch(FakeStore(), GoodEmbedder()).search("q", mode="magic")


class FakeChat:
    def __init__(self, answer=None, error=None):
        self.answer, self.error = answer, error

    def with_structured_output(self, schema):
        return self

    def invoke(self, prompt):
        if self.error:
            raise self.error
        return self.answer


def make_rag(chats):
    search = CatalogSearch(FakeStore(vector=[hit("A"), hit("B")], keyword=[hit("A")]), GoodEmbedder())
    return CatalogRag(search, chats)


def test_rag_falls_back_to_next_model_and_drops_invented_citations():
    answer = CatalogAnswer(answer="Take A.", cited_product_ids=["A", "ZZZ"])
    rag = make_rag([("primary", FakeChat(error=RuntimeError("throttled"))), ("backup", FakeChat(answer))])
    r = rag.ask("q")
    assert r.model_used == "backup"
    assert r.cited_product_ids == ["A"]          # invented id removed


def test_rag_extractive_fallback_when_all_models_fail():
    rag = make_rag([("m1", FakeChat(error=RuntimeError("x"))), ("m2", FakeChat(error=RuntimeError("y")))])
    r = rag.ask("q")
    assert r.model_used == "extractive" and r.degraded and r.cited_product_ids == ["A"]


def test_score_metrics():
    assert score(["X", "P1", "Y"], {"P1", "P2"}, k=3) == (0.5, 0.5)
    assert score(["X"], {"P1"}, k=3) == (0.0, 0.0)


def test_dataset_integrity():
    catalog = json.loads((DATA / "catalog.json").read_text(encoding="utf-8"))
    ids = [p["id"] for p in catalog]
    assert len(ids) == len(set(ids))
    for q in json.loads((DATA / "eval_queries.json").read_text(encoding="utf-8")):
        assert set(q["expected"]) <= set(ids), q
