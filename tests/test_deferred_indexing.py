"""Tests for stored vectors and deferred indexing. Run from the websearch repo root: python -m pytest -q tests"""
import json
import types

import pytest

from services import local_index as li
from services import queue_worker as qw
from services import search_service as ss
from test_local_index import new_store


class Throttled(Exception):
    status_code = 503


class Table:
    """Wraps the index table so writes can be made to fail the way a throttled table does, and counted."""

    def __init__(self, store):
        self.mode, self.write_calls, self.read_calls = "ok", 0, 0
        chunks = store.chunks
        write, single = chunks.submit_transaction, chunks.upsert_entity

        def guard(call):
            def wrapped(*args, **kwargs):
                self.write_calls += 1
                if self.mode == "busy":
                    raise Throttled("the table is busy")
                return call(*args, **kwargs)
            return wrapped
        chunks.submit_transaction, chunks.upsert_entity = guard(write), guard(single)
        keys = store.existing_ids

        def existing(ids):
            self.read_calls += 1
            if self.mode == "check_busy":
                raise Throttled("the table is busy")
            return keys(ids)
        store.existing_ids = existing


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
        return [[len(t) % 7 / 10, 0.25, -0.5, 0.123456789] for t in texts]


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(ss.time, "sleep", lambda s: None)
    monkeypatch.setattr(ss.settings, "embedding_configured", True, raising=False)
    monkeypatch.setattr(ss.settings, "embedding_deployment", "sf-embeddings", raising=False)
    monkeypatch.setattr(ss.settings, "embedding_dimensions", 4, raising=False)
    ss._breaker_clear()
    svc = object.__new__(ss.SearchService)
    store = new_store()
    svc.table = Table(store)
    svc.storage, svc.embedder, svc.local = Store(), Embedder(), li.LocalIndexManager(store=store)
    yield svc
    ss._breaker_clear()


def docs(count, prefix="p", text="chunk text "):
    return [{"id": f"{prefix}-{i}", "title": "T", "url": "https://x/", "domain": "x",
             "content": text + str(i) * i, "concepts": []} for i in range(count)]


def test_vector_round_trip_keeps_float32_precision():
    vector = [0.123456789, -0.5, 3.0e-5, 1.0]
    back = ss.SearchService._unpack_vector(ss.SearchService._pack_vector(vector))
    assert max(abs(a - b) for a, b in zip(vector, back)) < 1e-6


def test_chunks_are_embedded_once_and_vectors_are_stored(service):
    assert service.upsert_or_defer(docs(3)) == (3, [])
    assert service.embedder.calls == [3]
    assert sorted(k for k in service.storage.blobs if k.startswith("vectors/")) == [f"vectors/p-{i}.json" for i in range(3)]
    service.upsert_or_defer(docs(3))
    assert service.embedder.calls == [3]  # reused


def test_changed_text_is_embedded_again(service):
    service.upsert_or_defer(docs(2))
    service.upsert_or_defer(docs(2, text="new text "))
    assert service.embedder.calls == [2, 2]


def test_embedding_requests_are_batched(service):
    service.upsert_or_defer(docs(40))
    assert service.embedder.calls == [16, 16, 8]


def test_busy_index_defers_without_raising_and_keeps_everything(service):
    service.table.mode = "busy"
    count, deferred = service.upsert_or_defer(docs(2))
    assert count == 0 and deferred == ["documents/p-0.json", "documents/p-1.json"]
    assert "vectors/p-0.json" in service.storage.blobs and "documents/p-0.json" in service.storage.blobs
    writes = service.table.write_calls
    _, again = service.upsert_or_defer(docs(2, prefix="q"))
    assert len(again) == 2 and service.table.write_calls == writes  # breaker open: no retries


def test_upsert_many_raises_when_busy(service):
    service.table.mode = "busy"
    with pytest.raises(ss.IndexBusyError):
        service.upsert_many(docs(1))


