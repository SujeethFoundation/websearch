"""Azure AI Search indexing and hybrid retrieval; Blob Storage remains canonical."""
import base64
import hashlib
import logging
import os
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import requests
from services.config import settings
from services.embedding_service import EmbeddingService

log = logging.getLogger("sujeeth-web-search.ai-search")

# Index write pacing and patience. The search service rejects writes with 429 or 503 when it is
# busy. SEARCH_MIN_INTERVAL_SECONDS spaces index writes at least this far apart on each running
# instance (0 = off). SEARCH_WRITE_ATTEMPTS is how many times one write is tried; the waits grow
# from 2 seconds to 60, so 8 attempts is about 3 minutes, well inside the queue's 15-minute lease.
SEARCH_MIN_INTERVAL = settings.search_min_interval
SEARCH_WRITE_ATTEMPTS = settings.search_write_attempts
_pace_lock = threading.Lock()
_next_write_slot = 0.0

# Chunks are embedded once. The vector is stored next to the chunk (vectors/<id>.json) so a busy or
# failed index never causes a second embedding call. EMBED_BATCH_SIZE caps texts per embedding request.
EMBED_BATCH_SIZE = settings.embed_batch_size
# After the index stays busy through every retry, other writes on this instance skip straight to
# deferral for this many seconds, so workers keep crawling instead of each waiting out the retries.
SEARCH_BREAKER_SECONDS = settings.search_breaker_seconds
_breaker_until = 0.0

# Optional in-memory cache of chunk text, OFF by default because it uses memory (about 2 to 4 KB
# per chunk at the default chunk size, so 1000 entries is roughly 2 to 4 MB). Set
# BLOB_CACHE_SECONDS above 0 to turn it on. A chunk is cleared when this instance writes or
# deletes it; another instance can serve older text for up to BLOB_CACHE_SECONDS.
BLOB_CACHE_SECONDS = int(os.getenv("BLOB_CACHE_SECONDS", "0") or 0)
BLOB_CACHE_MAX = max(100, int(os.getenv("BLOB_CACHE_MAX_ENTRIES", "1000") or 1000))
_blob_cache = {}  # blob name -> (expires, text)
_blob_cache_lock = threading.Lock()


def _blob_cache_get(name):
    if BLOB_CACHE_SECONDS <= 0:
        return None
    with _blob_cache_lock:
        hit = _blob_cache.get(name)
    return hit[1] if hit and hit[0] > time.monotonic() else None


