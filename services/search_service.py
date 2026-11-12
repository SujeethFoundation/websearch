"""Azure AI Search indexing and hybrid retrieval; Blob Storage remains canonical."""
import base64
import copy
import hashlib
import logging
import os
import random
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import requests
from services.config import settings
from services.embedding_service import EmbeddingService
from services.http_pool import pooled_session
from services.local_index import (
    LocalIndexManager, LocalIndexUnavailable, binary_code, entity_from_search_doc, query_terms, split_chunk_id,
)

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
# Counts for this process, so the cache can be checked from outside (see blob_cache_stats and the /health route).
# hits: reads answered from memory. misses: cache on, not found or expired. storage_reads: every actual
# download from Blob Storage, whether or not the cache is on.
_blob_stats = {"hits": 0, "misses": 0, "storage_reads": 0}


def _blob_stat(name):
    with _blob_cache_lock:
        _blob_stats[name] += 1


def blob_cache_stats():
    """What the chunk-text cache is doing in this process. Cleared when the process restarts."""
    with _blob_cache_lock:
        hits, misses, reads = _blob_stats["hits"], _blob_stats["misses"], _blob_stats["storage_reads"]
        entries = len(_blob_cache)
    lookups = hits + misses
    return {
        "enabled": BLOB_CACHE_SECONDS > 0,
        "seconds": BLOB_CACHE_SECONDS,
        "max_entries": BLOB_CACHE_MAX,
        "entries": entries,
        "hits": hits,
        "misses": misses,
        "hit_rate": round(hits / lookups, 3) if lookups else None,
        "storage_reads": reads,
    }


# Compute Units the service reports for each request (the x-ms-azs-compute-units-consumed header, present on the
# Serverless tier). Counted per label for this process so the cost of one kind of request can be read from
# /health without a log query. Every response counts, including the ones that are retried.
_cu_stats = {}
_cu_lock = threading.Lock()


def _record_compute_units(label, response):
    raw = None
    try:
        raw = response.headers.get("x-ms-azs-compute-units-consumed")
    except Exception:
        pass
    with _cu_lock:
        row = _cu_stats.setdefault(label, {"requests": 0, "with_header": 0, "total": 0.0, "max": 0.0})
        row["requests"] += 1
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return
        row["with_header"] += 1
        row["total"] += value
        row["max"] = max(row["max"], value)


def compute_unit_stats():
    """Compute Units per kind of request since this process started. average is per request that reported one."""
    with _cu_lock:
        out = {}
        for label, row in sorted(_cu_stats.items()):
            counted = row["with_header"]
            out[label] = {
                "requests": row["requests"],
                "reported": counted,
                "total": round(row["total"], 3),
                "average": round(row["total"] / counted, 3) if counted else None,
                "max": round(row["max"], 3) if counted else None,
            }
        return out


# Set when the service rejects the hybridSearch option, so later searches stop sending it.
_hybrid_option_rejected = [False]

# Shadow mode runs a sample of searches locally in the background and compares them with Azure AI Search.
_shadow_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="local-shadow")

# Search results kept for SEARCH_CACHE_SECONDS (off by default). Keyed on the normalized question, the concepts, the
# domains and the limit. Each entry remembers the Compute Units its query used, so /health can say how many a hit
# saved. Failed searches and searches that found nothing are never kept.
_query_cache = {}  # key -> (expires, result, compute_units or None)
_query_stats = {"hits": 0, "misses": 0, "saved": 0.0}
_query_lock = threading.Lock()
# Bumped every time the cache is cleared. A search remembers the number it started under and stores its answer only if
# the number is unchanged, so an answer computed from an index that has since been replaced is never kept.
_query_generation = [0]


def normalize_query(query):
    """Case, spacing and trailing punctuation do not change what is asked. Digits and words always do."""
    text = " ".join(str(query or "").lower().split())
    return text.strip(" \"'?!.,;:")


def _query_cache_clear():
    with _query_lock:
        _query_cache.clear()
        _query_generation[0] += 1


