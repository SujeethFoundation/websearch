"""Embedding client for the Azure OpenAI-compatible Microsoft Foundry endpoint."""
import requests
from services.config import settings


class EmbeddingService:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"api-key": settings.embedding_key})

    def embed(self, texts):
        texts = [str(x or "").strip() for x in (texts or [])]
        if not texts:
            return []
        if not settings.embedding_configured:
            raise RuntimeError("Embeddings are not configured. Set AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, and AZURE_OPENAI_EMBEDDING_DEPLOYMENT.")
        # Azure AI Foundry's OpenAI-compatible v1 embeddings endpoint.
        url = f"{settings.embedding_endpoint}/openai/v1/embeddings"
        response = self.session.post(
            url,
            params={"api-version": settings.embedding_api_version},
            json={
                "model": settings.embedding_deployment,
                "input": texts,
                "dimensions": settings.embedding_dimensions,
            },
            timeout=90,
        )
        response.raise_for_status()
        data = response.json().get("data", [])
        vectors = [x["embedding"] for x in sorted(data, key=lambda x: x.get("index", 0))]
        if len(vectors) != len(texts):
            raise RuntimeError(f"Embedding service returned {len(vectors)} vectors for {len(texts)} inputs.")
        if any(len(v) != settings.embedding_dimensions for v in vectors):
            raise RuntimeError("Embedding dimensions do not match the Azure AI Search index.")
        return vectors
