
import math
import re
from services.embedding_service import EmbeddingService


def tokens(value):
    return re.findall(r"[a-z0-9]+", (value or "").lower())


def cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


class SearchService:
    def __init__(self, storage):
        self.storage = storage
        self.embedder = EmbeddingService()

    def upsert(self, doc):
        if not doc.get("id"):
            raise ValueError("Document requires an id")
        doc = dict(doc)
        doc["_key"] = "documents/" + str(doc["id"]) + ".json"
        vectors = self.embedder.embed([doc.get("content", "")])
        if vectors:
            doc["embedding"] = vectors[0]
        self.storage.put(doc["_key"], doc)

    def search(self, query, concepts=None, domains=None, limit=5):
        concepts = {str(x).lower() for x in (concepts or [])}
        domains = {str(x).lower() for x in (domains or [])}
        if not concepts or not domains:
            raise ValueError("Search requires ontology-resolved concepts and domains.")

        query_tokens = set(tokens(query))
        query_vector = self.embedder.embed([query])
        query_vector = query_vector[0] if query_vector else None
        ranked = []

        for doc in self.storage.list("documents/"):
            if str(doc.get("domain", "")).lower() not in domains:
                continue
            doc_concepts = {str(x).lower() for x in doc.get("concepts", [])}
            if not concepts.intersection(doc_concepts):
                continue

            words = tokens(
                doc.get("title", "") + " " + doc.get("content", "")
                + " " + " ".join(doc_concepts)
            )
            keyword_score = sum(words.count(token) for token in query_tokens) / max(1, len(query_tokens))
            vector_score = cosine(query_vector, doc.get("embedding")) if query_vector else 0.0
            score = 0.5 * keyword_score + 0.5 * vector_score if query_vector else keyword_score
            if score > 0:
                ranked.append((score, doc))

        ranked.sort(key=lambda item: item[0], reverse=True)
        results = []
        for score, doc in ranked[:limit]:
            results.append({
                "title": doc.get("title"),
                "url": doc.get("url"),
                "snippet": doc.get("content", "")[:500],
                "domain": doc.get("domain"),
                "concepts": doc.get("concepts", []),
                "score": round(score, 6),
                "source": "website",
            })

        return {
            "query": query,
            "count": len(results),
            "search_mode": "hybrid" if query_vector else "keyword_fallback",
            "embedding_enabled": bool(query_vector),
            "results": results,
        }

    def rebuild_vectors(self):
        docs = list(self.storage.list("documents/"))
        updated = 0
        for doc in docs:
            vectors = self.embedder.embed([doc.get("content", "")])
            if vectors:
                doc["embedding"] = vectors[0]
                self.storage.put(doc["_key"], doc)
                updated += 1
        return updated
