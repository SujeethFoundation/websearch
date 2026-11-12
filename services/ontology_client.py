
import threading
import time
from urllib.parse import quote

import requests
from services.config import settings
from services.http_pool import pooled_session


class OntologyClient:
    def __init__(self):
        self.base = settings.ontology_api_url
        self.session = pooled_session()
        self.session.headers.update({"Accept": "application/json"})
        if settings.ontology_function_key:
            self.session.headers["x-functions-key"] = settings.ontology_function_key
        self._cache = {}  # key -> (expires, value); only successful answers are kept
        self._cache_lock = threading.Lock()
        self._locks = {}  # key -> Lock, one fetch at a time per key

    def _store(self, key, value, ttl):
        with self._cache_lock:
            self._cache[key] = (time.monotonic() + ttl, value)
            limit = settings.ontology_cache_max_entries
            if len(self._cache) > limit:
                for old in list(self._cache)[: max(1, limit // 10)]:
                    self._cache.pop(old, None)

    def _lock_for(self, key):
        with self._cache_lock:
            return self._locks.setdefault(key, threading.Lock())

    def _cached(self, key, fetch):
        """A successful answer, reused for ONTOLOGY_CACHE_SECONDS. A failure is raised and never cached.

        Once an answer has been fetched, a request never waits for the ontology app again: an expired answer is served
        as it is while one background thread fetches the new one (a failed refresh keeps the old answer and is tried
        again next time). The first request for a key waits, and requests that arrive meanwhile wait for that same
        fetch instead of each making their own."""
        ttl = settings.ontology_cache_seconds
        if ttl <= 0:
            return fetch()
        with self._cache_lock:
            hit = self._cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        lock = self._lock_for(key)
        if hit:  # expired: serve it, refresh in the background
            if lock.acquire(blocking=False):
                def refresh():
                    try:
                        self._store(key, fetch(), ttl)
                    except Exception:
                        pass  # keep serving the old answer
                    finally:
                        lock.release()
                threading.Thread(target=refresh, name="ontology-refresh", daemon=True).start()
            return hit[1]
        with lock:  # nothing kept yet: one caller fetches, the others wait for it
            with self._cache_lock:
                hit = self._cache.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
            value = fetch()
            self._store(key, value, ttl)
            return value

    def _url(self, path):
        if not self.base:
            raise RuntimeError("ONTOLOGY_API_URL is not configured")
        return self.base + path

    def get(self, path):
        r = self.session.get(self._url(path), timeout=settings.timeout)
        r.raise_for_status()
        return r.json()

    def post(self, path, data):
        r = self.session.post(self._url(path), json=data, timeout=settings.timeout)
        r.raise_for_status()
        return r.json()

    def ontology(self):
        return self.get("/api/ontology")

    def summary(self):
        return self.get("/api/ontology/summary")

    def classes(self):
        return self.get("/api/ontology/classes")

    def terminology(self):
        return self.get("/api/ontology/terminology")

    def relationships(self):
        return self.get("/api/ontology/relationships")

    def search_manifest(self):
        return self._cached("search-manifest", lambda: self.get("/api/ontology/search-manifest"))

    def crawl_manifest(self):
        return self._cached("crawl-manifest", lambda: self.get("/api/ontology/crawl-manifest"))

    def resolve_domains(self, concepts, purpose="search"):
        # Reused for ONTOLOGY_CACHE_SECONDS, like the manifests and domain validation. Every search resolves
        # its concepts twice (the MCP server resolves first, then the search resolves again), and the answer
        # depends only on the concepts and the purpose, so without this each search made two requests to the
        # ontology app. A change to which domains a concept allows takes effect after that interval. The key
        # keeps the concepts in the order given, so a different order is a different entry.
        key = "resolve:" + str(purpose) + ":" + ",".join(str(c) for c in concepts)
        return self._cached(key, lambda: self.post("/api/ontology/domain-resolution", {
            "concepts": concepts,
            "purpose": purpose,
        }))

    def validate_domains(self, domains, purpose="search"):
        # Reused for ONTOLOGY_CACHE_SECONDS, like the manifests, so a page read does not wait on
        # a second app. A change to a domain's approval takes effect after that interval.
        key = "validate:" + str(purpose) + ":" + ",".join(sorted(str(d).lower() for d in domains))
        return self._cached(key, lambda: self.post("/api/ontology/validate-domains", {
            "domains": domains,
            "purpose": purpose,
        }))


    def domain_concepts(self, domain):
        """Return the ontology-approved concept IDs associated with a crawl domain."""
        payload = self._cached("domain-concepts:" + str(domain).lower(),
                               lambda: self.get("/api/ontology/domains/" + quote(domain, safe="") + "/concepts"))
        if isinstance(payload, dict):
            values = payload.get("associated_concepts") or payload.get("concepts") or payload.get("domain_concepts") or []
            if isinstance(values, list):
                return [str(x.get("id") if isinstance(x, dict) else x) for x in values if x]
        if isinstance(payload, list):
            return [str(x.get("id") if isinstance(x, dict) else x) for x in payload if x]
        return []
