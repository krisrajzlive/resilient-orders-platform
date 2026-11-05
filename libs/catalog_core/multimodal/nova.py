"""Amazon Nova multimodal embeddings: text and images share one vector space."""
from __future__ import annotations

import base64
import json

import boto3
from botocore.config import Config

from ..config import Settings

MODEL_ID = "amazon.nova-2-multimodal-embeddings-v1:0"


class NovaMultimodalEmbedder:
    def __init__(self, settings: Settings, client=None):
        self.dimensions = settings.embedding_dimensions
        self._client = client or boto3.Session(
            profile_name=settings.profile, region_name=settings.region
        ).client("bedrock-runtime", config=Config(retries={"max_attempts": 5, "mode": "adaptive"}))

    def _embed(self, params: dict) -> list[float]:
        body = {"schemaVersion": "nova-multimodal-embed-v1", "taskType": "SINGLE_EMBEDDING",
                "singleEmbeddingParams": {"embeddingDimension": self.dimensions, **params}}
        resp = self._client.invoke_model(modelId=MODEL_ID, body=json.dumps(body))
        return json.loads(resp["body"].read())["embeddings"][0]["embedding"]

    def embed_text(self, text: str, purpose: str = "GENERIC_RETRIEVAL") -> list[float]:
        return self._embed({"embeddingPurpose": purpose,
                            "text": {"truncationMode": "END", "value": text}})

    def embed_image(self, data: bytes, fmt: str = "png", purpose: str = "GENERIC_INDEX") -> list[float]:
        return self._embed({"embeddingPurpose": purpose, "image": {
            "format": fmt, "source": {"bytes": base64.b64encode(data).decode()}}})
