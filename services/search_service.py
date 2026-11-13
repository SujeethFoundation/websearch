"""The search service: chunk indexing and hybrid retrieval over the local index; Blob Storage remains canonical.

Chunks and their vectors are written to Blob Storage first (the durable, rebuildable copy), then to the Table Storage
table every instance loads into memory (services/local_index.py). A search runs on the instance that receives it.
"""
import base64
import copy
import hashlib
import logging
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from services.config import settings
from services.embedding_service import EmbeddingService
from services.local_index import (
    LocalIndexManager, LocalIndexUnavailable, VECTOR_FIELD, binary_code, entity_from_search_doc, query_terms,
    split_chunk_id,
)

log = logging.getLogger("sujeeth-web-search.search")

# Chunks are embedded once. The vector is stored next to the chunk (vectors/<id>.json) so a busy or
# failed index table never causes a second embedding call. EMBED_BATCH_SIZE caps texts per embedding request.
EMBED_BATCH_SIZE = settings.embed_batch_size
# After the index table stays busy or unreachable, other writes on this instance skip straight to deferral for this
# many seconds, so workers keep crawling instead of each waiting out the failure.
SEARCH_BREAKER_SECONDS = settings.search_breaker_seconds
_breaker_until = 0.0

# Search results kept for SEARCH_CACHE_SECONDS (off by default). Keyed on the normalized question, the concepts, the
# domains and the limit. Failed searches and searches that found nothing are never kept.
_query_cache = {}  # key -> (expires, result)
_query_stats = {"hits": 0, "misses": 0}
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
        hits, misses, entries = _query_stats["hits"], _query_stats["misses"], len(_query_cache)
    lookups = hits + misses
    return {
        "enabled": settings.search_cache_seconds > 0,
        "seconds": settings.search_cache_seconds,
        "max_entries": settings.search_cache_max_entries,
        "entries": entries,
        "hits": hits,
        "misses": misses,
        "hit_rate": round(hits / lookups, 3) if lookups else None,
    }


class IndexBusyError(RuntimeError):
    """The index table stayed busy or unreachable (throttled, timed out, or a server error)."""


def _is_transient(exc):
    """True for the failures worth trying again later: throttling, timeouts, server errors, dropped connections."""
    if getattr(exc, "status_code", None) in (408, 429, 500, 502, 503, 504):
        return True
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    return exc.__class__.__name__ in ("ServiceRequestError", "ServiceResponseError", "ServerBusy", "ConnectTimeout", "ReadTimeout")


def _breaker_open():
    return time.monotonic() < _breaker_until


def _breaker_trip():
    global _breaker_until
    _breaker_until = time.monotonic() + SEARCH_BREAKER_SECONDS


def _breaker_clear():
    global _breaker_until
    _breaker_until = 0.0


