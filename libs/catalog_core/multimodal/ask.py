"""Ask the multimodal router a question, optionally with a photo.

  python -m catalog_core.multimodal.ask "waterproof boots for muddy trails"
  python -m catalog_core.multimodal.ask "find something like this" --image data/images/queries/P027.jpg
"""
from __future__ import annotations

import argparse

from ..config import load_settings
from ..models import build_embeddings
from ..search import CatalogSearch
from ..store import PgVectorStore
from .engines import build_router, make_query, pick_llm
from .nova import NovaMultimodalEmbedder
from .store import MediaStore


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("question")
    ap.add_argument("--image", help="path to a query photo")
    a = ap.parse_args()

    s = load_settings()
    model_id, llm = pick_llm(s)
    router = build_router(
        CatalogSearch(PgVectorStore(s.database_url, s.embedding_dimensions), build_embeddings(s)),
        MediaStore(s.database_url, s.embedding_dimensions), NovaMultimodalEmbedder(s), llm)

    response = router.query(make_query(a.question, a.image))
    selected = response.metadata.get("selector_result") if response.metadata else None
    print(f"model: {model_id}")
    print(f"router selected: {selected}")
    print(f"\n{response}")


if __name__ == "__main__":
    main()
