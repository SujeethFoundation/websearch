"""Embedding client for the Azure OpenAI-compatible Microsoft Foundry endpoint."""
import logging
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor

import requests

from services.config import settings
from services.http_pool import pooled_session
from services.local_index import binary_code
from services.query_code_cache import QueryCodeCache, normalize_question

log = logging.getLogger("sujeeth-web-search.embeddings")

RETRYABLE_STATUS = (429, 500, 502, 503, 504)
MAX_ATTEMPTS = 6
MAX_WAIT_SECONDS = 60

# Indexing throttle. Spaces embedding calls at least this many seconds apart on each
# running instance, so indexing leaves quota for search (the index vectorizer shares
# this deployment). 0 disables it. Set EMBED_MIN_INTERVAL_SECONDS in app settings.
MIN_INTERVAL_SECONDS = settings.embed_min_interval
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


QUERY_CACHE_ENTRIES = 2000  # about 6 KB each as Python floats


class EmbeddingService:
    def __init__(self):
        self.session = pooled_session()
        self.session.headers.update({"api-key": settings.embedding_key})
        self._query_cache = OrderedDict()
        self._query_lock = threading.Lock()
        self._inflight = {}      # question key -> {"event": Event, "error": exception or None}
        self._down_until = 0.0   # while time.monotonic() is below this, search questions skip the endpoint
        # Question codes (1 bit per dimension, 192 bytes) in memory, then in a table every instance shares.
        self._codes = OrderedDict()
        self._code_inflight = {}
        self._shared = QueryCodeCache() if settings.embed_cache_shared else None
        self._code_stats = {"lookups": 0, "memory_hits": 0, "shared_hits": 0, "coalesced": 0, "embed_calls": 0,
                            "embed_failures": 0, "remote_ms": 0.0, "seeded": 0}

    def _post_with_retry(self, url, payload, throttled=True, attempts=MAX_ATTEMPTS, timeout=90, fail_fast_over=None):
        """POST with exponential backoff on throttling and transient server errors.

        With fail_fast_over, a wait longer than that many seconds is not slept: the error is raised at once."""
        response = None
        for attempt in range(attempts):
            if throttled:
                _throttle()
            try:
                response = self.session.post(
                    url,
                    params={"api-version": settings.embedding_api_version},
                    json=payload,
                    timeout=timeout,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt == attempts - 1:
                    raise
                wait = min(2 ** attempt, MAX_WAIT_SECONDS)
                if fail_fast_over is not None:
                    wait = min(wait, fail_fast_over)
                log.warning("Embedding request failed (%s); retry %d/%d in %ss",
                            exc.__class__.__name__, attempt + 1, attempts - 1, wait)
                time.sleep(wait)
                continue

            if response.status_code in RETRYABLE_STATUS and attempt < attempts - 1:
                try:
                    wait = float(response.headers.get("Retry-After", 2 ** attempt))
                except ValueError:
                    wait = 2 ** attempt
                wait = min(wait, MAX_WAIT_SECONDS)
                if fail_fast_over is not None and wait > fail_fast_over:
                    log.warning("Embedding endpoint returned %s and asked for a %ss wait; not waiting on the search path",
                                response.status_code, wait)
                    response.raise_for_status()
                log.warning("Embedding endpoint returned %s; retry %d/%d in %ss",
                            response.status_code, attempt + 1, attempts - 1, wait)
                time.sleep(wait)
                continue

            response.raise_for_status()
            return response

        response.raise_for_status()
        return response

    def _request_query_vector(self, text):
        """One embedding request for a search question, bounded by the EMBED_QUERY_* settings."""
        url = f"{settings.embedding_endpoint}/openai/v1/embeddings"
        response = self._post_with_retry(url, {
            "model": settings.embedding_deployment,
            "input": [str(text).strip()],
            "dimensions": settings.embedding_dimensions,
        }, throttled=False, attempts=settings.embed_query_attempts, timeout=settings.embed_query_timeout,
            fail_fast_over=settings.embed_query_max_wait)
        data = response.json().get("data", [])
        if len(data) != 1 or len(data[0]["embedding"]) != settings.embedding_dimensions:
            raise RuntimeError("The embedding service returned an unexpected answer for a search question.")
        return data[0]["embedding"]

    def embed_query(self, text, remember=True):
        """The vector for one search question. Unlike embed(), this is not slowed down by the indexing throttle, gives up
        sooner, and remembers recent questions, since the same question often arrives again.

        Concurrent requests for the same new question share one embedding call (single flight). After a failure the
        endpoint is skipped for EMBED_QUERY_COOLDOWN_SECONDS, so searches fall back to keywords at once instead of each
        waiting out the timeout and retries while the endpoint is down. remember=False leaves the vector out of the
        in-memory vector cache (embed_query_code keeps only the 1-bit code, which is all the local search uses)."""
        key = " ".join(str(text or "").lower().split()).strip(" \"'?!.,;:")
        if not key:
            raise ValueError("A question is required.")
        with self._query_lock:
            cached = self._query_cache.get(key)
            if cached is not None:
                self._query_cache.move_to_end(key)
                return cached
            flight = self._inflight.get(key)
            leader = flight is None
            if leader:
                if time.monotonic() < self._down_until:
                    raise RuntimeError("Search question embedding is paused after a recent failure.")
                flight = self._inflight[key] = {"event": threading.Event(), "error": None}
        if not leader:
            flight["event"].wait(settings.embed_query_timeout * settings.embed_query_attempts + 1)
            with self._query_lock:
                cached = self._query_cache.get(key)
            if cached is None:
                cached = flight.get("vector")
            if cached is not None:
                return cached
            raise flight["error"] or RuntimeError("The embedding request for this search question did not finish.")
        try:
            vector = self._request_query_vector(text)
            flight["vector"] = vector
            if remember:
                with self._query_lock:
                    self._query_cache[key] = vector
                    while len(self._query_cache) > QUERY_CACHE_ENTRIES:
                        self._query_cache.popitem(last=False)
            return vector
        except Exception as exc:
            flight["error"] = exc
            if settings.embed_query_cooldown > 0:
                self._down_until = time.monotonic() + settings.embed_query_cooldown
            raise
        finally:
            with self._query_lock:
                self._inflight.pop(key, None)
            flight["event"].set()

    # ---- the 1-bit code of a question: memory, then the shared table, then the embedding call

    def _memory_get(self, key):
        with self._query_lock:
            code = self._codes.get(key)
            if code is not None:
                self._codes.move_to_end(key)
            return code

    def _memory_put(self, key, code):
        with self._query_lock:
            self._codes[key] = code
            while len(self._codes) > settings.embed_cache_memory_entries:
                self._codes.popitem(last=False)

    def _bump(self, name, amount=1):
        with self._query_lock:
            self._code_stats[name] += amount

    def embed_query_code(self, text):
        """The 1-bit code of one search question, which is all the local search uses.

        Looks in memory, then in the table every instance shares, and only then calls the embedding service (and
        remembers the answer in both). Several searches for the same new question at once share one call."""
        key = normalize_question(text)
        if not key:
            raise ValueError("A question is required.")
        self._bump("lookups")
        code = self._memory_get(key)
        if code is not None:
            self._bump("memory_hits")
            return code
        with self._query_lock:
            waiting = self._code_inflight.get(key)
            if waiting is None:
                self._code_inflight[key] = mine = Future()
        if waiting is not None:
            self._bump("coalesced")
            return waiting.result(timeout=settings.embed_query_timeout * settings.embed_query_attempts + 5)
        try:
            code = self._shared.get(key) if self._shared else None
            if code is not None:
                self._bump("shared_hits")
            else:
                began = time.perf_counter()
                try:
                    code = binary_code(self.embed_query(text, remember=False))
                except BaseException:
                    self._bump("embed_failures")
                    raise
                finally:
                    self._bump("embed_calls")
                    self._bump("remote_ms", (time.perf_counter() - began) * 1000)
                if self._shared:
                    self._shared.put(key, code)
            self._memory_put(key, code)
            mine.set_result(code)
            return code
        except BaseException as exc:
            mine.set_exception(exc)
            raise
        finally:
            with self._query_lock:
                self._code_inflight.pop(key, None)

    def seed_questions(self, questions, workers=4):
        """Embed questions ahead of time and store their codes, so visitors never wait for them. Questions already
        remembered are skipped. Returns counts: requested, unique, already_cached, embedded, failed."""
        unique = {}
        for q in questions or []:
            key = normalize_question(q)
            if key and key not in unique:
                unique[key] = str(q).strip()
        out = {"requested": len(questions or []), "unique": len(unique), "already_cached": 0, "embedded": 0, "failed": 0}
        lock = threading.Lock()

        def one(item):
            key, text = item
            try:
                if self._memory_get(key) is not None or (self._shared and self._shared.get(key) is not None):
                    with lock:
                        out["already_cached"] += 1
                    return
                code = binary_code(self.embed_query(text, remember=False))
                self._memory_put(key, code)
                if self._shared:
                    self._shared.put(key, code, wait=True)
                with lock:
                    out["embedded"] += 1
                self._bump("seeded")
            except Exception as exc:
                log.warning("Could not seed a question (%s)", exc.__class__.__name__)
                with lock:
                    out["failed"] += 1

        with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
            list(pool.map(one, unique.items()))
        return out

    def cache_stats(self):
        """What the question-code cache has saved: lookups, where each was answered, and the shared table's own counts."""
        with self._query_lock:
            s = dict(self._code_stats)
            entries = len(self._codes)
        lookups = s["lookups"]
        saved = lookups - s["embed_calls"]
        return {
            "lookups": lookups, "memory_hits": s["memory_hits"], "shared_hits": s["shared_hits"], "coalesced": s["coalesced"],
            "embed_calls": s["embed_calls"], "embed_failures": s["embed_failures"], "seeded": s["seeded"],
            "hit_rate": round(saved / lookups, 4) if lookups else None,
            "avg_remote_ms": round(s["remote_ms"] / s["embed_calls"], 1) if s["embed_calls"] else None,
            "memory_entries": entries, "memory_limit": settings.embed_cache_memory_entries,
            "shared": self._shared.describe() if self._shared else {"enabled": False},
        }

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