"""PostgreSQL + pgvector store: HNSW vector index and GIN full-text index on one table."""
from __future__ import annotations

import re
from typing import Iterable, Sequence

import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector

from .search import SearchHit

_SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS products (
    id          text PRIMARY KEY,
    name        text NOT NULL,
    category    text NOT NULL,
    description text NOT NULL,
    price       numeric(10,2) NOT NULL,
    embedding   vector({dim}),
    tsv         tsvector GENERATED ALWAYS AS (
                    to_tsvector('english', name || ' ' || category || ' ' || description)) STORED
);
CREATE INDEX IF NOT EXISTS products_tsv_idx ON products USING gin (tsv);
CREATE INDEX IF NOT EXISTS products_embedding_idx ON products USING hnsw (embedding vector_cosine_ops);
"""

_COLUMNS = "id, name, category, description, price"


def _hit(row) -> SearchHit:
    return SearchHit(row[0], row[1], row[2], row[3], float(row[4]), float(row[5]))


class PgVectorStore:
    def __init__(self, database_url: str, dimensions: int = 1024):
        self.url = database_url
        self.dimensions = dimensions

    def ensure_schema(self) -> None:
        with psycopg.connect(self.url, autocommit=True) as conn:
            conn.execute(_SCHEMA.format(dim=self.dimensions))

    def _conn(self) -> psycopg.Connection:
        conn = psycopg.connect(self.url, autocommit=True)
        register_vector(conn)
        return conn

    def upsert(self, products: Iterable[dict], embeddings: Sequence[Sequence[float]]) -> int:
        rows = [(p["id"], p["name"], p["category"], p["description"], p["price"], Vector(list(e)))
                for p, e in zip(products, embeddings, strict=True)]
        with self._conn() as conn, conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO products (id, name, category, description, price, embedding)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, category = EXCLUDED.category,
                       description = EXCLUDED.description, price = EXCLUDED.price,
                       embedding = EXCLUDED.embedding""", rows)
        return len(rows)

    def count(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT count(*) FROM products").fetchone()[0]

    def vector_search(self, embedding: Sequence[float], limit: int) -> list[SearchHit]:
        vec = Vector(list(embedding))
        with self._conn() as conn:
            rows = conn.execute(
                f"""SELECT {_COLUMNS}, 1 - (embedding <=> %s) AS score FROM products
                    WHERE embedding IS NOT NULL ORDER BY embedding <=> %s LIMIT %s""",
                (vec, vec, limit)).fetchall()
        return [_hit(r) for r in rows]

    def keyword_search(self, query: str, limit: int) -> list[SearchHit]:
        # OR the terms so paraphrased queries still match on any shared word.
        terms = re.findall(r"[a-z0-9]+", query.lower())
        if not terms:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                f"""SELECT {_COLUMNS}, ts_rank_cd(tsv, q) AS score
                    FROM products, to_tsquery('english', %s) q
                    WHERE tsv @@ q ORDER BY score DESC LIMIT %s""",
                (" | ".join(terms), limit)).fetchall()
        return [_hit(r) for r in rows]
