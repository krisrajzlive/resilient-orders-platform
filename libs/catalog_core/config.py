from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv() -> None:
    path = Path(__file__).resolve().parents[2] / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


@dataclass(frozen=True)
class Settings:
    region: str
    profile: str | None
    database_url: str
    embedding_model_id: str
    embedding_dimensions: int
    # Ordered fallback chain: first model that answers wins.
    chat_model_ids: tuple[str, ...]


def load_settings() -> Settings:
    _load_dotenv()
    env = os.environ
    chain = [env.get("REASONING_MODEL_ID"), env.get("GENERATION_MODEL_ID"), env.get("FALLBACK_MODEL_ID")]
    chain = [m for m in chain if m] or [
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "amazon.nova-lite-v1:0",
    ]
    return Settings(
        region=env.get("AWS_REGION", "us-east-1"),
        profile=env.get("AWS_PROFILE") or None,
        database_url=env.get(
            "DATABASE_URL", "postgresql://orders:orders-local-dev@127.0.0.1:5433/orders"),
        embedding_model_id=env.get("EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0"),
        embedding_dimensions=int(env.get("EMBEDDING_DIMENSIONS", "1024")),
        chat_model_ids=tuple(dict.fromkeys(chain)),
    )
