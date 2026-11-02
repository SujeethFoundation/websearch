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
        def index_existing_blobs(self, names):
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
        def index_existing_blobs(self, names):
            return len(names)

    out = processor(Good()).process(json.dumps({"task_type": "index_blob_batch", "blob_names": ["a", "b"]}))
    assert out["status"] == "indexed" and out["documents_indexed"] == 2


def test_large_deferrals_are_split_into_small_messages():
    proc = processor(None)
    proc._defer_index_batch([f"documents/{i}.json" for i in range(250)], attempt=0)
    assert [len(m[0]["blob_names"]) for m in proc.queue.sent] == [100, 100, 50]
