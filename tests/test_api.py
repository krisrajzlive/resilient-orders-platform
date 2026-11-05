"""API tests with injected fakes: contract shape, validation and chaos switch."""
from __future__ import annotations

from fastapi.testclient import TestClient

from catalog_core.search import SearchHit
from services.catalog_api.app import Deps, create_app


def hit(pid):
    return SearchHit(pid, f"name-{pid}", "cat", "desc", 9.5, 0.9)


class FakeStore:
    def vector_search(self, e, limit):
        return [hit("A"), hit("B")][:limit]

    def keyword_search(self, q, limit):
        return [hit("B")][:limit]

    def count(self):
        return 2


class FakeEmbedder:
    def embed_query(self, t):
        return [0.1]


class FakeChat:
    def with_structured_output(self, schema):
        return self

    def invoke(self, prompt):
        from catalog_core.rag import CatalogAnswer
        return CatalogAnswer(answer="Pick A.", cited_product_ids=["A", "ghost"])


class FakeDeps(Deps):
    store = FakeStore()                      # type: ignore[assignment]
    embedder = FakeEmbedder()                # type: ignore[assignment]
    chat_models = [("fake-model", FakeChat())]  # type: ignore[assignment]
    popular = [hit("TOP")]                   # type: ignore[assignment]


def client():
    return TestClient(create_app(FakeDeps(settings=None)))


def test_search_contract_is_camel_case():
    body = client().get("/api/search", params={"q": "boots"}).json()
    assert body["modeServed"] == "hybrid" and body["degraded"] is False
    assert body["hits"][0]["productId"] in {"A", "B"}


def test_embedding_chaos_degrades_to_keyword():
    body = client().get("/api/search", params={"q": "boots"}, headers={"X-Chaos": "embedding-down"}).json()
    assert (body["modeServed"], body["degraded"]) == ("keyword", True)


def test_db_chaos_serves_cached_popular():
    body = client().get("/api/search", params={"q": "boots"}, headers={"X-Chaos": "db-down"}).json()
    assert body["modeServed"] == "cached" and body["hits"][0]["productId"] == "TOP"


def test_ask_drops_invented_citations_and_llm_chaos_goes_extractive():
    c = client()
    ok = c.post("/api/ask", json={"question": "what?"}).json()
    assert ok["modelUsed"] == "fake-model" and ok["citedProductIds"] == ["A"]
    down = c.post("/api/ask", json={"question": "what?"}, headers={"X-Chaos": "llm-down"}).json()
    assert down["modelUsed"] == "extractive" and down["degraded"] is True


def test_validation_and_health():
    c = client()
    assert c.get("/api/search", params={"q": "x", "mode": "magic"}).status_code == 422
    assert c.get("/api/search").status_code == 422
    assert c.get("/api/health").json()["products"] == 2
