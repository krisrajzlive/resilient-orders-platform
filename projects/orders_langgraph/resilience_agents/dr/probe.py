"""PostgreSQL probe used by the drills: numbered writes, row checks and a vector self-search."""
from __future__ import annotations

from typing import Callable

import psycopg

MARKER_TABLE = "drill_markers"


class Credentials:
    """Database credentials held in memory only. Never logged and never part of a report."""

    def __init__(self, user: str, password: str, dbname: str) -> None:
        self.user, self.password, self.dbname = user, password, dbname

    def __repr__(self) -> str:             # keep the password out of any accidental print or traceback
        return f"Credentials(user={self.user!r}, dbname={self.dbname!r}, password=***)"


class PgProbe:
    def __init__(self, host: str, port: int, creds: Credentials, timeout: int = 4) -> None:
        self.host, self.port, self.creds, self.timeout = host, port, creds, timeout

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(host=self.host, port=self.port, user=self.creds.user, password=self.creds.password,
                               dbname=self.creds.dbname, sslmode="require", connect_timeout=self.timeout,
                               autocommit=True)

    def ensure_markers_table(self) -> None:
        with self._connect() as c:
            c.execute(f"CREATE TABLE IF NOT EXISTS {MARKER_TABLE} (id bigint PRIMARY KEY, written_at timestamptz DEFAULT now())")

    def write_marker(self, n: int) -> bool:
        try:
            with self._connect() as c:
                c.execute(f"INSERT INTO {MARKER_TABLE} (id) VALUES (%s) ON CONFLICT DO NOTHING", (n,))
            return True
        except Exception:                  # an outage is the expected case during a failover
            return False

    def markers(self) -> set[int] | None:
        try:
            with self._connect() as c:
                return {r[0] for r in c.execute(f"SELECT id FROM {MARKER_TABLE}").fetchall()}
        except Exception:
            return None

    def product_count(self) -> int | None:
        try:
            with self._connect() as c:
                return c.execute("SELECT count(*) FROM products").fetchone()[0]
        except Exception:
            return None

    def self_search_top1(self, product_id: str) -> str | None:
        """Vector search using a stored product's own embedding: the product itself must come back first."""
        try:
            with self._connect() as c:
                row = c.execute(
                    "SELECT id FROM products ORDER BY embedding <=> (SELECT embedding FROM products WHERE id = %s) LIMIT 1",
                    (product_id,)).fetchone()
                return row[0] if row else None
        except Exception:
            return None


def probe_factory(creds: Credentials) -> Callable[[str, int], PgProbe]:
    return lambda host, port: PgProbe(host, port, creds)
