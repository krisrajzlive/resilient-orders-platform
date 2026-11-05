"""Catalog API: search, grounded answers and photo search over catalog_core.

Chaos switch (local development only, disabled with CHAOS_ENABLED=0): send the header
  X-Chaos: embedding-down | db-down | llm-down
to force the matching dependency to fail and watch the graceful-degradation paths serve.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from fastapi import FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from catalog_core.config import Settings, load_settings
from catalog_core.rag import CatalogRag
from catalog_core.search import CatalogSearch, SearchHit

ROOT = Path(__file__).resolve().parents[2]
POPULAR_IDS = ["P013", "P001", "P003", "P010", "P027"]


class Camel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class HitOut(Camel):
    product_id: str
    name: str
    category: str
    description: str
    price: float
    score: float


class SearchOut(Camel):
    hits: list[HitOut]
    mode_served: str
    degraded: bool
    reason: str | None = None


class AskIn(Camel):
    question: str


class AskOut(Camel):
    answer: str
    cited_product_ids: list[str]
    hits: list[HitOut]
    model_used: str
    mode_served: str
    degraded: bool


class VisualHitOut(Camel):
    product_id: str
    name: str
    price: float
    score: float


def _hit(h: SearchHit) -> HitOut:
    return HitOut(product_id=h.product_id, name=h.name, category=h.category,
                  description=h.description, price=h.price, score=h.score)


class _FailingEmbedder:
    def embed_query(self, text):
        raise RuntimeError("chaos: embedding-down")


class _FailingStore:
    def vector_search(self, embedding, limit):
        raise RuntimeError("chaos: db-down")

    def keyword_search(self, query, limit):
        raise RuntimeError("chaos: db-down")


class _FailingChat:
    def with_structured_output(self, schema):
        return self

    def invoke(self, prompt):
        raise RuntimeError("chaos: llm-down")


@dataclass
class Deps:
    """Lazily-built real dependencies; tests pass their own."""
    settings: Settings

    @cached_property
    def store(self):
        from catalog_core.store import PgVectorStore
        return PgVectorStore(self.settings.database_url, self.settings.embedding_dimensions)

    @cached_property
    def embedder(self):
        from catalog_core.models import build_embeddings
        return build_embeddings(self.settings)

    @cached_property
    def chat_models(self):
        from catalog_core.models import build_chat_models
        return build_chat_models(self.settings)

    @cached_property
    def popular(self) -> list[SearchHit]:
        catalog = {p["id"]: p for p in json.loads((ROOT / "data" / "catalog.json").read_text("utf-8"))}
        return [SearchHit(i, catalog[i]["name"], catalog[i]["category"], catalog[i]["description"],
                          catalog[i]["price"], 0.0) for i in POPULAR_IDS if i in catalog]

    @cached_property
    def media_store(self):
        from catalog_core.multimodal.store import MediaStore
        return MediaStore(self.settings.database_url, self.settings.embedding_dimensions)

    @cached_property
    def nova(self):
        from catalog_core.multimodal.nova import NovaMultimodalEmbedder
        return NovaMultimodalEmbedder(self.settings)


def create_app(deps: Deps | None = None) -> FastAPI:
    deps = deps or Deps(load_settings())
    chaos_enabled = os.environ.get("CHAOS_ENABLED", "1") != "0"
    app = FastAPI(title="Catalog API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"], allow_headers=["*"])

    def chaos_of(value: str | None) -> str | None:
        return value if chaos_enabled and value in {"embedding-down", "db-down", "llm-down"} else None

    def search_for(chaos: str | None) -> CatalogSearch:
        embedder = _FailingEmbedder() if chaos == "embedding-down" else deps.embedder
        store = _FailingStore() if chaos == "db-down" else deps.store
        return CatalogSearch(store, embedder, popular=deps.popular)

    @app.get("/api/health")
    def health():
        try:
            return {"status": "ok", "products": deps.store.count(), "chaosEnabled": chaos_enabled}
        except Exception as exc:
            return {"status": "degraded", "error": type(exc).__name__, "chaosEnabled": chaos_enabled}

    @app.get("/api/search", response_model=SearchOut, response_model_by_alias=True)
    def search(q: str = Query(min_length=1, max_length=300), mode: str = "hybrid",
               limit: int = Query(5, ge=1, le=20), x_chaos: str | None = Header(None)):
        if mode not in {"hybrid", "vector", "keyword"}:
            raise HTTPException(422, "mode must be hybrid, vector or keyword")
        r = search_for(chaos_of(x_chaos)).search(q, mode=mode, limit=limit)
        return SearchOut(hits=[_hit(h) for h in r.hits], mode_served=r.mode_served,
                         degraded=r.degraded, reason=r.reason)

    @app.post("/api/ask", response_model=AskOut, response_model_by_alias=True)
    def ask(body: AskIn, x_chaos: str | None = Header(None)):
        chaos = chaos_of(x_chaos)
        chats = [("chaos", _FailingChat())] if chaos == "llm-down" else deps.chat_models
        r = CatalogRag(search_for(chaos), chats).ask(body.question)
        return AskOut(answer=r.answer, cited_product_ids=r.cited_product_ids,
                      hits=[_hit(h) for h in r.hits], model_used=r.model_used,
                      mode_served=r.mode_served, degraded=r.degraded or chaos == "llm-down")

    @app.post("/api/visual-search", response_model=list[VisualHitOut], response_model_by_alias=True)
    async def visual_search(image: UploadFile = File(...), x_chaos: str | None = Header(None)):
        data = await image.read()
        if not data or len(data) > 5_000_000:
            raise HTTPException(413, "image must be between 1 byte and 5 MB")
        fmt = "png" if (image.content_type or "").endswith("png") else "jpeg"
        try:
            if chaos_of(x_chaos) == "embedding-down":
                raise RuntimeError("chaos: embedding-down")
            vec = deps.nova.embed_image(data, fmt, "GENERIC_RETRIEVAL")
            hits = deps.media_store.search("image", vec, 5)
        except Exception as exc:
            raise HTTPException(503, f"visual search unavailable: {type(exc).__name__}") from exc
        return [VisualHitOut(product_id=h.product_id, name=h.name, price=h.price, score=h.score) for h in hits]

    return app


app = create_app()