def test_recovery_indexes_without_embedding_again(service):
    service.table.mode = "busy"
    _, deferred = service.upsert_or_defer(docs(5))
    service.table.mode = "ok"
    ss._breaker_clear()
    assert service.index_existing_blobs(deferred) == 5
    assert service.embedder.calls == [5]


class Queue:
    def __init__(self):
        self.sent = []

    def send_message(self, content, visibility_timeout=None, **kwargs):
        self.sent.append((json.loads(content), visibility_timeout))


def processor(search):
    proc = object.__new__(qw.QueueProcessor)
    proc.queue, proc.search = Queue(), search
    proc.worker = types.SimpleNamespace(process=lambda task: {})
    return proc


def test_busy_indexing_is_requeued_with_growing_delays_then_gives_up():
    class Busy:
        def index_blobs(self, names, only_missing=False, domains=None):
            raise ss.IndexBusyError("busy")

    proc = processor(Busy())
    task = {"task_type": "index_blob_batch", "blob_names": ["documents/a.json"]}
    delays = [proc.process(json.dumps({**task, "attempt": a}))["retry_in_seconds"] for a in range(3)]
    assert delays == [300, 600, 1200]
    assert proc.queue.sent[-1][0]["attempt"] == 3
    with pytest.raises(ss.IndexBusyError):
        proc.process(json.dumps({**task, "attempt": qw.MAX_INDEX_DEFERRALS}))


def test_a_healthy_index_just_indexes():
    class Good:
        def index_blobs(self, names, only_missing=False, domains=None):
            return {"read": len(names), "already_indexed": 0, "indexed": len(names)}

    out = processor(Good()).process(json.dumps({"task_type": "index_blob_batch", "blob_names": ["a", "b"]}))
    assert out["status"] == "indexed" and out["documents_indexed"] == 2


def test_large_deferrals_are_split_into_small_messages():
    proc = processor(None)
    proc._defer_index_batch([f"documents/{i}.json" for i in range(250)], attempt=0)
    assert [len(m[0]["blob_names"]) for m in proc.queue.sent] == [100, 100, 50]


def test_purge_waits_for_a_busy_index_and_removes_nothing_meanwhile():
    store = Store()
    store.blobs["documents/p0-0.json"] = {"id": "p0-0", "domain": "tealhq.com", "url": "https://www.tealhq.com/job/a"}
    store.blobs["vectors/p0-0.json"] = {"v": "x"}

    class Policy:
        def all_rules(self):
            return {"tealhq.com": ("/job",)}

        def all_includes(self):
            return {}

    class BusySearch:
        vector_key = staticmethod(ss.SearchService.vector_key)

        def delete_ids(self, ids):
            raise ss.IndexBusyError("busy")

    proc = object.__new__(qw.QueueProcessor)
    proc.queue, proc.storage, proc.search = Queue(), store, BusySearch()
    proc.worker = types.SimpleNamespace(path_policy=Policy())
    out = proc.process(json.dumps({"task_type": "index_purge_batch", "blob_names": ["documents/p0-0.json"], "domains": []}))
    assert out["status"] == "index_busy_deferred" and out["retry_in_seconds"] == 120
    assert proc.queue.sent[-1][0]["attempt"] == 1
    assert sorted(store.blobs) == ["documents/p0-0.json", "vectors/p0-0.json"]  # nothing removed yet


def stored(service, count):
    for doc in docs(count):
        service.storage.put("documents/" + doc["id"] + ".json", doc)
    return ["documents/" + d["id"] + ".json" for d in docs(count)]


def written(service, count):
    """Put the first `count` test chunks in the table, as if an earlier run had indexed them."""
    service.upsert_or_defer(docs(count))
    service.embedder.calls.clear()
    service.table.write_calls = service.table.read_calls = 0


def test_missing_mode_indexes_only_what_the_table_lacks(service):
    names = stored(service, 5)
    written(service, 3)
    outcome = service.index_blobs(names, only_missing=True)
    assert outcome == {"read": 5, "already_indexed": 3, "indexed": 2}
    assert service.embedder.calls == [2]                                      # only the two missing chunks needed vectors
    assert len(service.local.store().chunks.rows) == 5


