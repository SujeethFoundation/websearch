
import requests
from services.config import settings


class EmbeddingService:
    def embed(self, texts):
        if not settings.embedding_configured:
            return None
        url = (
            f"{settings.embedding_endpoint}/openai/deployments/"
            f"{settings.embedding_deployment}/embeddings"
            f"?api-version={settings.embedding_api_version}"
        )
        response = requests.post(
            url,
            headers={"api-key": settings.embedding_key},
            json={"input": texts},
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()["data"]
        return [x["embedding"] for x in sorted(data, key=lambda x: x["index"])]
