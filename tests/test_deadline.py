import time
import pytest
from services import deadline
from services.deadline import SearchDeadline


def test_no_deadline_means_no_limit():
    deadline.check("anything")


def test_passes_inside_the_limit_and_stops_after_it():
    token = deadline.start(5)
    try:
        deadline.check("scoring")
    finally:
        deadline.clear(token)
    token = deadline.start(1, arrived=time.monotonic() - 3)    # arrived 3 s ago: the wait already used the limit
    try:
        with pytest.raises(SearchDeadline) as err:
            deadline.check("scoring")
        assert err.value.stage == "scoring" and err.value.elapsed >= 3
    finally:
        deadline.clear(token)
    deadline.check("after clear")


def test_zero_turns_it_off():
    token = deadline.start(0, arrived=time.monotonic() - 100)
    try:
        deadline.check("scoring")
    finally:
        deadline.clear(token)


def test_a_search_past_its_deadline_is_refused_with_a_503(monkeypatch):
    import function_app
    from services.search_service import SearchService
    monkeypatch.setattr(function_app.settings, "search_deadline_seconds", 1.0, raising=False)

    @function_app.gated
    def slow(req):
        # arrived long ago: the next stage's check stops it
        deadline.start(1, arrived=time.monotonic() - 5)
        deadline.check("scoring")

    out = slow(None)
    assert out.status_code == 503
    assert b"search_deadline" in out.get_body() and out.headers.get("Retry-After")


class Req:
    def __init__(self, budget_ms=None):
        self.headers = {} if budget_ms is None else {"X-Request-Budget-Ms": str(budget_ms)}


def test_the_callers_budget_shortens_the_deadline(monkeypatch):
    import function_app
    monkeypatch.setattr(function_app.settings, "search_deadline_seconds", 20.0, raising=False)
    seen = []

    @function_app.gated
    def look(req):
        seen.append(deadline._until.get())

    look(Req(3000))
    look(Req())
    assert round(seen[0][2], 1) == 3.0 and round(seen[1][2], 1) == 20.0


def test_the_callers_budget_also_caps_the_wait_for_a_slot(monkeypatch):
    import function_app
    from services.admission import AdmissionGate
    gate = AdmissionGate(1, 5)
    monkeypatch.setattr(function_app, "gate", gate)
    gate.acquire()                                   # the only slot is taken
    try:
        began = time.monotonic()
        out = function_app.gated(lambda req: None)(Req(300))
        assert out.status_code == 503 and time.monotonic() - began < 1.5
    finally:
        gate.release()
