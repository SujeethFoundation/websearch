"""Writing chunks: Blob Storage keeps the document and its vector, the index table keeps the row, and a retag
rewrites rows without embedding again."""
import json
import types

import pytest

from services import local_index as li
from services import queue_worker as qw
from services import search_service as ss
from test_local_index import new_store


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
    for name, value in {"embedding_configured": True, "embedding_deployment": "sf-embeddings", "embedding_dimensions": 4}.items():
        monkeypatch.setattr(ss.settings, name, value, raising=False)
    ss._breaker_clear()
    svc = object.__new__(ss.SearchService)
    svc.storage, svc.embedder = Store(), Embedder()
    svc.local = li.LocalIndexManager(store=new_store())
    yield svc
    ss._breaker_clear()


def chunk(i, text=None, concepts=("a",)):
    return {"id": f"p-{i}", "title": "T", "url": "https://x/", "domain": "x", "content": text or f"chunk text {i}", "concepts": list(concepts)}


def rows(service):
    return service.local.store().chunks.rows


def test_a_write_keeps_the_document_and_vector_in_blobs_and_the_row_in_the_table(service):
    assert service.upsert_or_defer([chunk(0), chunk(1)]) == (2, [])
    assert sorted(service.storage.blobs) == ["documents/p-0.json", "documents/p-1.json", "vectors/p-0.json", "vectors/p-1.json"]
    assert len(rows(service)) == 2 and service.local.store().get_version()
    assert service.embedder.calls == [2]


def test_a_chunk_is_embedded_once_however_often_it_is_written(service):
    service.upsert_or_defer([chunk(0)])
    service.upsert_or_defer([chunk(0)])
    assert service.embedder.calls == [1]                       # the stored vector is reused while the text is the same


def test_retag_rewrites_the_chunks_with_their_stored_vectors(service):
    docs = [chunk(0), chunk(1), chunk(2)]
    service.upsert_or_defer(docs)                              # stores the vectors next to the chunks
    updated, missing = service.update_concepts([("p-0", ["x"]), ("p-1", ["y"]), ("p-2", ["z"]), ("p-9", ["w"])], docs=docs)
    assert (updated, missing) == (3, 1)                        # p-9 has no stored document to rewrite
    assert len(rows(service)) == 3                             # rewritten in place, not added
    index = service.local.reload() or service.local.current()
    assert sorted(tuple(index.concepts[i]) for i in range(index.n)) == [("x",), ("y",), ("z",)]
    assert service.embedder.calls == [3]                       # embedded once, at the first write


def test_a_busy_table_defers_the_work_and_keeps_the_stored_chunks(service, monkeypatch):
    class Throttled(Exception):
        status_code = 503
    service.local.store().chunks.submit_transaction = lambda ops: (_ for _ in ()).throw(Throttled("busy"))
    service.local.store().chunks.upsert_entity = lambda e, mode=None: (_ for _ in ()).throw(Throttled("busy"))
    done, deferred = service.upsert_or_defer([chunk(0), chunk(1)])
    assert done == 0 and deferred == ["documents/p-0.json", "documents/p-1.json"]
    assert "documents/p-0.json" in service.storage.blobs and "vectors/p-0.json" in service.storage.blobs     # nothing lost
    assert ss._breaker_open()
    again = service.upsert_or_defer([chunk(2)])
    assert again == (0, ["documents/p-2.json"]) and service.embedder.calls == [2, 1]      # breaker open: stored, not written


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
