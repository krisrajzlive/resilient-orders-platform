"""pgvector table for product media: one image vector and one caption vector per product."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector

_SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS product_media (
    product_id        text PRIMARY KEY,
    image_path        text NOT NULL,
    image_embedding   vector({dim}) NOT NULL,
    caption_embedding vector({dim}) NOT NULL
);
CREATE INDEX IF NOT EXISTS product_media_image_idx
    ON product_media USING hnsw (image_embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS product_media_caption_idx
    ON product_media USING hnsw (caption_embedding vector_cosine_ops);
"""

_COLUMNS = {"image": "image_embedding", "caption": "caption_embedding"}


@dataclass(frozen=True)
class MediaHit:
    product_id: str
    image_path: str
    name: str
    description: str
    score: float
    price: float = 0.0


class MediaStore:
    def __init__(self, database_url: str, dimensions: int = 1024):
        self.url, self.dimensions = database_url, dimensions

    def ensure_schema(self) -> None:
        with psycopg.connect(self.url, autocommit=True) as conn:
            conn.execute(_SCHEMA.format(dim=self.dimensions))

    def _conn(self) -> psycopg.Connection:
        conn = psycopg.connect(self.url, autocommit=True)
        register_vector(conn)
        return conn

    def upsert(self, product_id: str, image_path: str, image_vec: Sequence[float],
               caption_vec: Sequence[float]) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO product_media VALUES (%s, %s, %s, %s)
                   ON CONFLICT (product_id) DO UPDATE SET image_path = EXCLUDED.image_path,
                       image_embedding = EXCLUDED.image_embedding,
                       caption_embedding = EXCLUDED.caption_embedding""",
                (product_id, image_path, Vector(list(image_vec)), Vector(list(caption_vec))))

    def search(self, space: str, embedding: Sequence[float], limit: int) -> list[MediaHit]:
        """space: 'image' compares against image vectors, 'caption' against caption vectors."""
        col = _COLUMNS[space]
        vec = Vector(list(embedding))
        with self._conn() as conn:
            rows = conn.execute(
                f"""SELECT m.product_id, m.image_path, COALESCE(p.name, ''), COALESCE(p.description, ''),
                           1 - (m.{col} <=> %s) AS score, COALESCE(p.price, 0)
                    FROM product_media m LEFT JOIN products p ON p.id = m.product_id
                    ORDER BY m.{col} <=> %s LIMIT %s""", (vec, vec, limit)).fetchall()
        return [MediaHit(r[0], r[1], r[2], r[3], float(r[4]), float(r[5])) for r in rows]