def query_cache_stats():
    with _query_lock:
        hits, misses, saved, entries = _query_stats["hits"], _query_stats["misses"], _query_stats["saved"], len(_query_cache)
    lookups = hits + misses
    return {
        "enabled": settings.search_cache_seconds > 0,
        "seconds": settings.search_cache_seconds,
        "max_entries": settings.search_cache_max_entries,
        "entries": entries,
        "hits": hits,
        "misses": misses,
        "hit_rate": round(hits / lookups, 3) if lookups else None,
        "compute_units_saved": round(saved, 3),
    }


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


class FacetUnavailable(RuntimeError):
    """The index will not return counts for the concepts field (it is not facetable)."""


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
        self.session = pooled_session()
        self.session.headers.update({"api-key": settings.search_api_key})
        self.local = LocalIndexManager()
        self.local.on_swap = _query_cache_clear  # a rebuilt in-memory index makes kept answers out of date

    @property
    def configured(self):
        return settings.search_configured

    def _url(self, suffix):
        return (f"{settings.search_endpoint}/indexes/{settings.search_index_name}/docs/"
                f"{suffix}?api-version={settings.search_api_version}")

    def _post(self, kind, payload, timeout, attempts=6, max_wait=60, label=None):
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
            _record_compute_units(label or ("write" if kind == "index" else "other"), response)
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
            _query_cache_clear()  # the index changed: answers kept on this instance may be out of date
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
        outcomes = []
        if settings.ai_search_writes:
            if not self.configured:
                raise RuntimeError("Azure AI Search is not configured.")
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
        self._local_write(docs)
        return outcomes

    def _local_write(self, docs, strict=False):
        """Write chunks to the Table Storage table the local index reads, then change its version row so every
        instance reloads. While Azure AI Search still takes the writes, a failure here is logged and the backfill can
        repair it; once Azure AI Search is off (or when strict), the failure is raised so the queue retries."""
        if not settings.local_index_write or not docs:
            return
        try:
            entities = [entity_from_search_doc(d) for d in docs if d.get(settings.search_vector_field) is not None]
            store = self.local.store()
            store.upsert_entities(entities)
            store.bump_version()
            _query_cache_clear()
        except Exception:
            if strict or not settings.ai_search_writes:
                raise
            log.warning("Could not write %d chunk(s) to the local index table; the local backfill can repair it",
                        len(docs), exc_info=True)

    def _local_delete(self, ids):
        if not settings.local_index_write or not ids:
            return
        try:
            store = self.local.store()
            store.delete_ids(ids)
            store.bump_version()
            _query_cache_clear()
        except Exception:
            if not settings.ai_search_writes:
                raise
            log.warning("Could not delete %d chunk(s) from the local index table", len(ids), exc_info=True)

    def _blob_text(self, doc_id):
        """Text of one chunk from memory or Blob Storage, or None when the blob is missing."""
        name = "documents/" + str(doc_id) + ".json"
        cached = _blob_cache_get(name)
        if cached is not None:
            _blob_stat("hits")
            return cached
        if BLOB_CACHE_SECONDS > 0:
            _blob_stat("misses")
        _blob_stat("storage_reads")
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
        """Blob-backed retag: re-upload whole documents (with their stored vectors) with their new concepts.

        Blob Storage holds every chunk, so the index is not asked which chunks it already has (that was one
        query per batch). A chunk that was not indexed yet is simply indexed now.
        """
        by_id = {str(d["id"]): d for d in (docs or []) if d.get("id")}
        todo = []
        for doc_id, concepts in updates:
            doc = by_id.get(str(doc_id))
            if doc:
                fresh = dict(doc)
                fresh["concepts"] = [str(c) for c in concepts]
                todo.append(fresh)
        if todo:
            vectors = self._vectors_for(todo)
            self._upload([self._search_doc(d, v) for d, v in zip(todo, vectors)])
        return len(todo), len(updates) - len(todo)

    def update_concepts(self, updates, docs=None):
        """Apply new concept tags to documents already in the index. No re-embedding of unchanged text.

        updates is a list of (document_id, concepts). Normally this uses the 'merge' action, so a
        document that is not indexed yet is skipped, never created as an empty stub. A blob-backed index
        stores no vector copy, so there the whole document is re-uploaded from `docs` instead.
        Returns (updated, not_indexed).
        """
        if settings.ai_search_writes and not self.configured:
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
        if settings.search_backend == "local":
            try:
                return self._fetch_chunks_local(url, chunk_id, neighbors, max_chars, timings)
            except LocalIndexUnavailable as exc:
                if not (settings.local_search_fallback and self.configured):
                    raise RuntimeError(f"The local search index is not available: {exc}") from exc
                self.local.record_fallback()
                log.warning("Local index not available (%s); reading the page from Azure AI Search", exc)
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
        }, 45, attempts=3, max_wait=5, label="page_chunks")
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

    def _fetch_chunks_local(self, url, chunk_id, neighbors, max_chars, timings):
        """fetch_chunks answered from the in-memory index. Same return value: (rows, focus, total_chunks)."""
        began = time.perf_counter()
        index = self.local.current()
        focus = None
        if chunk_id:
            try:
                page_id, focus = split_chunk_id(chunk_id)
            except ValueError:
                raise ValueError("chunk_id must look like <page>-<number>.") from None
            reach = max(0, min(int(neighbors), 3))
            wanted = [f"{page_id}-{i}" for i in range(max(0, focus - reach), focus + reach + 1)]
            order = [index.by_id[w] for w in wanted if w in index.by_id]
            total = len(index.pages.get(page_id, []))
        elif url:
            base = str(url).strip().split("#", 1)[0]
            variants = sorted({base, base.rstrip("/"), base.rstrip("/") + "/"} - {"", "/"})
            seen = {}
            for variant in variants:
                for i in index.by_url.get(variant, []):
                    seen[i] = True
            order = sorted(seen, key=lambda i: chunk_index(index.ids[i]))
            total = len(order)
            order = order[:400]  # a page this long is cut off at 400 chunks, as before
        else:
            raise ValueError("Provide url or chunk_id.")
        if timings is not None:
            timings["index_query_ms"] = round((time.perf_counter() - began) * 1000)
        began = time.perf_counter()
        texts = self._local_texts(index, order, None if chunk_id else max_chars)
        rows = []
        for i in order:
            row = index.row_dict(i)
            row["content"] = texts.get(i, "")
            rows.append(row)
        if timings is not None:
            timings["blob_ms"] = round((time.perf_counter() - began) * 1000)
        return rows, focus, (total if rows else None)

    def local_backfill(self, blob_names, domains=None, workers=8):
        """Copy chunks from Blob Storage into the local index table, without touching Azure AI Search.

        Stored vectors are reused (a chunk with no stored vector is embedded, as the AI Search backfill would). Raises on
        any failure, so the queue retries the batch. Returns counts."""
        only = {str(x).lower() for x in (domains or []) if str(x).strip()}
        docs = []
        for name in blob_names:
            doc = self.storage.get(name)
            if doc and doc.get("id") and doc.get("content"):
                if only and str(doc.get("domain") or "").lower() not in only:
                    continue
                docs.append(doc)
        if not docs:
            return {"read": len(blob_names), "written": 0}
        groups = [docs[i:i + 25] for i in range(0, len(docs), 25)]
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(groups)))) as pool:
            vector_sets = list(pool.map(self._vectors_for, groups))
        search_docs = []
        for group, vectors in zip(groups, vector_sets):
            search_docs.extend(self._search_doc(d, v) for d, v in zip(group, vectors))
        self._local_write(search_docs, strict=True)
        return {"read": len(blob_names), "written": len(search_docs)}

    def delete_ids(self, ids):
        """Delete documents from the index by id. Deleting an id that is not there is not an error."""
        ids = [str(x) for x in ids if x]
        if not ids:
            return 0
        if settings.ai_search_writes and self.configured:
            results = self._write_batch([{"@search.action": "delete", "id": doc_id} for doc_id in ids])
            failed = [x for x in results if not x.get("status")]
            if failed:
                raise RuntimeError(f"Azure AI Search could not delete {len(failed)} document(s): {failed[:3]}")
        elif not settings.local_index_write:
            return 0
        self._local_delete(ids)
        return len(ids)

    def delete_chunks(self, page_id, start, end):
        """Delete chunks page_id-start up to page_id-(end-1) by id: index entries, blobs and vectors.

        Chunk ids are deterministic (<page hash>-<index>), so no index query is needed to find them.
        An id that is not there is not an error.
        """
        ids = [f"{page_id}-{i}" for i in range(int(start), int(end))]
        if not ids:
            return 0
        self.delete_ids(ids)
        for doc_id in ids:
            name = "documents/" + doc_id + ".json"
            try:
                self.storage.delete(name)
                _blob_cache_forget(name)
                self.storage.delete(self.vector_key(doc_id))
            except Exception:
                log.warning("Could not delete stored blob for chunk %s", doc_id, exc_info=True)
        return len(ids)

    def delete_page(self, url, chunk_count=None):
        """Remove every chunk of a page. With a known chunk_count this deletes by id and runs no
        index query. Without one it falls back to delete_url, which does run a query."""
        if chunk_count:
            page_id = hashlib.sha256(str(url).encode()).hexdigest()
            return self.delete_chunks(page_id, 0, int(chunk_count))
        return self.delete_url(url)

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

    def uses_local_admin(self):
        """True when the table, not Azure AI Search, is the index of record: the counts, tags and checks below read it."""
        return settings.search_backend == "local" or not settings.ai_search_writes

    def _existing_ids(self, ids):
        """Which of these chunk ids are already in the index."""
        ids = [str(x) for x in ids]
        if not settings.ai_search_writes:
            return self.local.store().existing_ids(ids)
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

    def search(self, query, concepts=None, domains=None, limit=5, label="query"):
        concepts = sorted({str(x).lower() for x in (concepts or [])})
        domains = sorted({str(x).lower() for x in (domains or [])})
        if not concepts or not domains:
            raise ValueError("Search requires ontology-resolved concepts and domains.")
        backend = settings.search_backend
        if backend != "local" and not self.configured:
            raise RuntimeError("Azure AI Search is not configured.")
        cache_key, generation = None, 0
        if backend == "local" and settings.search_cache_seconds > 0:
            try:
                # A first load swaps the index in, and a swap clears the kept answers. Let that happen before this search
                # notes the cache generation, so its own answer (from the new index) can still be kept.
                self.local.current()
            except LocalIndexUnavailable:
                pass  # the search below reports it, or falls back to Azure AI Search
        if settings.search_cache_seconds > 0:
            cache_key = (normalize_query(query), tuple(concepts), tuple(domains), int(limit))
            now = time.monotonic()
            with _query_lock:
                generation = _query_generation[0]
                entry = _query_cache.get(cache_key)
                if entry and entry[0] > now:
                    _query_stats["hits"] += 1
                    _query_stats["saved"] += entry[2] or 0.0
                else:
                    entry = None
                    _query_stats["misses"] += 1
            if entry is not None:
                cached = copy.deepcopy(entry[1])  # entries are never changed once stored, so copy outside the lock
                cached["query"] = query  # the caller's own wording, not the first asker's
                return cached
        answer, units = None, None
        if backend == "local":
            try:
                answer = self._search_local(query, concepts, domains, limit)
            except LocalIndexUnavailable as exc:
                if not (settings.local_search_fallback and self.configured):
                    raise RuntimeError(f"The local search index is not available: {exc}") from exc
                self.local.record_fallback()
                log.warning("Local index not available (%s); answering from Azure AI Search", exc)
        if answer is None:
            answer, units = self._search_azure(query, concepts, domains, limit, label)
            if backend == "shadow":
                self._maybe_shadow(query, concepts, domains, limit, answer)
        # A keyword-only answer (the question could not be embedded) is a degraded one: it must not be kept for minutes.
        if cache_key is not None and answer.get("results") and answer.get("search_mode") != "keyword":
            snapshot = copy.deepcopy(answer)  # copied outside the lock
            with _query_lock:
                if generation == _query_generation[0]:  # the index did not change while this search ran
                    _query_cache[cache_key] = (time.monotonic() + settings.search_cache_seconds, snapshot, units)
                    limit_entries = settings.search_cache_max_entries
                    if len(_query_cache) > limit_entries:
                        for old in list(_query_cache)[: max(1, limit_entries // 10)]:
                            _query_cache.pop(old, None)
        return answer

    def _search_azure(self, query, concepts, domains, limit, label):
        """The Azure AI Search hybrid query. Returns (answer, Compute Units the query used or None)."""
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
                "k": max(limit, settings.search_vector_k) if settings.search_vector_k else max(50, limit * 10),
            }],
            "select": "id,title,url,domain,concepts" if settings.search_blob_backed else "id,title,url,domain,concepts,content",
            "top": limit,
        }
        if settings.search_count:
            body["count"] = True
        if settings.search_fields:
            body["searchFields"] = settings.search_fields
        if settings.search_max_text_recall and not _hybrid_option_rejected[0]:
            body["hybridSearch"] = {"maxTextRecallSize": settings.search_max_text_recall}
        response = self._post("search", body, 45, attempts=3, max_wait=5, label=label)
        if response.status_code == 400 and "hybridSearch" in body:
            # An API version or service that does not accept the option must not break search.
            log.warning("Azure AI Search rejected hybridSearch.maxTextRecallSize; searching without it")
            _hybrid_option_rejected[0] = True
            body.pop("hybridSearch")
            response = self._post("search", body, 45, attempts=3, max_wait=5, label=label)
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
        answer = {
            "query": query,
            "count": len(results),
            "total_matches": payload.get("@odata.count"),
            "search_mode": "hybrid",
            "embedding_enabled": True,
            "results": results,
        }
        try:
            units = float(response.headers.get("x-ms-azs-compute-units-consumed"))
        except (TypeError, ValueError):
            units = None
        return answer, units

    def _local_texts(self, index, rows, max_chars=None, batch=8):
        """{row: text} for these rows, in the order given. With max_chars, stop once that much text is loaded."""
        out, loaded, position = {}, 0, 0
        while position < len(rows):
            if max_chars is not None and loaded >= max_chars:
                break
            group = rows[position:position + batch]
            if index.texts is not None:
                texts = [index.texts[i] for i in group]
            else:
                store = self.local.store()
                with ThreadPoolExecutor(max_workers=max(1, min(batch, len(group)))) as pool:
                    texts = [t or "" for t in pool.map(lambda i: store.get_text(index.ids[i]), group)]
            for i, text in zip(group, texts):
                out[i] = text
                loaded += len(text)
            position += batch
        return out

    def _search_local(self, query, concepts, domains, limit):
        """The same hybrid search, answered from the in-memory index. Raises LocalIndexUnavailable when it is not ready."""
        index = self.local.current()
        terms = query_terms(query)
        began = time.perf_counter()
        qcode, keyword_only = None, False
        try:
            get_code = getattr(self.embedder, "embed_query_code", None)
            qcode = get_code(query) if get_code else binary_code(self.embedder.embed_query(query))
        except Exception as exc:
            keyword_only = True
            log.warning("Search question could not be embedded (%s); searching by keywords only", type(exc).__name__)
        embed_ms = (time.perf_counter() - began) * 1000
        began = time.perf_counter()
        candidates = max(limit, settings.search_vector_k) if settings.search_vector_k else max(50, limit * 10)
        hits, matching = index.search(terms, qcode, domains, concepts, candidates=candidates, limit=limit)
        texts = self._local_texts(index, [row for row, _ in hits])
        results = []
        for row, score in hits:
            data = index.row_dict(row)
            results.append({
                "title": data["title"],
                "url": data["url"],
                "snippet": (texts.get(row) or "")[:700],
                "domain": data["domain"],
                "concepts": data["concepts"],
                "score": round(float(score), 6),
                "source": "website",
                "chunk_id": data["id"],
            })
        self.local.record_search(embed_ms, (time.perf_counter() - began) * 1000, keyword_only)
        return {
            "query": query,
            "count": len(results),
            "total_matches": matching,
            "search_mode": "keyword" if keyword_only else "hybrid",
            "embedding_enabled": not keyword_only,
            "backend": "local",
            "results": results,
        }

    def _maybe_shadow(self, query, concepts, domains, limit, azure_answer):
        """Run a sample of searches locally too, in the background, and record how closely they agree."""
        sample = settings.local_shadow_sample
        if sample <= 0 or random.random() >= sample or not azure_answer.get("results"):
            return

        def work():
            try:
                local = self._search_local(query, concepts, domains, limit)
                a = [r["chunk_id"] for r in azure_answer["results"]]
                b = [r["chunk_id"] for r in local["results"]]
                self.local.record_shadow(len(set(a) & set(b)) / max(1, len(a)), bool(a and b and a[0] == b[0]))
            except Exception:
                self.local.record_shadow_failure()

        _shadow_pool.submit(work)

    @staticmethod
    def _domain_filter(domain):
        return "domain eq '" + str(domain).strip().lower().replace("'", "''") + "'" if domain else None

    def tag_counts(self, domain=None, top=200):
        """Chunks per ontology tag (the concepts field) from one facet query, plus the untagged chunks.

        Without a domain it also returns chunks per domain. Raises FacetUnavailable when the index does not
        allow faceting on the concepts field.
        """
        if self.uses_local_admin():
            return self._local_tag_counts(domain, top)
        if not self.configured:
            raise RuntimeError("Azure AI Search is not configured.")
        flt = self._domain_filter(domain)
        concept_facet = f"concepts,count:{max(1, min(int(top), 1000))}"
        attempts = [[concept_facet]] if flt else [[concept_facet, "domain,count:100"], [concept_facet]]
        payload = None
        for facets in attempts:
            body = {"search": "*", "top": 0, "count": True, "facets": facets}
            if flt:
                body["filter"] = flt
            response = self._post("search", body, 45, attempts=3, max_wait=5)
            if response.status_code == 400:
                last_error = response.text[:300]
                continue
            response.raise_for_status()
            payload = response.json()
            break
        if payload is None:
            raise FacetUnavailable(last_error)
        found = payload.get("@search.facets") or {}
        counted = self._post("search", {
            "search": "*", "top": 0, "count": True,
            "filter": "not concepts/any()" + (f" and {flt}" if flt else ""),
        }, 45, attempts=3, max_wait=5)
        counted.raise_for_status()
        return {
            "source": "index",
            "total_chunks": payload.get("@odata.count"),
            "untagged_chunks": counted.json().get("@odata.count"),
            "tags": [{"tag": f.get("value"), "chunks": f.get("count")} for f in found.get("concepts", [])],
            "domains": [{"domain": f.get("value"), "chunks": f.get("count")} for f in found.get("domain", [])],
        }

    def count_chunks(self, domain=None):
        """How many chunks the index holds (for one domain, when given): one count query."""
        if self.uses_local_admin():
            index = self.local.current()
            return index.n if not domain else int((index.domain_codes == self._domain_code(index, domain)).sum())
        if not self.configured:
            raise RuntimeError("Azure AI Search is not configured.")
        body = {"search": "*", "top": 0, "count": True}
        flt = self._domain_filter(domain)
        if flt:
            body["filter"] = flt
        response = self._post("search", body, 45, attempts=3, max_wait=5)
        response.raise_for_status()
        return int(response.json().get("@odata.count") or 0)

    def sample_tag_counts(self, domain=None, scan=3000, population=None):
        """Chunks per tag estimated from a sample of the stored chunk blobs.

        The index cannot count by the concepts field (it is not facetable), but every stored chunk carries its tags.
        Blob names start with a hash, so the first `scan` names are an unbiased sample of all chunks. Shares are
        measured on the sample and scaled to `population`, the number of chunks the index holds (for the domain, when
        one is given). margin_points is the worst-case 95% margin on a share, in percentage points.
        """
        scan = max(100, min(int(scan), 5000))
        names, _ = self.storage.list_blob_name_page("documents/", page_size=scan)
        want = str(domain).strip().lower() if domain else None

        def read(name):
            doc = self.storage.get(name) or {}
            return str(doc.get("domain") or "").lower(), doc.get("concepts") or []

        with ThreadPoolExecutor(max_workers=16) as pool:
            rows = list(pool.map(read, names))
        tags, hosts, matched, untagged = {}, {}, 0, 0
        for host, concepts in rows:
            hosts[host] = hosts.get(host, 0) + 1
            if want and host != want:
                continue
            matched += 1
            untagged += 0 if concepts else 1
            for concept in concepts:
                tags[str(concept)] = tags.get(str(concept), 0) + 1
        total = int(population) if population is not None else matched
        scale = (total / matched) if matched else 0
        ranked = lambda counts, key: [
            {key: k, "chunks": round(v * scale), "sample_chunks": v}
            for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        return {
            "source": "blob_sample", "scanned": len(names), "sample_chunks": matched,
            "total_chunks": total, "untagged_chunks": round(untagged * scale),
            "margin_points": round(98 / (matched ** 0.5), 1) if matched else None,
            "tags": ranked(tags, "tag"),
            "domains": [] if want else [
                {"domain": k, "chunks": round(v * (total / len(names))) if names else 0}
                for k, v in sorted(hosts.items(), key=lambda kv: (-kv[1], kv[0]))
            ],
        }

    def pages_with_tag(self, tag, domain=None, limit=50):
        """The pages that carry one tag, with how many of their chunks have it. Reads up to 1000 matching chunks."""
        if self.uses_local_admin():
            return self._local_pages_with_tag(tag, domain, limit)
        if not self.configured:
            raise RuntimeError("Azure AI Search is not configured.")
        flt = "concepts/any(c: c eq '" + str(tag).strip().lower().replace("'", "''") + "')"
        domain_filter = self._domain_filter(domain)
        if domain_filter:
            flt += " and " + domain_filter
        response = self._post("search", {
            "search": "*", "filter": flt, "select": "id,title,url,domain", "top": 1000, "count": True,
        }, 45, attempts=3, max_wait=5)
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("value", [])
        pages = {}
        for row in rows:
            key = row.get("url") or ""
            page = pages.setdefault(key, {"url": key, "title": row.get("title"), "domain": row.get("domain"), "chunks": 0})
            page["chunks"] += 1
        ranked = sorted(pages.values(), key=lambda p: (-p["chunks"], p["url"]))
        return {
            "tag": str(tag).strip().lower(), "chunks_matched": payload.get("@odata.count"), "chunks_read": len(rows),
            "pages_found": len(ranked), "pages": ranked[:max(1, int(limit))],
        }

    def page_tags(self, url):
        """The tags on each chunk of one page, read from the index (no chunk text)."""
        if self.uses_local_admin():
            return self._local_page_tags(url)
        if not self.configured:
            raise RuntimeError("Azure AI Search is not configured.")
        base = str(url).strip().split("#", 1)[0]
        variants = sorted({base, base.rstrip("/"), base.rstrip("/") + "/"} - {"", "/"})
        joined = "|".join(v.replace("'", "''") for v in variants)
        response = self._post("search", {
            "search": "*", "filter": f"search.in(url, '{joined}', '|')", "top": 1000,
            "select": "id,title,url,domain,concepts",
        }, 45, attempts=3, max_wait=5)
        response.raise_for_status()
        rows = sorted(response.json().get("value", []), key=lambda r: chunk_index(r.get("id")))
        return {
            "url": rows[0].get("url") if rows else base, "title": rows[0].get("title") if rows else None,
            "domain": rows[0].get("domain") if rows else None,
            "chunks": [{"chunk": chunk_index(r.get("id")), "tags": r.get("concepts") or []} for r in rows],
        }

    def delete_url(self, url):
        if not settings.ai_search_writes and settings.local_index_write:
            # The table is the index now: a page's chunks all share one partition, named by the hash of its url.
            ids = self.local.store().page_ids(hashlib.sha256(str(url).encode()).hexdigest())
            if not ids:
                return 0
            self._local_delete(ids)
            for doc_id in ids:
                self.storage.delete("documents/" + str(doc_id) + ".json")
                _blob_cache_forget("documents/" + str(doc_id) + ".json")
                self.storage.delete(self.vector_key(doc_id))
            return len(ids)
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
        self._local_delete(ids)
        for doc_id in ids:
            self.storage.delete("documents/" + str(doc_id) + ".json")
            _blob_cache_forget("documents/" + str(doc_id) + ".json")
            self.storage.delete(self.vector_key(doc_id))
        return len(ids)

    def status(self):
        if self.uses_local_admin():
            # The table is the index: count what it holds now (a read of keys and domains, no text).
            out = {"configured": True, "index": settings.local_index_table, "source": "local_index_table",
                   "embedding_configured": settings.embedding_configured}
            try:
                out["document_count"] = self.local.store().count()
            except Exception as exc:
                out["document_count"] = None
                out["error"] = f"{type(exc).__name__}: {exc}"[:200]
        else:
            out = self._status_azure()
        if settings.search_backend != "azure" or settings.local_index_write:
            out["local_index"] = self.local.describe()
        return out

    # ---- the same admin reads, answered from the in-memory index ----
    @staticmethod
    def _domain_code(index, domain):
        want = str(domain).strip().lower()
        return index.domain_names.index(want) if want in index.domain_names else -1

    def _local_rows(self, index, domain=None):
        if not domain:
            return range(index.n)
        code = self._domain_code(index, domain)
        return [int(i) for i in (index.domain_codes == code).nonzero()[0]]

    def _local_tag_counts(self, domain=None, top=200):
        index = self.local.current()
        rows = self._local_rows(index, domain)
        tags, hosts, untagged = {}, {}, 0
        for i in rows:
            concepts = index.concepts[i]
            untagged += 0 if concepts else 1
            for c in concepts:
                tags[c] = tags.get(c, 0) + 1
            if not domain:
                host = index.domain_names[index.domain_codes[i]]
                hosts[host] = hosts.get(host, 0) + 1
        rank = lambda counts, key, limit: [{key: k, "chunks": v} for k, v in
                                           sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]
        return {
            "source": "local_index",
            "total_chunks": len(rows),
            "untagged_chunks": untagged,
            "tags": rank(tags, "tag", max(1, min(int(top), 1000))),
            "domains": rank(hosts, "domain", 100),
        }

    def _local_pages_with_tag(self, tag, domain=None, limit=50):
        index = self.local.current()
        want = str(tag).strip().lower()
        pages, matched = {}, 0
        for i in self._local_rows(index, domain):
            if want in {c.lower() for c in index.concepts[i]}:
                matched += 1
                page = pages.setdefault(index.urls[i], {"url": index.urls[i], "title": index.titles[i],
                                                        "domain": index.domain_names[index.domain_codes[i]], "chunks": 0})
                page["chunks"] += 1
        ranked = sorted(pages.values(), key=lambda p: (-p["chunks"], p["url"]))
        return {"tag": want, "chunks_matched": matched, "chunks_read": matched, "pages_found": len(ranked),
                "pages": ranked[:max(1, int(limit))]}

    def _local_page_tags(self, url):
        index = self.local.current()
        base = str(url).strip().split("#", 1)[0]
        variants = sorted({base, base.rstrip("/"), base.rstrip("/") + "/"} - {"", "/"})
        seen = {}
        for variant in variants:
            for i in index.by_url.get(variant, []):
                seen[i] = True
        rows = sorted(seen, key=lambda i: chunk_index(index.ids[i]))
        first = rows[0] if rows else None
        return {
            "url": index.urls[first] if rows else base,
            "title": index.titles[first] if rows else None,
            "domain": index.domain_names[index.domain_codes[first]] if rows else None,
            "chunks": [{"chunk": chunk_index(index.ids[i]), "tags": list(index.concepts[i])} for i in rows],
        }

    def _status_azure(self):
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
