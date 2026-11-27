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
