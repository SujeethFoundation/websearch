"""Tests for the process information on /health."""
import json
import os
import time

import azure.functions as func

import function_app as fa


def _health():
    return json.loads(fa.health._function._func(func.HttpRequest("GET", "/api/health", headers={}, body=b"")).get_body())


def test_health_reports_the_process_and_the_result_cache():
    body = _health()
    assert body["status"] == "healthy"
    proc = body["process"]
    assert proc["pid"] == os.getpid() and proc["instance"] and proc["uptime_seconds"] >= 0
    assert proc["started_at"].endswith("+00:00")
    cache = body["query_cache"]
    assert {"enabled", "seconds", "max_entries", "entries", "hits", "misses", "hit_rate"} <= set(cache)
    assert "compute_units_saved" not in cache and "blob_cache" not in body


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
