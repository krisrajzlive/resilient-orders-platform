"""catalog_core: semantic product search and cited RAG answers, shared by both projects."""
from .search import CatalogSearch, SearchHit, SearchResult, fuse_rrf

__all__ = ["CatalogSearch", "SearchHit", "SearchResult", "fuse_rrf"]
