"""Retrieval evaluation: recall@k and MRR for vector, keyword and hybrid search."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import load_settings
from .hashing import HashingEmbedder
from .search import CatalogSearch
from .store import PgVectorStore

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def load_catalog() -> list[dict]:
    return json.loads((DATA_DIR / "catalog.json").read_text(encoding="utf-8"))


def load_queries() -> list[dict]:
    return json.loads((DATA_DIR / "eval_queries.json").read_text(encoding="utf-8"))


def product_text(p: dict) -> str:
    return f"{p['name']}. {p['category']}. {p['description']}"


def score(ranked_ids: list[str], expected: set[str], k: int) -> tuple[float, float]:
    """Return (recall@k, reciprocal rank of first relevant hit)."""
    top = ranked_ids[:k]
    recall = len(expected & set(top)) / len(expected)
    rr = next((1.0 / i for i, pid in enumerate(ranked_ids, start=1) if pid in expected), 0.0)
    return recall, rr


class _CachedEmbedder:
    """Embed each distinct query once, even though several modes use it."""
    def __init__(self, inner):
        self.inner, self.cache = inner, {}

    def embed_query(self, text):
        if text not in self.cache:
            self.cache[text] = self.inner.embed_query(text)
        return self.cache[text]


def evaluate(search: CatalogSearch, queries: list[dict], k: int = 5) -> dict[str, dict[str, float]]:
    report = {}
    for mode in ("keyword", "vector", "hybrid"):
        recalls, rrs = [], []
        for q in queries:
            ranked = [h.product_id for h in search.search(q["query"], mode=mode, limit=10).hits]
            r, rr = score(ranked, set(q["expected"]), k)
            recalls.append(r)
            rrs.append(rr)
        report[mode] = {f"recall@{k}": sum(recalls) / len(recalls), "mrr": sum(rrs) / len(rrs)}
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--embedder", choices=["bedrock", "hash"], default="bedrock")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--json", help="write the report to this path")
    a = ap.parse_args()

    settings = load_settings()
    if a.embedder == "bedrock":
        from .models import build_embeddings
        embedder = build_embeddings(settings)
    else:
        embedder = HashingEmbedder(settings.embedding_dimensions)

    store = PgVectorStore(settings.database_url, settings.embedding_dimensions)
    store.ensure_schema()
    catalog = load_catalog()
    store.upsert(catalog, embedder.embed_documents([product_text(p) for p in catalog]))

    report = evaluate(CatalogSearch(store, _CachedEmbedder(embedder)), load_queries(), a.k)
    print(f"embedder={a.embedder} products={len(catalog)} queries={len(load_queries())}")
    print(f"{'mode':10} {'recall@%d' % a.k:>10} {'mrr':>8}")
    for mode, m in report.items():
        print(f"{mode:10} {m[f'recall@{a.k}']:>10.3f} {m['mrr']:>8.3f}")
    if a.json:
        Path(a.json).write_text(json.dumps({"embedder": a.embedder, **report}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
