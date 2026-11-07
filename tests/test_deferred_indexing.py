"""Tests for stored vectors and deferred indexing. Run from the websearch repo root: python -m pytest -q tests"""
import json
import types

import pytest
import requests

from services import queue_worker as qw
from services import search_service as ss


class Resp:
    def __init__(self, code, body=None, headers=None):
        self.status_code, self._body, self.headers = code, body or {}, headers or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)


class Session:
    def __init__(self):
        self.mode, self.index_calls = "ok", 0

    def post(self, url, json=None, timeout=None):
        if "/docs/index" in url:
            self.index_calls += 1
            if self.mode == "busy":
                return Resp(429, headers={"Retry-After": "0"})
            return Resp(200, {"value": [{"key": a["id"], "status": True, "statusCode": 200} for a in json["value"]]})
        return Resp(200, {"value": []})


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
    monkeypatch.setattr(ss.settings, "search_endpoint", "https://x", raising=False)
    monkeypatch.setattr(ss.settings, "search_index_name", "sf", raising=False)
    monkeypatch.setattr(ss.settings, "search_api_version", "v", raising=False)
    monkeypatch.setattr(ss.settings, "search_batch_size", 100, raising=False)
    monkeypatch.setattr(ss.SearchService, "configured", property(lambda self: True))
    ss._breaker_clear()
    svc = object.__new__(ss.SearchService)
    svc.session, svc.storage, svc.embedder = Session(), Store(), Embedder()
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
    service.session.mode = "busy"
    count, deferred = service.upsert_or_defer(docs(2))
    assert count == 0 and deferred == ["documents/p-0.json", "documents/p-1.json"]
    assert "vectors/p-0.json" in service.storage.blobs and "documents/p-0.json" in service.storage.blobs
    posts = service.session.index_calls
    _, again = service.upsert_or_defer(docs(2, prefix="q"))
    assert len(again) == 2 and service.session.index_calls == posts  # breaker open: no retries


def test_upsert_many_raises_when_busy(service):
    service.session.mode = "busy"
    with pytest.raises(ss.IndexBusyError):
        service.upsert_many(docs(1))


def test_recovery_indexes_without_embedding_again(service):
    service.session.mode = "busy"
    _, deferred = service.upsert_or_defer(docs(5))
    service.session.mode = "ok"
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


class LookupSession(Session):
    """Answers id lookups: filter queries can be refused (400), and direct lookups use GET."""

    def __init__(self, present, filterable=True, busy=False):
        super().__init__()
        self.present, self.filterable, self.busy, self.gets = set(present), filterable, busy, 0

    def post(self, url, json=None, timeout=None):
        if "/docs/search" in url and "search.in(id" in (json or {}).get("filter", ""):
            if self.busy:
                return Resp(429, headers={"Retry-After": "0"})
            if not self.filterable:
                return Resp(400, {"error": {"message": "id is not filterable"}})
            asked = json["filter"].split("'")[1].split(",")
            return Resp(200, {"value": [{"id": i} for i in asked if i in self.present]})
        return super().post(url, json, timeout)

    def get(self, url, timeout=None):
        self.gets += 1
        doc_id = url.split("docs('")[1].split("')")[0]
        return Resp(200, {"id": doc_id}) if doc_id in self.present else Resp(404)


def stored(service, count):
    for doc in docs(count):
        service.storage.put("documents/" + doc["id"] + ".json", doc)
    return ["documents/" + d["id"] + ".json" for d in docs(count)]


def test_missing_mode_indexes_only_what_the_index_lacks(service):
    names = stored(service, 5)
    service.session = LookupSession(present={"p-0", "p-1", "p-2"})
    outcome = service.index_blobs(names, only_missing=True)
    assert outcome == {"read": 5, "already_indexed": 3, "indexed": 2}
    assert service.embedder.calls == [2]  # only the two missing chunks needed vectors
    assert service.session.index_calls == 1


def test_all_mode_resends_everything(service):
    names = stored(service, 3)
    service.session = LookupSession(present={"p-0", "p-1", "p-2"})
    assert service.index_blobs(names)["indexed"] == 3


def test_missing_mode_with_nothing_missing_writes_nothing(service):
    names = stored(service, 3)
    service.session = LookupSession(present={"p-0", "p-1", "p-2"})
    assert service.index_blobs(names, only_missing=True) == {"read": 3, "already_indexed": 3, "indexed": 0}
    assert service.session.index_calls == 0 and service.embedder.calls == []


def test_missing_mode_falls_back_to_lookups_when_id_is_not_filterable(service):
    names = stored(service, 4)
    service.session = LookupSession(present={"p-1"}, filterable=False)
    outcome = service.index_blobs(names, only_missing=True)
    assert outcome == {"read": 4, "already_indexed": 1, "indexed": 3}
    assert service.session.gets == 4


def test_missing_mode_defers_when_the_existence_check_is_throttled(service):
    names = stored(service, 2)
    service.session = LookupSession(present=set(), busy=True)
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
