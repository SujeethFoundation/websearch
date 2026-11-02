"""Tests for the blob-backed index mode (run from the websearch repo root)."""
import json
import types

import pytest
import requests

from services import queue_worker as qw
from services import search_service as ss


class Resp:
    def __init__(self, code=200, body=None, headers=None):
        self.status_code, self._body, self.headers = code, body or {}, headers or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)


class Session:
    """Records requests. `present` is the set of ids the index already holds."""

    def __init__(self, present=(), hits=None):
        self.present, self.hits, self.bodies = set(present), hits or [], []

    def post(self, url, json=None, timeout=None):
        self.bodies.append((url, json))
        if "/docs/index" in url:
            return Resp(200, {"value": [{"key": a["id"], "status": True, "statusCode": 200} for a in json["value"]]})
        flt = (json or {}).get("filter", "")
        if "search.in(id" in flt:
            asked = flt.split("'")[1].split(",")
            return Resp(200, {"value": [{"id": i} for i in asked if i in self.present]})
        return Resp(200, {"value": self.hits, "@odata.count": len(self.hits)})

    def index_actions(self):
        return [a["@search.action"] for url, body in self.bodies if "/docs/index" in url for a in body["value"]]


class Store:
    def __init__(self):
        self.blobs = {}

    def get(self, name):
        return json.loads(json.dumps(self.blobs[name])) if name in self.blobs else None

    def put(self, name, obj):
        self.blobs[name] = json.loads(json.dumps(obj))

    def delete(self, name):
        self.blobs.pop(name, None)


class Embedder:
    def __init__(self):
        self.calls = []

    def embed(self, texts):
        self.calls.append(len(texts))
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(ss.time, "sleep", lambda s: None)
    for name, value in {"embedding_configured": True, "embedding_deployment": "sf-embeddings", "embedding_dimensions": 4,
                        "search_endpoint": "https://x", "search_index_name": "sf", "search_api_version": "v",
                        "search_batch_size": 100, "search_vector_field": "contentVector", "search_null_vector_fields": (),
                        "search_blob_backed": True}.items():
        monkeypatch.setattr(ss.settings, name, value, raising=False)
    monkeypatch.setattr(ss.SearchService, "configured", property(lambda self: True))
    ss._breaker_clear()
    svc = object.__new__(ss.SearchService)
    svc.session, svc.storage, svc.embedder = Session(), Store(), Embedder()
    yield svc
    ss._breaker_clear()


def chunk(i, text=None):
    return {"id": f"p-{i}", "title": "T", "url": "https://x/", "domain": "x", "content": text or f"chunk text {i}", "concepts": ["a"]}


def test_writes_replace_whole_documents(service, monkeypatch):
    service.upsert_or_defer([chunk(0), chunk(1)])
    assert set(service.session.index_actions()) == {"upload"}
    monkeypatch.setattr(ss.settings, "search_blob_backed", False, raising=False)
    service.session.bodies.clear()
    service.upsert_or_defer([chunk(0)])
    assert set(service.session.index_actions()) == {"mergeOrUpload"}


def test_retag_rewrites_present_chunks_with_their_vectors(service):
    docs = [chunk(0), chunk(1), chunk(2)]
    service.upsert_or_defer(docs)  # stores the vectors next to the chunks
    service.session = Session(present={"p-0", "p-1"})
    updated, missing = service.update_concepts([("p-0", ["x"]), ("p-1", ["y"]), ("p-2", ["z"])], docs=docs)
    assert (updated, missing) == (2, 1)
    assert set(service.session.index_actions()) == {"upload"}  # never a partial merge
    sent = [a for url, body in service.session.bodies if "/docs/index" in url for a in body["value"]]
    assert {a["id"]: a["concepts"] for a in sent} == {"p-0": ["x"], "p-1": ["y"]}
    assert all(a["contentVector"] == pytest.approx([0.1, 0.2, 0.3, 0.4], abs=1e-6) for a in sent)
    assert service.embedder.calls == [3]  # embedded once, at the first write


def test_search_snippets_come_from_blob_storage(service):
    service.storage.put("documents/p-0.json", chunk(0, "the full text of chunk zero " * 40))
    service.session = Session(hits=[{"id": "p-0", "title": "T", "url": "https://x/", "domain": "x", "concepts": ["a"], "@search.score": 0.5}])
    result = service.search("donate", ["a"], ["x"], 3)
    url, body = [b for b in service.session.bodies if "/docs/search" in b[0]][0]
    assert "content" not in body["select"].split(",")
    assert result["results"][0]["snippet"].startswith("the full text of chunk zero") and len(result["results"][0]["snippet"]) == 700


def test_chunk_lookup_fills_the_text_from_blobs(service):
    for i in range(3):
        service.storage.put(f"documents/p-{i}.json", chunk(i))
    service.session = Session(present={"p-0", "p-1", "p-2"})  # the id lookup answers with the matching ids only
    rows, focus, total = service.fetch_chunks(chunk_id="p-1", neighbors=1)
    assert [r["content"] for r in rows] == ["chunk text 0", "chunk text 1", "chunk text 2"]
    assert focus == 1


def test_the_retag_job_passes_the_whole_documents():
    seen = {}

    class Search:
        def update_concepts(self, updates, docs=None):
            seen["updates"], seen["docs"] = updates, docs
            return len(updates), 0

    proc = object.__new__(qw.QueueProcessor)
    proc.storage, proc.search = Store(), Search()
    doc = {"id": "p-0", "content": "c"}
    proc._retag_changes = lambda names, domains=None, collect_examples=0: ([("documents/p-0.json", doc, ["x"])], {"read": 1, "checked": 1}, [])
    out = proc.retag_blobs(["documents/p-0.json"])
    assert out["changed"] == 1 and seen["updates"] == [("p-0", ["x"])] and seen["docs"][0]["concepts"] == ["x"]