def _blob_cache_put(name, text):
    if BLOB_CACHE_SECONDS <= 0:
        return
    with _blob_cache_lock:
        _blob_cache[name] = (time.monotonic() + BLOB_CACHE_SECONDS, text)
        if len(_blob_cache) > BLOB_CACHE_MAX:
            for key in list(_blob_cache)[: BLOB_CACHE_MAX // 10]:
                _blob_cache.pop(key, None)


def _blob_cache_forget(name):
    with _blob_cache_lock:
        _blob_cache.pop(name, None)


class IndexBusyError(RuntimeError):
    """The search service stayed busy (429 or 503) through every retry."""


def _breaker_open():
    return time.monotonic() < _breaker_until


def _breaker_trip():
    global _breaker_until
    _breaker_until = time.monotonic() + SEARCH_BREAKER_SECONDS


def _breaker_clear():
    global _breaker_until
    _breaker_until = 0.0


def _pace_writes():
    global _next_write_slot
    if SEARCH_MIN_INTERVAL <= 0:
        return
    with _pace_lock:
        now = time.monotonic()
        wait = _next_write_slot - now
        _next_write_slot = max(now, _next_write_slot) + SEARCH_MIN_INTERVAL
    if wait > 0:
        time.sleep(wait)


class SearchService:
    def __init__(self, storage):
        self.storage = storage
        self.embedder = EmbeddingService()
        self.session = requests.Session()
        self.session.headers.update({"api-key": settings.search_api_key})

    @property
    def configured(self):
        return settings.search_configured

    def _url(self, suffix):
        return (f"{settings.search_endpoint}/indexes/{settings.search_index_name}/docs/"
                f"{suffix}?api-version={settings.search_api_version}")

    def _post(self, kind, payload, timeout, attempts=6, max_wait=60):
        """POST to the index with backoff when the service is busy (429, 502, 503, 504) or the
        connection drops. Honors Retry-After. Returns the final response; callers check it."""
        response = None
        for attempt in range(attempts):
            try:
                response = self.session.post(self._url(kind), json=payload, timeout=timeout)
            except (requests.ConnectionError, requests.Timeout):
                if attempt == attempts - 1:
                    raise
                time.sleep(min(2 ** attempt, max_wait))
                continue
            if response.status_code in (429, 502, 503, 504) and attempt < attempts - 1:
                try:
                    wait = float(response.headers.get("Retry-After", 2 ** (attempt + 1)))
                except ValueError:
                    wait = 2 ** (attempt + 1)
                log.warning("Azure AI Search returned %s; retry %d/%d in %.0fs",
                            response.status_code, attempt + 1, attempts - 1, min(wait, max_wait))
                time.sleep(min(wait, max_wait))
                continue
            return response
        return response

    def _write_batch(self, actions, rounds=3):
        """Send index actions and retry the documents the service reports as busy (429 or 503
        inside an otherwise successful response). Returns the final result for each document."""
        pending = list(actions)
        final = {}
        for round_number in range(rounds):
            _pace_writes()
            response = self._post("index", {"value": pending}, 90, attempts=SEARCH_WRITE_ATTEMPTS)
            if response.status_code in (429, 502, 503, 504):
                raise IndexBusyError(f"Azure AI Search stayed busy (HTTP {response.status_code}) through every retry.")
            response.raise_for_status()
            retry_ids = set()
            for item in response.json().get("value", []):
                final[item.get("key")] = item
                if not item.get("status") and item.get("statusCode") in (429, 503):
                    retry_ids.add(item.get("key"))
            if not retry_ids or round_number == rounds - 1:
                break
            pending = [a for a in pending if str(a.get("id")) in retry_ids]
            time.sleep(min(5 * (round_number + 1), 30))
        busy = [x for x in final.values() if not x.get("status") and x.get("statusCode") in (429, 503)]
        if busy:
            raise IndexBusyError(f"Azure AI Search was still busy for {len(busy)} document(s) after every retry.")
        return list(final.values())

    @staticmethod
    def _search_doc(doc, vector=None):
        item = {
            "id": str(doc["id"]),
            "title": str(doc.get("title") or ""),
            "url": str(doc.get("url") or ""),
            "domain": str(doc.get("domain") or "").lower(),
            "concepts": [str(x) for x in (doc.get("concepts") or [])],
            "content": str(doc.get("content") or ""),
        }
        if vector is not None:
            item[settings.search_vector_field] = vector
            for old in settings.search_null_vector_fields:
                if old != settings.search_vector_field:
                    item[old] = None  # drop the old vector so it stops using quota
        return item

    def _upload(self, docs):
        if not self.configured:
            raise RuntimeError("Azure AI Search is not configured.")
        outcomes = []
        for start in range(0, len(docs), settings.search_batch_size):
            batch = docs[start:start + settings.search_batch_size]
            # Blob-backed indexes keep no stored vector copy, so a merge cannot update a vector: replace whole documents.
            action = "upload" if settings.search_blob_backed else "mergeOrUpload"
            actions = [{"@search.action": action, **x} for x in batch]
            result = self._write_batch(actions)
            failures = [x for x in result if not x.get("status")]
            if failures:
                raise RuntimeError(f"Azure AI Search rejected {len(failures)} document(s): {failures[:3]}")
            outcomes.extend(result)
        return outcomes

    def _blob_text(self, doc_id):
        """Text of one chunk from memory or Blob Storage, or None when the blob is missing."""
        name = "documents/" + str(doc_id) + ".json"
        cached = _blob_cache_get(name)
        if cached is not None:
            return cached
        doc = self.storage.get(name)
        if not doc:
            return None
        text = str(doc.get("content") or "")
        _blob_cache_put(name, text)
        return text

    def _blob_content(self, ids, workers=8):
        """Chunk text for these ids, read from Blob Storage several at a time.

        Reading one chunk at a time cost one storage round trip per chunk, so a long page took
        many seconds. Missing blobs are left out, as before.
        """
        ids = [i for i in ids if i]
        if not ids:
            return {}
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(ids)))) as pool:
            texts = list(pool.map(self._blob_text, ids))
        return {str(i): t for i, t in zip(ids, texts) if t is not None}

    def _load_blob_content(self, rows, max_chars=None, batch=8):
        """Fill row["content"] for rows in page order, stopping once max_chars of text is loaded.

        A page read needs only the first max_chars of text, so a long page is not read in full.
        Rows that are not read keep empty content and sit after the rows that were, which lets
        assemble_document report the page as truncated.
        """
        position, loaded = 0, 0
        while position < len(rows):
            if max_chars is not None and loaded >= max_chars:
                break
            group = rows[position:position + batch]
            texts = self._blob_content([r.get("id") for r in group], workers=batch)
            for row in group:
                row["content"] = texts.get(str(row.get("id")), "")
                loaded += len(row["content"])
            position += batch
        for row in rows[position:]:
            row["content"] = ""
        return rows

    def _rewrite_existing(self, updates, docs):
        """Blob-backed retag: re-upload whole documents (with their stored vectors) for chunks already indexed."""
        by_id = {str(d["id"]): d for d in (docs or []) if d.get("id")}
        ids = [str(doc_id) for doc_id, _ in updates]
        present = self._existing_ids(ids)
        todo = []
        for doc_id, concepts in updates:
            doc = by_id.get(str(doc_id))
            if str(doc_id) in present and doc:
                fresh = dict(doc)
                fresh["concepts"] = [str(c) for c in concepts]
                todo.append(fresh)
        if todo:
            vectors = self._vectors_for(todo)
            self._upload([self._search_doc(d, v) for d, v in zip(todo, vectors)])
        return len(todo), len(ids) - len(present)

    def update_concepts(self, updates, docs=None):
        """Apply new concept tags to documents already in the index. No re-embedding of unchanged text.

        updates is a list of (document_id, concepts). Normally this uses the 'merge' action, so a
        document that is not indexed yet is skipped, never created as an empty stub. A blob-backed index
        stores no vector copy, so there the whole document is re-uploaded from `docs` instead.
        Returns (updated, not_indexed).
        """
        if not self.configured:
            raise RuntimeError("Azure AI Search is not configured.")
        if settings.search_blob_backed:
            return self._rewrite_existing(updates, docs)
        updated = 0
        not_indexed = 0
        for start in range(0, len(updates), settings.search_batch_size):
            batch = updates[start:start + settings.search_batch_size]
            actions = [
                {"@search.action": "merge", "id": str(doc_id), "concepts": [str(c) for c in concepts]}
                for doc_id, concepts in batch
            ]
            for item in self._write_batch(actions):
                if item.get("status"):
                    updated += 1
                elif item.get("statusCode") == 404:
                    not_indexed += 1
                else:
                    raise RuntimeError(f"Azure AI Search rejected a concept update: {item}")
        return updated, not_indexed

    def fetch_chunks(self, url=None, chunk_id=None, neighbors=1, max_chars=None, timings=None):
        """Read stored chunks of one page from the index.

        With chunk_id, returns that chunk and up to `neighbors` chunks on each side. With
        url, returns every chunk of the page. Returns (rows, focus_index, total_chunks).
        """
        if not self.configured:
            raise RuntimeError("Azure AI Search is not configured.")
        focus = None
        if chunk_id:
            page_id, _, suffix = str(chunk_id).rpartition("-")
            if not page_id or not suffix.isdigit():
                raise ValueError("chunk_id must look like <page>-<number>.")
            focus = int(suffix)
            reach = max(0, min(int(neighbors), 3))
            ids = [f"{page_id}-{i}" for i in range(max(0, focus - reach), focus + reach + 1)]
            filter_expr = f"search.in(id, '{self._odata_values(ids)}', ',')"
        elif url:
            base = str(url).strip().split("#", 1)[0]
            variants = sorted({base, base.rstrip("/"), base.rstrip("/") + "/"} - {"", "/"})
            joined = "|".join(v.replace("'", "''") for v in variants)
            filter_expr = f"search.in(url, '{joined}', '|')"
        else:
            raise ValueError("Provide url or chunk_id.")
        select = "id,title,url,domain,concepts" if settings.search_blob_backed else "id,title,url,domain,concepts,content"
        began = time.perf_counter()
        response = self._post("search", {
            "search": "*", "filter": filter_expr, "top": 1000, "select": select,
        }, 45, attempts=3, max_wait=5)
        response.raise_for_status()
        rows = response.json().get("value", [])
        found = len(rows)
        distinct = len({r.get("id") for r in rows})
        if timings is not None:
            timings["index_query_ms"] = round((time.perf_counter() - began) * 1000)
        if settings.search_blob_backed:
            # A page this long is cut off at 400 chunks. Read them in page order, and for a whole
            # page stop once max_chars of text is loaded.
            began = time.perf_counter()
            rows = sorted(rows, key=lambda r: chunk_index(r.get("id")))[:400]
            rows = self._load_blob_content(rows, None if chunk_id else max_chars)
            if timings is not None:
                timings["blob_ms"] = round((time.perf_counter() - began) * 1000)
        total = None
        if rows:
            if not chunk_id and found < 1000:
                # The query above already returned every chunk of the page, so no count is needed.
                total = distinct
            else:
                began = time.perf_counter()
                page_url = str(rows[0].get("url") or "").replace("'", "''")
                counted = self._post("search", {
                    "search": "*", "filter": f"url eq '{page_url}'", "top": 0, "count": True,
                }, 45, attempts=3, max_wait=5)
                counted.raise_for_status()
                total = counted.json().get("@odata.count")
                if timings is not None:
                    timings["count_ms"] = round((time.perf_counter() - began) * 1000)
        return rows, focus, total

    def delete_ids(self, ids):
        """Delete documents from the index by id. Deleting an id that is not there is not an error."""
        ids = [str(x) for x in ids if x]
        if not self.configured or not ids:
            return 0
        results = self._write_batch([{"@search.action": "delete", "id": doc_id} for doc_id in ids])
        failed = [x for x in results if not x.get("status")]
        if failed:
            raise RuntimeError(f"Azure AI Search could not delete {len(failed)} document(s): {failed[:3]}")
        return len(ids)

    @staticmethod
    def vector_key(doc_id):
        return "vectors/" + str(doc_id) + ".json"

    @staticmethod
    def _pack_vector(vector):
        return base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode("ascii")

    @staticmethod
    def _unpack_vector(text):
        raw = base64.b64decode(text)
        return list(struct.unpack(f"<{len(raw) // 4}f", raw))

    @staticmethod
    def _content_sha(content):
        return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()

    def _vectors_for(self, documents):
        """One vector per document. A stored vector is reused when it matches the chunk's current
        text and the embedding model. Only the rest are embedded, then stored immediately."""
        shas = [self._content_sha(d.get("content", "")) for d in documents]
        vectors = [None] * len(documents)
        for i, doc in enumerate(documents):
            stored = self.storage.get(self.vector_key(doc["id"]))
            if (stored and stored.get("sha") == shas[i]
                    and stored.get("model") == settings.embedding_deployment
                    and int(stored.get("dims", 0)) == settings.embedding_dimensions):
                try:
                    vectors[i] = self._unpack_vector(stored["v"])
                except Exception:
                    vectors[i] = None
        missing = [i for i, vector in enumerate(vectors) if vector is None]
        for start in range(0, len(missing), EMBED_BATCH_SIZE):
            group = missing[start:start + EMBED_BATCH_SIZE]
            fresh = self.embedder.embed([documents[i].get("content", "") for i in group])
            for i, vector in zip(group, fresh):
                vectors[i] = vector
                self.storage.put(self.vector_key(documents[i]["id"]), {
                    "id": str(documents[i]["id"]), "sha": shas[i],
                    "model": settings.embedding_deployment, "dims": len(vector),
                    "v": self._pack_vector(vector),
                })
        return vectors

    def upsert_or_defer(self, documents, persist_blobs=True):
        """Embed (once), store the chunks and vectors, then try to index them.

        Returns (indexed_count, deferred_blob_names). If the search index stays busy, nothing is
        raised: the chunks and vectors are already safe in Blob Storage, and the caller queues a
        delayed indexing task for the returned blob names.
        """
        documents = [dict(x) for x in documents if x and x.get("id")]
        if not documents:
            return 0, []
        if not settings.embedding_configured:
            raise RuntimeError("Embedding configuration is missing; refusing to index documents without vectors.")
        vectors = self._vectors_for(documents)
        search_docs = [self._search_doc(doc, vector) for doc, vector in zip(documents, vectors)]
        # Save source documents first. They remain the durable, rebuildable source of truth.
        if persist_blobs:
            for doc in documents:
                key = "documents/" + str(doc["id"]) + ".json"
                stored = dict(doc)
                stored["_key"] = key
                stored.pop("embedding", None)
                self.storage.put(key, stored)
                _blob_cache_forget(key)
        names = ["documents/" + str(doc["id"]) + ".json" for doc in documents]
        if _breaker_open():
            return 0, names
        try:
            self._upload(search_docs)
        except IndexBusyError as exc:
            _breaker_trip()
            log.warning("Search index busy; %d chunk(s) stored and queued for later indexing: %s", len(names), exc)
            return 0, names
        _breaker_clear()
        return len(search_docs), []

    def upsert_many(self, documents, persist_blobs=True):
        count, deferred = self.upsert_or_defer(documents, persist_blobs)
        if deferred:
            raise IndexBusyError(f"Azure AI Search is busy; {len(deferred)} chunk(s) are stored but not indexed.")
        return count

    def _existing_ids(self, ids):
        """Which of these chunk ids are already in the index."""
        ids = [str(x) for x in ids]
        found = set()
        for start in range(0, len(ids), 100):
            group = ids[start:start + 100]
            response = self._post("search", {
                "search": "*",
                "filter": f"search.in(id, '{self._odata_values(group)}', ',')",
                "select": "id",
                "top": len(group),
            }, 45, attempts=3, max_wait=5)
            if response.status_code in (429, 502, 503, 504):
                raise IndexBusyError(f"Azure AI Search was busy (HTTP {response.status_code}) while checking existing chunks.")
            if response.status_code == 400:
                # The id field is not filterable in this index, so look each chunk up directly.
                found.update(self._existing_ids_by_lookup(group))
                continue
            response.raise_for_status()
            found.update(str(x.get("id")) for x in response.json().get("value", []))
        return found

    def _existing_ids_by_lookup(self, ids):
        found = set()
        for doc_id in ids:
            url = (f"{settings.search_endpoint}/indexes/{settings.search_index_name}/docs('{doc_id}')"
                   f"?api-version={settings.search_api_version}&$select=id")
            response = self.session.get(url, timeout=30)
            if response.status_code == 200:
                found.add(str(doc_id))
            elif response.status_code in (429, 502, 503, 504):
                raise IndexBusyError(f"Azure AI Search was busy (HTTP {response.status_code}) while checking existing chunks.")
            elif response.status_code != 404:
                response.raise_for_status()
        return found

    def index_blobs(self, blob_names, only_missing=False, domains=None):
        """Index stored chunks. With only_missing, chunks already in the index are left alone and are never downloaded
        (the chunk id is in the blob name). With domains, only chunks from those domains are considered.
        Stored vectors are reused, so a chunk is never embedded twice."""
        only = {str(x).lower() for x in (domains or []) if str(x).strip()}
        already = 0
        if only_missing and not only:
            ids = {name: name.rsplit("/", 1)[-1].removesuffix(".json") for name in blob_names}
            present = self._existing_ids(list(ids.values()))
            already = len(present)
            blob_names = [name for name, doc_id in ids.items() if doc_id not in present]
        docs = []
        for name in blob_names:
            doc = self.storage.get(name)
            if doc and doc.get("id") and doc.get("content"):
                if only and str(doc.get("domain") or "").lower() not in only:
                    continue
                docs.append(doc)
        if only_missing and only and docs:
            present = self._existing_ids([d["id"] for d in docs])
            already = len(present)
            docs = [d for d in docs if str(d["id"]) not in present]
        indexed = self.upsert_many(docs, persist_blobs=False) if docs else 0
        return {"read": len(docs) + already, "already_indexed": already, "indexed": indexed}

    def index_existing_blobs(self, blob_names):
        return self.index_blobs(blob_names)["indexed"]

    @staticmethod
    def _odata_values(values):
        safe = [str(x).replace("'", "''") for x in values if str(x)]
        return ",".join(safe)

    def search(self, query, concepts=None, domains=None, limit=5):
        concepts = sorted({str(x).lower() for x in (concepts or [])})
        domains = sorted({str(x).lower() for x in (domains or [])})
        if not concepts or not domains:
            raise ValueError("Search requires ontology-resolved concepts and domains.")
        if not self.configured:
            raise RuntimeError("Azure AI Search is not configured.")
        domain_values = self._odata_values(domains)
        concept_values = self._odata_values(concepts)
        filter_expr = (
            f"search.in(domain, '{domain_values}', ',') and "
            f"concepts/any(c: search.in(c, '{concept_values}', ','))"
        )
        body = {
            "search": query,
            "searchMode": "any",
            "filter": filter_expr,
            "vectorQueries": [{
                "kind": "text",
                "text": query,
                "fields": settings.search_vector_field,
                "k": max(50, limit * 10),
            }],
            "select": "id,title,url,domain,concepts" if settings.search_blob_backed else "id,title,url,domain,concepts,content",
            "top": limit,
            "count": True,
        }
        response = self._post("search", body, 45, attempts=3, max_wait=5)
        response.raise_for_status()
        payload = response.json()
        results = []
        items = payload.get("value", [])
        texts = self._blob_content([i.get("id") for i in items]) if settings.search_blob_backed else {}
        for item in items:
            text = texts.get(str(item.get("id")), "") if settings.search_blob_backed else (item.get("content") or "")
            results.append({
                "title": item.get("title"),
                "url": item.get("url"),
                "snippet": text[:700],
                "domain": item.get("domain"),
                "concepts": item.get("concepts", []),
                "score": round(float(item.get("@search.score", 0.0)), 6),
                "source": "website",
                "chunk_id": item.get("id"),
            })
        return {
            "query": query,
            "count": len(results),
            "total_matches": payload.get("@odata.count"),
            "search_mode": "hybrid",
            "embedding_enabled": True,
            "results": results,
        }

    def delete_url(self, url):
        if not self.configured:
            # Legacy fallback is retained for local development only.
            removed = 0
            for doc in list(self.storage.list("documents/")):
                if doc.get("url") == url:
                    self.storage.delete(doc.get("_key") or ("documents/" + str(doc.get("id")) + ".json"))
                    removed += 1
            return removed
        filter_expr = "url eq '" + str(url).replace("'", "''") + "'"
        response = self._post("search", {
            "search": "*", "filter": filter_expr, "select": "id", "top": 1000
        }, 45, attempts=3, max_wait=5)
        response.raise_for_status()
        ids = [x.get("id") for x in response.json().get("value", []) if x.get("id")]
        if not ids:
            return 0
        self._write_batch([{"@search.action": "delete", "id": doc_id} for doc_id in ids])
        for doc_id in ids:
            self.storage.delete("documents/" + str(doc_id) + ".json")
            _blob_cache_forget("documents/" + str(doc_id) + ".json")
            self.storage.delete(self.vector_key(doc_id))
        return len(ids)

    def status(self):
        if not self.configured:
            return {"configured": False, "index": settings.search_index_name}
        response = self._post("search", {
            "search": "*", "top": 0, "count": True
        }, 30, attempts=3, max_wait=5)
        response.raise_for_status()
        payload = response.json()
        return {
            "configured": True,
            "index": settings.search_index_name,
            "document_count": payload.get("@odata.count"),
            "embedding_configured": settings.embedding_configured,
        }


def chunk_index(chunk_id):
    """Position of a chunk within its page: the number after the last dash in its id."""
    try:
        return int(str(chunk_id).rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return 0


def assemble_document(rows, focus=None, limit=6000):
    """Choose which chunks to return within a character limit.

    With a focus chunk, the matched passage comes first and neighbors are added by closeness
    while they fit. Without one, chunks are taken in page order and the last one is cut at
    the limit. Returns (chunks in page order, truncated).
    """
    rows = sorted(rows, key=lambda r: chunk_index(r.get("id")))
    order = rows
    if focus is not None:
        order = sorted(rows, key=lambda r: abs(chunk_index(r.get("id")) - focus))
    chosen, used, truncated = [], 0, False
    for row in order:
        text = str(row.get("content") or "")
        room = limit - used
        if room <= 0:
            truncated = True
            break
        if len(text) <= room:
            chosen.append({**row, "content": text})
            used += len(text)
        elif not chosen or focus is None:
            chosen.append({**row, "content": text[:room]})
            used += room
            truncated = True
            if focus is None:
                break
        else:
            truncated = True
    chosen.sort(key=lambda r: chunk_index(r.get("id")))
    return chosen, truncated
