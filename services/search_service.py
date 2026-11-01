"""Azure AI Search indexing and hybrid retrieval; Blob Storage remains canonical."""
import logging
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
            payload = {"value": [{"@search.action": "mergeOrUpload", **x} for x in batch]}
            response = self.session.post(self._url("index"), json=payload, timeout=90)
            response.raise_for_status()
            result = response.json().get("value", [])
            failures = [x for x in result if not x.get("status")]
            if failures:
                raise RuntimeError(f"Azure AI Search rejected {len(failures)} document(s): {failures[:3]}")
            outcomes.extend(result)
        return outcomes

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
        response = self.session.post(self._url("search"), json=body, timeout=45)
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
        response = self.session.post(self._url("search"), json={
            "search": "*", "filter": filter_expr, "select": "id", "top": 1000
        }, timeout=45)
        response.raise_for_status()
        ids = [x.get("id") for x in response.json().get("value", []) if x.get("id")]
        if not ids:
            return 0
        payload = {"value": [{"@search.action": "delete", "id": doc_id} for doc_id in ids]}
        delete_response = self.session.post(self._url("index"), json=payload, timeout=90)
        delete_response.raise_for_status()
        for doc_id in ids:
            self.storage.delete("documents/" + str(doc_id) + ".json")
        return len(ids)

    def rebuild_vectors(self):
        raise RuntimeError("Use POST /api/index/backfill to queue a resumable Azure AI Search backfill.")

    def status(self):
        if not self.configured:
            return {"configured": False, "index": settings.search_index_name}
        response = self.session.post(self._url("search"), json={
            "search": "*", "top": 0, "count": True
        }, timeout=30)
        response.raise_for_status()
        payload = response.json()
        return {
            "configured": True,
            "index": settings.search_index_name,
            "document_count": payload.get("@odata.count"),
            "embedding_configured": settings.embedding_configured,
        }
