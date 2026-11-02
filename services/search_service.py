"""Azure AI Search indexing and hybrid retrieval; Blob Storage remains canonical."""
import logging
import time
import requests
from services.config import settings
from services.embedding_service import EmbeddingService

log = logging.getLogger("sujeeth-web-search.ai-search")


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

    def _write_batch(self, actions, rounds=5):
        """Send index actions and retry the documents the service reports as busy (429 or 503
        inside an otherwise successful response). Returns the final result for each document."""
        pending = list(actions)
        final = {}
        for round_number in range(rounds):
            response = self._post("index", {"value": pending}, 90)
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
            item["contentVector"] = vector
        return item

    def _upload(self, docs):
        if not self.configured:
            raise RuntimeError("Azure AI Search is not configured.")
        outcomes = []
        for start in range(0, len(docs), settings.search_batch_size):
            batch = docs[start:start + settings.search_batch_size]
            actions = [{"@search.action": "mergeOrUpload", **x} for x in batch]
            result = self._write_batch(actions)
            failures = [x for x in result if not x.get("status")]
            if failures:
                raise RuntimeError(f"Azure AI Search rejected {len(failures)} document(s): {failures[:3]}")
            outcomes.extend(result)
        return outcomes

    def update_concepts(self, updates):
        """Merge new concept tags into documents already in the index. No re-embedding.

        updates is a list of (document_id, concepts). Uses the 'merge' action so a
        document that is not indexed yet is skipped, never created as an empty stub.
        Returns (updated, not_indexed).
        """
        if not self.configured:
            raise RuntimeError("Azure AI Search is not configured.")
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

    def fetch_chunks(self, url=None, chunk_id=None, neighbors=1):
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
        response = self._post("search", {
            "search": "*", "filter": filter_expr, "top": 1000,
            "select": "id,title,url,domain,concepts,content",
        }, 45, attempts=3, max_wait=5)
        response.raise_for_status()
        rows = response.json().get("value", [])
        total = None
        if rows:
            page_url = str(rows[0].get("url") or "").replace("'", "''")
            counted = self._post("search", {
                "search": "*", "filter": f"url eq '{page_url}'", "top": 0, "count": True,
            }, 45, attempts=3, max_wait=5)
            counted.raise_for_status()
            total = counted.json().get("@odata.count")
        return rows, focus, total

    def upsert_many(self, documents, persist_blobs=True):
        documents = [dict(x) for x in documents if x and x.get("id")]
        if not documents:
            return 0
        if not settings.embedding_configured:
            raise RuntimeError("Embedding configuration is missing; refusing to index documents without vectors.")
        vectors = self.embedder.embed([x.get("content", "") for x in documents])
        search_docs = [self._search_doc(doc, vector) for doc, vector in zip(documents, vectors)]
        # Save source documents first. They remain the durable, rebuildable source of truth.
        if persist_blobs:
            for doc in documents:
                key = "documents/" + str(doc["id"]) + ".json"
                stored = dict(doc)
                stored["_key"] = key
                stored.pop("embedding", None)
                self.storage.put(key, stored)
        self._upload(search_docs)
        return len(search_docs)

    def upsert(self, doc):
        return self.upsert_many([doc], persist_blobs=True)

    def index_existing_blobs(self, blob_names):
        docs = []
        for name in blob_names:
            doc = self.storage.get(name)
            if doc and doc.get("id") and doc.get("content"):
                docs.append(doc)
        return self.upsert_many(docs, persist_blobs=False)

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
                "fields": "contentVector",
                "k": max(50, limit * 10),
            }],
            "select": "id,title,url,domain,concepts,content",
            "top": limit,
            "count": True,
        }
        response = self._post("search", body, 45, attempts=3, max_wait=5)
        response.raise_for_status()
        payload = response.json()
        results = []
        for item in payload.get("value", []):
            results.append({
                "title": item.get("title"),
                "url": item.get("url"),
                "snippet": (item.get("content") or "")[:700],
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
        return len(ids)

    def rebuild_vectors(self):
        raise RuntimeError("Use POST /api/index/backfill to queue a resumable Azure AI Search backfill.")

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