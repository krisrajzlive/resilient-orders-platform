"""Index product media and evaluate cross-modal retrieval.

  text -> image   26 paraphrased text queries against image / caption / fused vectors
  image -> image  32 perturbed catalog photos (rotated, blurred, recoloured, JPEG) against the index

The images are rendered product cards, so these numbers validate the pipeline and the
embedding behaviour; they are not a claim about real product photography.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..config import load_settings
from ..evaluate import DATA_DIR, load_catalog, load_queries, product_text, score
from .engines import _fuse_media
from .nova import NovaMultimodalEmbedder
from .store import MediaStore


def index_media(store: MediaStore, embedder: NovaMultimodalEmbedder, catalog: list[dict]) -> int:
    for p in catalog:
        path = DATA_DIR / "images" / "products" / f"{p['id']}.png"
        store.upsert(
            p["id"], str(path.relative_to(DATA_DIR.parent)),
            embedder.embed_image(path.read_bytes(), "png", "GENERIC_INDEX"),
            embedder.embed_text(product_text(p), "GENERIC_INDEX"))
    return len(catalog)


def evaluate_text_to_image(store: MediaStore, embedder: NovaMultimodalEmbedder, k: int = 5) -> dict:
    queries = load_queries()
    rows = {"image_only": [], "caption_only": [], "fused": []}
    for q in queries:
        vec = embedder.embed_text(q["query"])
        by_image, by_caption = store.search("image", vec, 20), store.search("caption", vec, 20)
        rankings = {"image_only": by_image, "caption_only": by_caption,
                    "fused": _fuse_media(by_image, by_caption, 10)}
        for name, hits in rankings.items():
            rows[name].append(score([h.product_id for h in hits], set(q["expected"]), k))
    return {name: {f"recall@{k}": sum(r for r, _ in v) / len(v), "mrr": sum(m for _, m in v) / len(v)}
            for name, v in rows.items()}


def evaluate_image_to_image(store: MediaStore, embedder: NovaMultimodalEmbedder, catalog: list[dict]) -> dict:
    r1, r5, rr = [], [], []
    for p in catalog:
        path = DATA_DIR / "images" / "queries" / f"{p['id']}.jpg"
        vec = embedder.embed_image(path.read_bytes(), "jpeg", "GENERIC_RETRIEVAL")
        ranked = [h.product_id for h in store.search("image", vec, 10)]
        a1, m = score(ranked, {p["id"]}, 1)
        a5, _ = score(ranked, {p["id"]}, 5)
        r1.append(a1), r5.append(a5), rr.append(m)
    n = len(catalog)
    return {"recall@1": sum(r1) / n, "recall@5": sum(r5) / n, "mrr": sum(rr) / n}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", help="write the report to this path")
    a = ap.parse_args()

    settings = load_settings()
    embedder = NovaMultimodalEmbedder(settings)
    store = MediaStore(settings.database_url, settings.embedding_dimensions)
    store.ensure_schema()
    catalog = load_catalog()
    n = index_media(store, embedder, catalog)

    t2i = evaluate_text_to_image(store, embedder)
    i2i = evaluate_image_to_image(store, embedder, catalog)
    print(f"indexed {n} products with Nova multimodal embeddings")
    print("\ntext -> image (26 queries)\n" + f"{'space':14} {'recall@5':>9} {'mrr':>7}")
    for name, m in t2i.items():
        print(f"{name:14} {m['recall@5']:>9.3f} {m['mrr']:>7.3f}")
    print("\nimage -> image (32 perturbed photos)")
    print(f"recall@1 {i2i['recall@1']:.3f}  recall@5 {i2i['recall@5']:.3f}  mrr {i2i['mrr']:.3f}")
    if a.json:
        Path(a.json).write_text(json.dumps({"text_to_image": t2i, "image_to_image": i2i}, indent=2),
                                encoding="utf-8")


if __name__ == "__main__":
    main()
