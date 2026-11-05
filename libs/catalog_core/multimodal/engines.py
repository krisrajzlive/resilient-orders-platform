"""LlamaIndex query engines over the catalog.

Three retrievers, one router:
  HybridTextRetriever   wraps catalog_core's hybrid text search (shared with the LangChain path)
  VisualRetriever       text -> image (cross-modal) or image -> image search in Nova's shared space
  Router (multi-select) an LLM decides which engine(s) fit a request; several can answer and
                        their answers are summarised together.
"""
from __future__ import annotations

import logging
from pathlib import Path

from llama_index.core import PromptTemplate, QueryBundle
from llama_index.core.base.base_retriever import BaseRetriever
from llama_index.core.query_engine import RetrieverQueryEngine, RouterQueryEngine
from llama_index.core.response_synthesizers import ResponseMode, get_response_synthesizer
from llama_index.core.schema import ImageNode, NodeWithScore, TextNode
from llama_index.core.selectors import LLMMultiSelector
from llama_index.core.tools import QueryEngineTool

from ..search import CatalogSearch, fuse_rrf
from .nova import NovaMultimodalEmbedder
from .store import MediaHit, MediaStore

log = logging.getLogger(__name__)


class HybridTextRetriever(BaseRetriever):
    """Hybrid (vector + keyword) text search exposed as a LlamaIndex retriever."""

    def __init__(self, search: CatalogSearch, k: int = 5):
        super().__init__()
        self._search, self._k = search, k

    def _retrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        result = self._search.search(query_bundle.query_str, limit=self._k)
        return [
            NodeWithScore(
                node=TextNode(
                    id_=h.product_id,
                    text=f"[{h.product_id}] {h.name} (${h.price:.2f}): {h.description}",
                    metadata={"product_id": h.product_id, "mode_served": result.mode_served}),
                score=h.score)
            for h in result.hits
        ]


class VisualRetriever(BaseRetriever):
    """Cross-modal retrieval. A photo searches image vectors; words search image + caption vectors."""

    def __init__(self, store: MediaStore, embedder: NovaMultimodalEmbedder, k: int = 5, pool: int = 20):
        super().__init__()
        self._store, self._embedder, self._k, self._pool = store, embedder, k, pool

    def _retrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        if query_bundle.image_path:
            path = Path(query_bundle.image_path)
            vec = self._embedder.embed_image(
                path.read_bytes(), fmt="jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "png",
                purpose="GENERIC_RETRIEVAL")
            hits = self._store.search("image", vec, self._k)
        else:
            vec = self._embedder.embed_text(query_bundle.query_str)
            by_image = self._store.search("image", vec, self._pool)
            by_caption = self._store.search("caption", vec, self._pool)
            hits = _fuse_media(by_image, by_caption, self._k)
        return [_to_node(h) for h in hits]


def _fuse_media(a: list[MediaHit], b: list[MediaHit], limit: int) -> list[MediaHit]:
    # Reuse catalog_core's rank fusion by adapting MediaHit to the shared hit shape.
    from ..search import SearchHit
    adapt = lambda hs: [SearchHit(h.product_id, h.name, "", h.description, 0.0, h.score) for h in hs]  # noqa: E731
    paths = {h.product_id: h.image_path for h in a + b}
    prices = {h.product_id: h.price for h in a + b}
    return [MediaHit(f.product_id, paths[f.product_id], f.name, f.description, f.score, prices[f.product_id])
            for f in fuse_rrf([adapt(a), adapt(b)], limit=limit)]


def _to_node(h: MediaHit) -> NodeWithScore:
    return NodeWithScore(
        node=ImageNode(id_=h.product_id, image_path=h.image_path,
                       text=(f"[{h.product_id}] {h.name} (${h.price:.2f}) "
                             f"visual similarity {h.score:.2f}: {h.description}"),
                       metadata={"product_id": h.product_id}),
        score=h.score)


def pick_llm(settings, models: list[str] | None = None):
    """Return the first Bedrock chat model in the fallback chain that answers right now."""
    from llama_index.llms.bedrock_converse import BedrockConverse
    errors = []
    for model_id in models or list(settings.chat_model_ids):
        llm = BedrockConverse(model=model_id, region_name=settings.region,
                              profile_name=settings.profile, temperature=0.0, max_tokens=700)
        try:
            llm.complete("Say OK")
            log.info("LlamaIndex LLM: %s", model_id)
            return model_id, llm
        except Exception as exc:  # model unavailable: try the next in the chain
            errors.append(f"{model_id}: {type(exc).__name__}")
    raise RuntimeError("no chat model answered: " + "; ".join(errors))


PHOTO_MARKER = "[customer attached a photo]"

_VISUAL_QA = PromptTemplate(
    "The products below were found by visual similarity search, from the customer's request "
    "and any photo they attached, each with a visual similarity score between 0 and 1. The "
    "highest score is the best match: name it first with its id and price, and never prefer a "
    "lower-scored product on the basis of its description. Mention one alternative only if its "
    "score is above 0.5. "
    "Never say you cannot see an image: the search already compared it for you.\n\n"
    "Products:\n{context_str}\n\nRequest: {query_str}\nAnswer:")


def make_query(question: str, image_path: str | None = None) -> QueryBundle:
    """Build a query bundle; the marker makes an attached photo visible to the LLM router."""
    if image_path:
        return QueryBundle(f"{PHOTO_MARKER} {question}", image_path=image_path)
    return QueryBundle(question)


def build_router(search: CatalogSearch, store: MediaStore, embedder: NovaMultimodalEmbedder, llm,
                 k: int = 5) -> RouterQueryEngine:
    synth = lambda **kw: get_response_synthesizer(  # noqa: E731
        llm=llm, response_mode=ResponseMode.COMPACT, **kw)
    text_engine = RetrieverQueryEngine(HybridTextRetriever(search, k), response_synthesizer=synth())
    visual_engine = RetrieverQueryEngine(
        VisualRetriever(store, embedder, k),
        response_synthesizer=synth(text_qa_template=_VISUAL_QA))
    tools = [
        QueryEngineTool.from_defaults(
            query_engine=text_engine, name="text_search",
            description="Answers shopping questions described in words: needs, uses, features, budgets. "
                        f"Do NOT select it when the request is only to match an attached photo "
                        f"({PHOTO_MARKER}) with no needs described in words."),
        QueryEngineTool.from_defaults(
            query_engine=visual_engine, name="visual_search",
            description=f"Finds products by appearance. ALWAYS select it when the query contains "
                        f"{PHOTO_MARKER}, and when the user asks for look-alikes or describes "
                        f"colour, shape or style."),
    ]
    return RouterQueryEngine(
        llm=llm,
        selector=LLMMultiSelector.from_defaults(llm=llm),
        query_engine_tools=tools,
        summarizer=get_response_synthesizer(llm=llm, response_mode=ResponseMode.TREE_SUMMARIZE),
    )
