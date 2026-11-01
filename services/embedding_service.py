"""Embedding client for the Azure OpenAI-compatible Microsoft Foundry endpoint."""
import logging
import os
import threading
import time

import requests

from services.config import settings

log = logging.getLogger("sujeeth-web-search.embeddings")

RETRYABLE_STATUS = (429, 500, 502, 503, 504)
MAX_ATTEMPTS = 6
MAX_WAIT_SECONDS = 60

# Indexing throttle. Spaces embedding calls at least this many seconds apart on each
# running instance, so indexing leaves quota for search (the index vectorizer shares
# this deployment). 0 disables it. Set EMBED_MIN_INTERVAL_SECONDS in app settings.
MIN_INTERVAL_SECONDS = float(os.getenv("EMBED_MIN_INTERVAL_SECONDS", "0") or 0)
_throttle_lock = threading.Lock()
_next_slot = 0.0


def _throttle():
    global _next_slot
    if MIN_INTERVAL_SECONDS <= 0:
        return
    with _throttle_lock:
        now = time.monotonic()
        wait = _next_slot - now
        _next_slot = max(now, _next_slot) + MIN_INTERVAL_SECONDS
    if wait > 0:
        time.sleep(wait)


class EmbeddingService:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"api-key": settings.embedding_key})

    def _post_with_retry(self, url, payload):
        """POST with exponential backoff on throttling and transient server errors."""
        response = None
        for attempt in range(MAX_ATTEMPTS):
            _throttle()
            try:
                response = self.session.post(
                    url,
                    params={"api-version": settings.embedding_api_version},
                    json=payload,
                    timeout=90,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt == MAX_ATTEMPTS - 1:
                    raise
                wait = min(2 ** attempt, MAX_WAIT_SECONDS)
                log.warning("Embedding request failed (%s); retry %d/%d in %ss",
                            exc.__class__.__name__, attempt + 1, MAX_ATTEMPTS - 1, wait)
                time.sleep(wait)
                continue

            if response.status_code in RETRYABLE_STATUS and attempt < MAX_ATTEMPTS - 1:
                try:
                    wait = float(response.headers.get("Retry-After", 2 ** attempt))
                except ValueError:
                    wait = 2 ** attempt
                wait = min(wait, MAX_WAIT_SECONDS)
                log.warning("Embedding endpoint returned %s; retry %d/%d in %ss",
                            response.status_code, attempt + 1, MAX_ATTEMPTS - 1, wait)
                time.sleep(wait)
                continue

            response.raise_for_status()
            return response

        response.raise_for_status()
        return response

    def embed(self, texts):
        texts = [str(x or "").strip() for x in (texts or [])]
        if not texts:
            return []
        if not settings.embedding_configured:
            raise RuntimeError("Embeddings are not configured. Set AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, and AZURE_OPENAI_EMBEDDING_DEPLOYMENT.")
        # Azure AI Foundry's OpenAI-compatible v1 embeddings endpoint.
        url = f"{settings.embedding_endpoint}/openai/v1/embeddings"
        response = self._post_with_retry(url, {
            "model": settings.embedding_deployment,
            "input": texts,
            "dimensions": settings.embedding_dimensions,
        })
        data = response.json().get("data", [])
        vectors = [x["embedding"] for x in sorted(data, key=lambda x: x.get("index", 0))]
        if len(vectors) != len(texts):
            raise RuntimeError(f"Embedding service returned {len(vectors)} vectors for {len(texts)} inputs.")
        if any(len(v) != settings.embedding_dimensions for v in vectors):
            raise RuntimeError("Embedding dimensions do not match the Azure AI Search index.")
        return vectors