class SearchService:
    def __init__(self, storage):
        self.storage = storage
        self.embedder = EmbeddingService()
        self.local = LocalIndexManager()
        self.local.on_swap = _query_cache_clear  # a rebuilt in-memory index makes kept answers out of date

    @staticmethod
    def _search_doc(doc, vector=None):
        """The document written to the index table: the chunk's fields and, when given, its vector."""
        item = {
            "id": str(doc["id"]),
            "title": str(doc.get("title") or ""),
            "url": str(doc.get("url") or ""),
            "domain": str(doc.get("domain") or "").lower(),
            "concepts": [str(x) for x in (doc.get("concepts") or [])],
            "content": str(doc.get("content") or ""),
        }
        if vector is not None:
            item[VECTOR_FIELD] = vector
        return item

    def _upload(self, docs):
        """Write chunks (documents that carry their vectors) to the index table."""
        self._local_write(docs)

    def _local_write(self, docs):
        """Write chunks to the Table Storage table the index reads, then change its version row so every instance
        reloads. Throttling, timeouts and server errors raise IndexBusyError, so the caller can defer the work."""
        if not docs:
            return
        if not self.local.enabled:
            raise RuntimeError("The local index table is not configured: set LOCAL_INDEX_CONNECTION_STRING.")
        try:
            entities = [entity_from_search_doc(d) for d in docs if d.get(VECTOR_FIELD) is not None]
            store = self.local.store()
            store.upsert_entities(entities)
            store.bump_version()
            _query_cache_clear()
        except Exception as exc:
            if _is_transient(exc):
                raise IndexBusyError(f"The index table is busy or unreachable ({exc.__class__.__name__}).") from exc
            raise

    def _local_delete(self, ids):
        if not ids:
            return
        if not self.local.enabled:
            raise RuntimeError("The local index table is not configured: set LOCAL_INDEX_CONNECTION_STRING.")
        try:
            store = self.local.store()
            store.delete_ids(ids)
            store.bump_version()
            _query_cache_clear()
        except Exception as exc:
            if _is_transient(exc):
                raise IndexBusyError(f"The index table is busy or unreachable ({exc.__class__.__name__}).") from exc
            raise

    def _rewrite_existing(self, updates, docs):
        """Retag: write whole documents (with their stored vectors) with their new concepts.

        Blob Storage holds every chunk, so the table is not asked which chunks it already has. A chunk that was not
        indexed yet is simply indexed now.
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
        """Apply new concept tags to chunks. No re-embedding of unchanged text: stored vectors are reused.

        updates is a list of (document_id, concepts); docs are the stored documents they belong to. Returns
        (updated, not_found), where not_found counts updates with no stored document to rewrite.
        """
        return self._rewrite_existing(updates, docs)

    def fetch_chunks(self, url=None, chunk_id=None, neighbors=1, max_chars=None, timings=None):
        """Read stored chunks of one page from the index.

        With chunk_id, returns that chunk and up to `neighbors` chunks on each side. With
        url, returns every chunk of the page. Returns (rows, focus_index, total_chunks).
        """
        try:
            return self._fetch_chunks_local(url, chunk_id, neighbors, max_chars, timings)
        except LocalIndexUnavailable as exc:
            raise RuntimeError(f"The local search index is not available: {exc}") from exc

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

    def delete_ids(self, ids):
        """Delete chunks from the index by id. Deleting an id that is not there is not an error."""
        ids = [str(x) for x in ids if x]
        if not ids:
            return 0
        self._local_delete(ids)
        return len(ids)

    def delete_chunks(self, page_id, start, end):
        """Delete chunks page_id-start up to page_id-(end-1) by id: index rows, blobs and vectors.

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
                self.storage.delete(self.vector_key(doc_id))
            except Exception:
                log.warning("Could not delete stored blob for chunk %s", doc_id, exc_info=True)
        return len(ids)

    def delete_page(self, url, chunk_count=None):
        """Remove every chunk of a page. With a known chunk_count this deletes by id and runs no
        index query. Without one it falls back to delete_url, which reads the page's rows from the table."""
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

        Returns (indexed_count, deferred_blob_names). If the index table stays busy, nothing is
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
        names = ["documents/" + str(doc["id"]) + ".json" for doc in documents]
        if _breaker_open():
            return 0, names
        try:
            self._upload(search_docs)
        except IndexBusyError as exc:
            _breaker_trip()
            log.warning("Index table busy; %d chunk(s) stored and queued for later indexing: %s", len(names), exc)
            return 0, names
        _breaker_clear()
        return len(search_docs), []

    def upsert_many(self, documents, persist_blobs=True):
        count, deferred = self.upsert_or_defer(documents, persist_blobs)
        if deferred:
            raise IndexBusyError(f"The index table is busy; {len(deferred)} chunk(s) are stored but not indexed.")
        return count

    def _existing_ids(self, ids):
        """Which of these chunk ids are already in the index table (one keys-only read per page)."""
        try:
            return self.local.store().existing_ids([str(x) for x in ids])
        except Exception as exc:
            if _is_transient(exc):
                raise IndexBusyError(f"The index table was busy or unreachable ({exc.__class__.__name__}) while checking existing chunks.") from exc
            raise

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
    def ranking_settings(overrides=None):
        """How the local search ranks beyond the plain fusion: the app's settings, changed by `overrides` for one request
        ({"concept_boost": 2, "phrase_boost": 0, "max_per_page": 1, "unique_pages": false})."""
        out = {"concept_boost": settings.local_concept_boost, "phrase_boost": settings.local_phrase_boost,
               "max_per_page": settings.local_max_per_page, "unique_pages": settings.search_unique_pages}
        for key, value in (overrides or {}).items():
            if key in out:
                try:
                    if key == "unique_pages":
                        out[key] = (str(value).strip().lower() in ("1", "true", "yes", "on")) if isinstance(value, str) else bool(value)
                    else:
                        out[key] = max(0, int(value)) if key == "max_per_page" else max(0.0, float(value))
                except (TypeError, ValueError):
                    pass
        return out

    def search(self, query, concepts=None, domains=None, limit=5, concept_weights=None, ranking=None):
        """One search over the in-memory index. concept_weights ({concept: weight}) names the concepts the question itself
        matched, which the search ranks higher (see LOCAL_CONCEPT_BOOST). ranking changes the ranking settings for this
        one search (see ranking_settings), for trying values out."""
        concepts = sorted({str(x).lower() for x in (concepts or [])})
        domains = sorted({str(x).lower() for x in (domains or [])})
        if not concepts or not domains:
            raise ValueError("Search requires ontology-resolved concepts and domains.")
        concept_weights = {str(c).lower(): float(w) for c, w in (concept_weights or {}).items() if w and float(w) > 0}
        cache_key, generation = None, 0
        if settings.search_cache_seconds > 0:
            try:
                # A first load swaps the index in, and a swap clears the kept answers. Let that happen before this search
                # notes the cache generation, so its own answer (from the new index) can still be kept.
                self.local.current()
            except LocalIndexUnavailable:
                pass  # the search below reports it
            cache_key = (normalize_query(query), tuple(concepts), tuple(domains), int(limit),
                         tuple(sorted(concept_weights.items())),
                         tuple(sorted((str(k), str(v)) for k, v in (ranking or {}).items())))
            now = time.monotonic()
            with _query_lock:
                generation = _query_generation[0]
                entry = _query_cache.get(cache_key)
                if entry and entry[0] > now:
                    _query_stats["hits"] += 1
                else:
                    entry = None
                    _query_stats["misses"] += 1
            if entry is not None:
                cached = copy.deepcopy(entry[1])  # entries are never changed once stored, so copy outside the lock
                cached["query"] = query  # the caller's own wording, not the first asker's
                return cached
        try:
            answer = self._search_local(query, concepts, domains, limit, concept_weights=concept_weights, ranking=ranking)
        except LocalIndexUnavailable as exc:
            raise RuntimeError(f"The local search index is not available: {exc}") from exc
        # A keyword-only answer (the question could not be embedded) is a degraded one: it must not be kept for minutes.
        if cache_key is not None and answer.get("results") and answer.get("search_mode") != "keyword":
            snapshot = copy.deepcopy(answer)  # copied outside the lock
            with _query_lock:
                if generation == _query_generation[0]:  # the index did not change while this search ran
                    _query_cache[cache_key] = (time.monotonic() + settings.search_cache_seconds, snapshot)
                    limit_entries = settings.search_cache_max_entries
                    if len(_query_cache) > limit_entries:
                        for old_key in list(_query_cache)[: max(1, limit_entries // 10)]:
                            _query_cache.pop(old_key, None)
        return answer

    def _local_texts(self, index, rows, max_chars=None, batch=8):
        """{row: text} for these rows, in the order given. With max_chars, stop once that much text is loaded."""
        out, loaded, position = {}, 0, 0
        while position < len(rows):
            if max_chars is not None and loaded >= max_chars:
                break
            group = rows[position:position + batch]
            # A chunk's text is in memory unless its domain keeps text in the table (None) or no text is in memory at all.
            missing = [i for i in group if index.texts is None or index.texts[i] is None]
            fetched = {}
            if missing:
                store = self.local.store()
                with ThreadPoolExecutor(max_workers=max(1, min(batch, len(missing)))) as pool:
                    for i, t in zip(missing, pool.map(lambda i: store.get_text(index.ids[i]), missing)):
                        fetched[i] = t or ""
            for i in group:
                text = fetched[i] if i in fetched else index.texts[i]
                out[i] = text
                loaded += len(text)
            position += batch
        return out

    def _search_local(self, query, concepts, domains, limit, concept_weights=None, ranking=None):
        """The hybrid search, answered from the in-memory index. Raises LocalIndexUnavailable when it is not ready."""
        how = self.ranking_settings(ranking)
        boost, per_page, unique = how["concept_boost"], how["max_per_page"], how["unique_pages"]
        index = self.local.current()
        terms = query_terms(query)
        began = time.perf_counter()
        qcode, qvec, keyword_only = self._embed_query(query)
        embed_ms = (time.perf_counter() - began) * 1000
        began = time.perf_counter()
        candidates = max(limit, settings.search_vector_k) if settings.search_vector_k else max(50, limit * 10)
        hits, matching = index.search(terms, qcode, domains, concepts, candidates=candidates, limit=limit, qvec=qvec,
                                      text_candidates=settings.local_text_recall or 1000,
                                      concept_weights=concept_weights if boost > 0 else None, concept_boost=boost,
                                      max_per_page=per_page, question=query, phrase_boost=how["phrase_boost"],
                                      phrase_pool=settings.local_phrase_pool, unique_pages=unique)
        answer = self._answer(index, query, hits, matching, keyword_only, concept_weights, how)
        self.local.record_search(embed_ms, (time.perf_counter() - began) * 1000, keyword_only,
                                 rescored=bool(qvec is not None and index.vec8 is not None and not keyword_only))
        return answer

    def _embed_query(self, query):
        """(code, vector, keyword_only) for a question. When it cannot be embedded the search goes on by keywords only."""
        qcode, qvec, keyword_only = None, None, False
        try:
            get_vectors = getattr(self.embedder, "embed_query_vectors", None)
            if get_vectors:
                qcode, qvec = get_vectors(query)
            else:
                get_code = getattr(self.embedder, "embed_query_code", None)
                qcode = get_code(query) if get_code else binary_code(self.embedder.embed_query(query))
        except Exception as exc:
            keyword_only = True
            log.warning("Search question could not be embedded (%s); searching by keywords only", type(exc).__name__)
        return qcode, qvec, keyword_only

    def _answer(self, index, query, hits, matching, keyword_only, concept_weights, how):
        """The answer a search returns, from the rows it picked."""
        boost, per_page, unique = how["concept_boost"], how["max_per_page"], how["unique_pages"]
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
        return {
            "query": query,
            "count": len(results),
            "total_matches": matching,
            "search_mode": "keyword" if keyword_only else "hybrid",
            "embedding_enabled": not keyword_only,
            "ranking": {"question_concepts": dict(sorted((concept_weights or {}).items())) if boost > 0 else {},
                        "concept_boost": boost, "phrase_boost": how["phrase_boost"], "max_per_page": per_page,
                        "unique_pages": unique},
            "results": results,
        }

    def search_many(self, query, jobs):
        """Several searches for one question in one pass over the in-memory index.

        jobs is a list of dicts with the arguments of search() that differ: concepts, domains, limit and, optionally,
        concept_weights and ranking. Returns one entry per job, in order: the answer search() gives for it, or the
        exception that job raised (invalid arguments, a failure reading text), so a side search that fails does not
        take the main one with it. A job that is just like one asked a moment ago is answered from the search cache
        when it is on. What the jobs share is worked out once: the embedding of the question, the keyword scores, the
        question's word pairs and the code distances. Raises RuntimeError, as search() does, when the index is not
        ready, and LocalIndexLoading while this instance is still loading it."""
        plans = []
        for job in jobs:
            try:
                concepts = sorted({str(x).lower() for x in (job.get("concepts") or [])})
                domains = sorted({str(x).lower() for x in (job.get("domains") or [])})
                if not concepts or not domains:
                    raise ValueError("Search requires ontology-resolved concepts and domains.")
                weights = {str(c).lower(): float(w) for c, w in (job.get("concept_weights") or {}).items() if w and float(w) > 0}
                plans.append({"concepts": concepts, "domains": domains, "limit": int(job.get("limit", 5)),
                              "weights": weights, "ranking": job.get("ranking"), "key": None})
            except Exception as exc:
                plans.append(exc)
        answers = [p if isinstance(p, Exception) else None for p in plans]
        todo = [i for i, p in enumerate(plans) if not isinstance(p, Exception)]
        generation = 0
        if settings.search_cache_seconds > 0 and todo:
            try:
                self.local.current()   # let a first load swap the index in (and clear kept answers) before the generation is read
            except LocalIndexUnavailable:
                pass
            now = time.monotonic()
            for i in list(todo):
                plan = plans[i]
                plan["key"] = (normalize_query(query), tuple(plan["concepts"]), tuple(plan["domains"]), plan["limit"],
                               tuple(sorted(plan["weights"].items())),
                               tuple(sorted((str(k), str(v)) for k, v in (plan["ranking"] or {}).items())))
                with _query_lock:
                    generation = _query_generation[0]
                    entry = _query_cache.get(plan["key"])
                    if entry and entry[0] > now:
                        _query_stats["hits"] += 1
                    else:
                        entry = None
                        _query_stats["misses"] += 1
                if entry is not None:
                    cached = copy.deepcopy(entry[1])
                    cached["query"] = query
                    answers[i] = cached
                    todo.remove(i)
        if todo:
            try:
                fresh = self._search_local_many(query, [plans[i] for i in todo])
            except LocalIndexUnavailable as exc:
                raise RuntimeError(f"The local search index is not available: {exc}") from exc
            for i, answer in zip(todo, fresh):
                answers[i] = answer
                key = plans[i]["key"]
                if (key is not None and not isinstance(answer, Exception) and answer.get("results")
                        and answer.get("search_mode") != "keyword"):
                    snapshot = copy.deepcopy(answer)
                    with _query_lock:
                        if generation == _query_generation[0]:
                            _query_cache[key] = (time.monotonic() + settings.search_cache_seconds, snapshot)
                            limit_entries = settings.search_cache_max_entries
                            if len(_query_cache) > limit_entries:
                                for old_key in list(_query_cache)[: max(1, limit_entries // 10)]:
                                    _query_cache.pop(old_key, None)
        return answers

    def _search_local_many(self, query, plans):
        """The hybrid searches of search_many, answered from the in-memory index in one pass. One entry per plan: the
        answer, or the exception that plan raised. Raises LocalIndexUnavailable when the index is not ready."""
        index = self.local.current()
        terms = query_terms(query)
        began = time.perf_counter()
        qcode, qvec, keyword_only = self._embed_query(query)
        embed_ms = (time.perf_counter() - began) * 1000
        began = time.perf_counter()
        work = index.begin(terms, qcode, qvec, query)
        hows, specs = [], []
        for plan in plans:
            how = self.ranking_settings(plan["ranking"])
            hows.append(how)
            limit = plan["limit"]
            specs.append({
                "domains": plan["domains"], "concepts": plan["concepts"], "limit": limit,
                "candidates": max(limit, settings.search_vector_k) if settings.search_vector_k else max(50, limit * 10),
                "text_candidates": settings.local_text_recall or 1000,
                "concept_weights": plan["weights"] if how["concept_boost"] > 0 else None,
                "concept_boost": how["concept_boost"], "max_per_page": how["max_per_page"],
                "phrase_boost": how["phrase_boost"], "phrase_pool": settings.local_phrase_pool,
                "unique_pages": how["unique_pages"],
            })
        found = index.search_many(work, specs)
        answers = []
        for plan, how, outcome in zip(plans, hows, found):
            if isinstance(outcome, Exception):
                answers.append(outcome)
                continue
            try:
                hits, matching = outcome
                answers.append(self._answer(index, query, hits, matching, keyword_only, plan["weights"], how))
            except Exception as exc:
                answers.append(exc)
        self.local.record_search(embed_ms, (time.perf_counter() - began) * 1000, keyword_only,
                                 rescored=bool(qvec is not None and index.vec8 is not None and not keyword_only))
        return answers

    def explain_local(self, query, concepts, domains, expect, concept_weights=None, limit=5, ranking=None):
        """Why the local search ranks a question as it does, and where the page it should have found stands. Uses the
        same settings as a real local search. Raises LocalIndexUnavailable when the index is not ready."""
        concepts = sorted({str(x).lower() for x in (concepts or [])})
        domains = sorted({str(x).lower() for x in (domains or [])})
        weights = {str(c).lower(): float(w) for c, w in (concept_weights or {}).items() if w and float(w) > 0}
        index = self.local.current()
        qcode, qvec, note = None, None, None
        try:
            get_vectors = getattr(self.embedder, "embed_query_vectors", None)
            if get_vectors:
                qcode, qvec = get_vectors(query)
            else:
                qcode = binary_code(self.embedder.embed_query(query))
        except Exception as exc:
            note = f"The question could not be embedded ({exc.__class__.__name__}), so this is the keyword half only."
        how = self.ranking_settings(ranking)
        boost = how["concept_boost"]
        out = index.explain(query_terms(query), qcode, domains, concepts, expect,
                            candidates=max(limit, settings.search_vector_k) if settings.search_vector_k else max(50, limit * 10),
                            limit=limit, qvec=qvec,
                            text_candidates=settings.local_text_recall or 1000,
                            concept_weights=weights if boost > 0 else None, concept_boost=boost,
                            max_per_page=how["max_per_page"], question=query, phrase_boost=how["phrase_boost"],
                            phrase_pool=settings.local_phrase_pool, unique_pages=how["unique_pages"])
        out.update({"query": query, "question_concepts": dict(sorted(weights.items())), "filter_concepts": concepts,
                    "filter_domains": domains, "note": note,
                    "settings": {"concept_boost": boost, "phrase_boost": how["phrase_boost"], "max_per_page": how["max_per_page"],
                                 "unique_pages": how["unique_pages"],
                                 "rescoring": bool(index.vec8 is not None), "text_in_memory": index.texts is not None,
                                 "text_recall": settings.local_text_recall or 1000}})
        return out

    def tag_counts(self, domain=None, top=200):
        """Chunks per ontology tag (the concepts field), plus the untagged chunks. Without a domain it also returns chunks
        per domain. Exact counts from the in-memory index."""
        return self._local_tag_counts(domain, top)

    def count_chunks(self, domain=None):
        """How many chunks the index holds (for one domain, when given)."""
        index = self.local.current()
        return index.n if not domain else int((index.domain_codes == self._domain_code(index, domain)).sum())

    def pages_with_tag(self, tag, domain=None, limit=50):
        """The pages that carry one tag, with how many of their chunks have it."""
        return self._local_pages_with_tag(tag, domain, limit)

    def page_tags(self, url):
        """The tags on each chunk of one page, read from the index (no chunk text)."""
        return self._local_page_tags(url)

    def delete_url(self, url):
        """Remove every chunk of a page: its rows (a page's chunks share one partition, named by the hash of its url),
        and its stored documents and vectors."""
        ids = self.local.store().page_ids(hashlib.sha256(str(url).encode()).hexdigest())
        if not ids:
            return 0
        self._local_delete(ids)
        for doc_id in ids:
            self.storage.delete("documents/" + str(doc_id) + ".json")
            self.storage.delete(self.vector_key(doc_id))
        return len(ids)

    def status(self):
        """What the index table holds now (a read of keys and domains, no text) and what this instance has loaded."""
        out = {"configured": self.local.enabled, "index": settings.local_index_table, "source": "local_index_table",
               "embedding_configured": settings.embedding_configured}
        try:
            out["document_count"] = self.local.store().count()
        except Exception as exc:
            out["document_count"] = None
            out["error"] = f"{type(exc).__name__}: {exc}"[:200]
        out["local_index"] = self.local.describe()
        return out

    # ---- the admin reads, answered from the in-memory index ----
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
