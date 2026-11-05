"""Unit tests for the multimodal retriever with fake embedder and store. No AWS or database."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from llama_index.core import QueryBundle

from catalog_core.multimodal.engines import VisualRetriever, _fuse_media
from catalog_core.multimodal.nova import NovaMultimodalEmbedder
from catalog_core.multimodal.store import MediaHit

ROOT = Path(__file__).resolve().parents[1]


def mh(pid: str, score: float = 1.0) -> MediaHit:
    return MediaHit(pid, f"data/images/products/{pid}.png", f"name-{pid}", "desc", score)


class FakeMediaStore:
    def __init__(self):
        self.calls = []

    def search(self, space, embedding, limit):
        self.calls.append((space, limit))
        return {"image": [mh("A"), mh("B")], "caption": [mh("B"), mh("C")]}[space][:limit]


class FakeEmbedder:
    def __init__(self):
        self.seen = []

    def embed_text(self, text, purpose="GENERIC_RETRIEVAL"):
        self.seen.append(("text", purpose))
        return [0.0]

    def embed_image(self, data, fmt="png", purpose="GENERIC_INDEX"):
        self.seen.append(("image", purpose, fmt))
        return [0.0]


def test_text_query_searches_both_spaces_and_fuses():
    store, emb = FakeMediaStore(), FakeEmbedder()
    nodes = VisualRetriever(store, emb, k=3).retrieve(QueryBundle("something blue"))
    assert {s for s, _ in store.calls} == {"image", "caption"}
    assert nodes[0].node.metadata["product_id"] == "B"      # found in both lists
    assert emb.seen == [("text", "GENERIC_RETRIEVAL")]


def test_image_query_uses_image_space_only(tmp_path):
    photo = tmp_path / "q.jpg"
    photo.write_bytes(b"not-a-real-jpeg")
    store, emb = FakeMediaStore(), FakeEmbedder()
    nodes = VisualRetriever(store, emb, k=2).retrieve(QueryBundle("look-alike", image_path=str(photo)))
    assert [s for s, _ in store.calls] == ["image"]
    assert emb.seen == [("image", "GENERIC_RETRIEVAL", "jpeg")]
    assert [n.node.metadata["product_id"] for n in nodes] == ["A", "B"]


def test_fuse_keeps_image_paths():
    fused = _fuse_media([mh("A")], [mh("A"), mh("C")], limit=5)
    assert {h.product_id: h.image_path for h in fused}["C"].endswith("C.png")


def test_nova_request_shape():
    sent = {}

    class Client:
        def invoke_model(self, modelId, body):
            sent.update(model=modelId, body=json.loads(body))

            class Resp(dict):
                pass
            import io
            return {"body": io.BytesIO(json.dumps({"embeddings": [{"embedding": [0.5] * 4}]}).encode())}

    class S:
        embedding_dimensions, profile, region = 4, None, "us-east-1"

    emb = NovaMultimodalEmbedder(S(), client=Client())
    assert emb.embed_text("hi") == [0.5] * 4
    params = sent["body"]["singleEmbeddingParams"]
    assert params["embeddingPurpose"] == "GENERIC_RETRIEVAL" and params["embeddingDimension"] == 4
    emb.embed_image(b"x", "png", "GENERIC_INDEX")
    assert "image" in sent["body"]["singleEmbeddingParams"]


def test_every_catalog_item_has_both_images():
    catalog = json.loads((ROOT / "data" / "catalog.json").read_text(encoding="utf-8"))
    for p in catalog:
        assert (ROOT / "data" / "images" / "products" / f"{p['id']}.png").exists()
        assert (ROOT / "data" / "images" / "queries" / f"{p['id']}.jpg").exists()
