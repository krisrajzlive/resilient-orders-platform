"""Deterministic bag-of-words hashing embedder for tests and offline runs.

Not semantic: it only captures word overlap. Used to test plumbing without AWS calls.
"""
from __future__ import annotations

import hashlib
import math
import re


class HashingEmbedder:
    def __init__(self, dimensions: int = 1024):
        self.dimensions = dimensions

    def embed_query(self, text: str) -> list[float]:
        vec = [0.0] * self.dimensions
        for token in re.findall(r"[a-z0-9]+", text.lower()):
            idx = int(hashlib.md5(token.encode()).hexdigest(), 16) % self.dimensions
            vec[idx] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_query(t) for t in texts]
