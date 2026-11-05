"""Bedrock model factories (LangChain). Model ids come from config, never from code."""
from __future__ import annotations

from langchain_aws import BedrockEmbeddings, ChatBedrockConverse

from .config import Settings


def build_embeddings(settings: Settings) -> BedrockEmbeddings:
    return BedrockEmbeddings(
        model_id=settings.embedding_model_id,
        region_name=settings.region,
        credentials_profile_name=settings.profile,
        model_kwargs={"dimensions": settings.embedding_dimensions, "normalize": True},
    )


def build_chat_models(settings: Settings) -> list[tuple[str, ChatBedrockConverse]]:
    """Ordered fallback chain of chat models: [(model_id, model), ...]."""
    return [
        (model_id, ChatBedrockConverse(
            model=model_id, region_name=settings.region,
            credentials_profile_name=settings.profile, temperature=0, max_tokens=1024))
        for model_id in settings.chat_model_ids
    ]