def test_all_mode_rewrites_everything(service):
    names = stored(service, 3)
    written(service, 3)
    assert service.index_blobs(names)["indexed"] == 3
    assert service.table.write_calls >= 1


def test_missing_mode_with_nothing_missing_writes_nothing(service):
    names = stored(service, 3)
    written(service, 3)
    assert service.index_blobs(names, only_missing=True) == {"read": 3, "already_indexed": 3, "indexed": 0}
    assert service.table.write_calls == 0 and service.embedder.calls == []


def test_missing_mode_asks_the_table_once_for_the_whole_batch(service):
    names = stored(service, 4)
    service.index_blobs(names, only_missing=True)
    assert service.table.read_calls == 1


def test_missing_mode_defers_when_the_existence_check_is_throttled(service):
    names = stored(service, 2)
    service.table.mode = "check_busy"
    with pytest.raises(ss.IndexBusyError):
        service.index_blobs(names, only_missing=True)


def test_backfill_pages_pass_the_mode_to_every_batch():
    class Storage:
        def list_blob_name_page(self, prefix, continuation_token=None, page_size=250):
            return [f"documents/{i}.json" for i in range(25)], "next"

    proc = processor(None)
    proc.storage = Storage()
    out = proc.process(json.dumps({"task_type": "index_backfill_page", "continuation_token": None, "mode": "missing"}))
    sent = [m[0] for m in proc.queue.sent]
    assert out["mode"] == "missing"
    assert all(m["mode"] == "missing" for m in sent) and sent[-1]["task_type"] == "index_backfill_page"


def test_a_batch_task_uses_the_missing_mode_and_keeps_it_when_deferred():
    seen = []

    class Idx:
        def index_blobs(self, names, only_missing=False, domains=None):
            seen.append(only_missing)
            if len(seen) == 2:
                raise ss.IndexBusyError("busy")
            return {"read": len(names), "already_indexed": 1, "indexed": len(names) - 1}

    proc = processor(Idx())
    task = {"task_type": "index_blob_batch", "blob_names": ["a", "b", "c"], "mode": "missing"}
    out = proc.process(json.dumps(task))
    assert seen == [True] and out["already_indexed"] == 1 and out["documents_indexed"] == 2
    proc.process(json.dumps(task))
    assert proc.queue.sent[-1][0]["mode"] == "missing" and proc.queue.sent[-1][0]["attempt"] == 1
    seen.clear()
    proc.process(json.dumps({"task_type": "index_blob_batch", "blob_names": ["a"]}))
    assert seen == [False]  # tasks without a mode keep the old behavior


def test_domain_filter_indexes_only_those_domains(service):
    for i, domain in enumerate(["sujeeth.org", "tealhq.com", "topics.sujeeth.org", "tealhq.com"]):
        service.storage.put(f"documents/d-{i}.json", {"id": f"d-{i}", "domain": domain, "title": "T", "url": "https://x/", "content": f"text {i}"})
    names = [f"documents/d-{i}.json" for i in range(4)]
    outcome = service.index_blobs(names, domains=["Sujeeth.org", "topics.sujeeth.org"])
    assert outcome == {"read": 2, "already_indexed": 0, "indexed": 2}
    assert service.embedder.calls == [2]  # the TealHQ chunks were never embedded


def test_backfill_keeps_the_domain_filter_on_every_message():
    class Storage:
        def list_blob_name_page(self, prefix, continuation_token=None, page_size=250):
            return [f"documents/{i}.json" for i in range(12)], "next"

    proc = processor(None)
    proc.storage = Storage()
    proc.process(json.dumps({"task_type": "index_backfill_page", "mode": "missing", "domains": ["sujeeth.org"]}))
    assert all(m[0]["domains"] == ["sujeeth.org"] for m in proc.queue.sent)
