"""Tests for the blob cache counters and the process information on /health."""
import json
import os
import time

import azure.functions as func
import pytest

import function_app as fa
from services import search_service as ss


class Storage:
    """Stands in for blob storage: every get is one download."""

    def __init__(self, docs):
        self.docs, self.gets = docs, 0

    def get(self, name):
        self.gets += 1
        return self.docs.get(name)


@pytest.fixture
def searcher(monkeypatch):
    monkeypatch.setattr(ss, "_blob_cache", {})
    monkeypatch.setattr(ss, "_blob_stats", {"hits": 0, "misses": 0, "storage_reads": 0})
    storage = Storage({f"documents/c{i}.json": {"content": f"text {i}"} for i in range(5)})
    svc = ss.SearchService.__new__(ss.SearchService)
    svc.storage = storage
    return svc


def test_with_the_cache_off_every_read_goes_to_storage_and_none_count_as_misses(searcher, monkeypatch):
    monkeypatch.setattr(ss, "BLOB_CACHE_SECONDS", 0)
    for _ in range(3):
        assert searcher._blob_text("c1") == "text 1"
    stats = ss.blob_cache_stats()
    assert stats["enabled"] is False and stats["storage_reads"] == 3
    assert stats["hits"] == 0 and stats["misses"] == 0 and stats["hit_rate"] is None and stats["entries"] == 0


def test_with_the_cache_on_the_first_read_misses_and_the_rest_hit(searcher, monkeypatch):
    monkeypatch.setattr(ss, "BLOB_CACHE_SECONDS", 600)
    for _ in range(4):
        assert searcher._blob_text("c1") == "text 1"
    stats = ss.blob_cache_stats()
    assert stats["enabled"] is True and stats["seconds"] == 600
    assert (stats["misses"], stats["hits"], stats["storage_reads"], stats["entries"]) == (1, 3, 1, 1)
    assert stats["hit_rate"] == 0.75
    assert searcher.storage.gets == 1  # three of the four reads never reached storage


def test_an_expired_entry_counts_as_a_miss_and_is_read_again(searcher, monkeypatch):
    monkeypatch.setattr(ss, "BLOB_CACHE_SECONDS", 600)
    searcher._blob_text("c2")
    name = "documents/c2.json"
    ss._blob_cache[name] = (time.monotonic() - 1, "stale")  # expired a second ago
    assert searcher._blob_text("c2") == "text 2"
    stats = ss.blob_cache_stats()
    assert stats["misses"] == 2 and stats["storage_reads"] == 2 and stats["hits"] == 0


def test_a_missing_blob_is_counted_as_a_read_and_a_miss_but_not_cached(searcher, monkeypatch):
    monkeypatch.setattr(ss, "BLOB_CACHE_SECONDS", 600)
    assert searcher._blob_text("nope") is None
    stats = ss.blob_cache_stats()
    assert stats["misses"] == 1 and stats["storage_reads"] == 1 and stats["entries"] == 0


def test_several_chunks_read_in_parallel_are_all_counted(searcher, monkeypatch):
    monkeypatch.setattr(ss, "BLOB_CACHE_SECONDS", 600)
    ids = ["c0", "c1", "c2", "c3", "c4"]
    searcher._blob_content(ids)
    searcher._blob_content(ids)
    stats = ss.blob_cache_stats()
    assert stats["misses"] == 5 and stats["hits"] == 5 and stats["storage_reads"] == 5
    assert searcher.storage.gets == 5


def _health():
    req = func.HttpRequest("GET", "/api/health", headers={}, body=b"")
    return json.loads(fa.health._function._func(req).get_body())


def test_health_reports_the_process_and_the_cache():
    body = _health()
    assert body["status"] == "healthy"
    proc = body["process"]
    assert proc["pid"] == os.getpid() and proc["instance"] and proc["uptime_seconds"] >= 0
    assert proc["started_at"].endswith("+00:00")
    cache = body["blob_cache"]
    assert {"enabled", "seconds", "max_entries", "entries", "hits", "misses", "hit_rate", "storage_reads"} <= set(cache)


def test_uptime_grows_and_started_at_is_stable():
    first = fa.process_info()
    time.sleep(1.1)
    second = fa.process_info()
    assert second["uptime_seconds"] >= first["uptime_seconds"] + 1
    assert second["started_at"] == first["started_at"] and second["pid"] == first["pid"]


def test_instance_name_prefers_the_azure_instance_id(monkeypatch):
    monkeypatch.setenv("WEBSITE_INSTANCE_ID", "abc123")
    assert fa.process_info()["instance"] == "abc123"
    monkeypatch.delenv("WEBSITE_INSTANCE_ID")
    assert fa.process_info()["instance"]  # falls back to the host name
