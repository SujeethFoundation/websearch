"""The search queue: at most SEARCH_MAX_CONCURRENT searches run at once on an instance, and one that waits longer than
SEARCH_QUEUE_TIMEOUT_SECONDS for a slot is refused with a 503."""
import json
import threading
import time

import azure.functions as func
import pytest

import function_app as fa
from services.admission import AdmissionGate, SearchBusy


def test_a_free_slot_is_taken_at_once_and_given_back():
    gate = AdmissionGate(2, 1)
    with gate:
        with gate:
            assert gate.stats()["running"] == 2
    assert gate.stats()["running"] == 0 and gate.stats()["admitted"] == 2


def test_a_request_that_waits_longer_than_the_timeout_is_refused():
    gate = AdmissionGate(1, 0.2)
    gate.acquire()
    began = time.monotonic()
    with pytest.raises(SearchBusy):
        gate.acquire()
    assert 0.15 <= time.monotonic() - began < 2.0
    stats = gate.stats()
    assert stats["refused"] == 1 and stats["waiting"] == 0 and stats["running"] == 1
    gate.release()


def test_a_request_that_waits_gets_the_slot_when_one_frees_in_time():
    gate = AdmissionGate(1, 5)
    gate.acquire()
    threading.Timer(0.2, gate.release).start()
    began = time.monotonic()
    gate.acquire()
    assert time.monotonic() - began < 2.0
    assert gate.stats()["admitted"] == 2 and gate.stats()["wait_max_ms"] >= 150
    gate.release()


def test_a_timeout_of_zero_refuses_at_once_when_no_slot_is_free():
    gate = AdmissionGate(1, 0)
    gate.acquire()
    began = time.monotonic()
    with pytest.raises(SearchBusy):
        gate.acquire()
    assert time.monotonic() - began < 0.5


def test_zero_slots_turns_the_limit_off():
    gate = AdmissionGate(0, 0)
    for _ in range(50):
        gate.acquire()
    gate.release()
    assert gate.stats()["refused"] == 0


def test_a_refusal_does_not_use_up_a_slot():
    gate = AdmissionGate(1, 0)
    gate.acquire()
    for _ in range(3):
        with pytest.raises(SearchBusy):
            gate.acquire()
    gate.release()
    gate.acquire()      # still works
    gate.release()


def test_a_search_that_waits_the_whole_timeout_gets_a_503_with_retry_after(monkeypatch):
    busy = AdmissionGate(1, 0.1)
    monkeypatch.setattr(fa, "gate", busy)
    busy.acquire()                                        # every slot is taken
    request = func.HttpRequest("GET", "/api/search", params={"q": "mentors"}, headers={}, body=b"")
    out = fa.search_get._function._func(request)
    assert out.status_code == 503 and out.headers["Retry-After"] == str(fa.settings.search_busy_retry_after)
    assert json.loads(out.get_body())["stage"] == "search_queue_timeout"
    busy.release()


def test_the_slot_is_given_back_when_the_search_fails(monkeypatch):
    one = AdmissionGate(1, 0)
    monkeypatch.setattr(fa, "gate", one)
    request = func.HttpRequest("POST", "/api/search", params={}, headers={}, body=b"{}")      # no query: a 400
    assert fa.search_post._function._func(request).status_code == 400
    assert one.stats()["running"] == 0 and one.stats()["admitted"] == 1


def test_health_reports_the_queue(monkeypatch):
    monkeypatch.setattr(fa, "gate", AdmissionGate(3, 10))
    body = json.loads(fa.health._function._func(func.HttpRequest("GET", "/api/health", headers={}, body=b"")).get_body())
    assert body["admission"]["slots"] == 3 and body["admission"]["queue_timeout_seconds"] == 10
