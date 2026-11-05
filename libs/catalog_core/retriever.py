"""LangChain retriever adapter so the catalog plugs into any LangChain/LangGraph chain."""
from __future__ import annotations

from typing import Any

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever


class CatalogRetriever(BaseRetriever):
    search: Any            # CatalogSearch
    mode: str = "hybrid"
    k: int = 5

    def _get_relevant_documents(self, query: str, *, run_manager: CallbackManagerForRetrieverRun
                                ) -> list[Document]:
        result = self.search.search(query, mode=self.mode, limit=self.k)
        return [
            Document(
                page_content=f"{h.name}: {h.description}",
                metadata={"product_id": h.product_id, "category": h.category, "price": h.price,
                          "score": h.score, "mode_served": result.mode_served,
                          "degraded": result.degraded},
            )
            for h in result.hits
        ]
