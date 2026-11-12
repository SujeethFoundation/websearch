"""Tests for Compute Unit reporting and the opt-in query tuning settings."""
import copy
import json

import pytest
import requests
from requests.structures import CaseInsensitiveDict

import function_app as fa
from services import search_service as ss
from services.config import settings


class Response:
    def __init__(self, status=200, body=None, cu=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {"value": [], "@odata.count": 0}
        self.headers = CaseInsensitiveDict(headers or {})
        if cu is not None:
            self.headers["x-ms-azs-compute-units-consumed"] = str(cu)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)


class Session:
    """Returns the queued responses in order, then repeats the last one. Keeps every request body."""

    def __init__(self, *responses):
        self.responses, self.bodies = list(responses), []

    def post(self, url, json=None, timeout=None):
        self.bodies.append(copy.deepcopy(json))  # the app edits its body after a rejected send
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


HIT = {"id": "p-0", "title": "T", "url": "https://sujeeth.org/x", "domain": "sujeeth.org", "concepts": ["c"],
       "content": "chunk text", "@search.score": 1.0}


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(ss, "_cu_stats", {})
    monkeypatch.setattr(ss, "_hybrid_option_rejected", [False])
    monkeypatch.setattr(ss.time, "sleep", lambda s: None)
    monkeypatch.setattr(settings, "search_blob_backed", False)
    monkeypatch.setattr(settings, "search_endpoint", "https://example.search.windows.net")
    monkeypatch.setattr(settings, "search_api_key", "k")
    monkeypatch.setattr(settings, "search_configured", True)  # computed once at import from the environment
    for name, value in (("search_max_text_recall", 0), ("search_vector_k", 0), ("search_count", True), ("search_fields", "")):
        monkeypatch.setattr(settings, name, value)
    svc = ss.SearchService.__new__(ss.SearchService)
    svc.storage = None
    return svc


def run(service, session, **kw):
    service.session = session
    return service.search("week 3 agenda", concepts=["circle"], domains=["sujeeth.org"], limit=kw.get("limit", 5))


# ---- Compute Units ----

def test_each_response_header_is_recorded_under_its_label(service):
    run(service, Session(Response(body={"value": [HIT]}, cu=12.5)))
    stats = ss.compute_unit_stats()
    assert stats["query"] == {"requests": 1, "reported": 1, "total": 12.5, "average": 12.5, "max": 12.5}


def test_totals_averages_and_maximums_accumulate(service):
    session = Session(Response(cu=10), Response(cu=20), Response(cu=30))
    for _ in range(3):
        run(service, session)
    row = ss.compute_unit_stats()["query"]
    assert (row["requests"], row["total"], row["average"], row["max"]) == (3, 60.0, 20.0, 30.0)


def test_a_response_without_the_header_is_counted_but_not_averaged(service):
    run(service, Session(Response(), Response(cu=8)))
    run(service, Session(Response(cu=8)))
    row = ss.compute_unit_stats()["query"]
    assert row["requests"] == 2 and row["reported"] == 1 and row["average"] == 8.0
    ss._cu_stats.clear()
    run(service, Session(Response()))
    assert ss.compute_unit_stats()["query"]["average"] is None  # nothing reported, no divide by zero


def test_a_garbled_header_is_ignored(service):
    session = Session(Response(headers={"x-ms-azs-compute-units-consumed": "lots"}))
    run(service, session)
    assert ss.compute_unit_stats()["query"]["reported"] == 0


def test_retried_responses_are_counted_too(service):
    run(service, Session(Response(status=503, cu=2), Response(status=503, cu=2), Response(body={"value": [HIT]}, cu=10)))
    row = ss.compute_unit_stats()["query"]
    assert row["requests"] == 3 and row["total"] == 14.0


def test_page_reads_and_writes_get_their_own_labels(service):
    service.session = Session(Response(body={"value": [dict(HIT, **{"chunk": 0})]}, cu=4))
    service.fetch_chunks(chunk_id="p-0", neighbors=0)
    service._post("index", {"value": []}, 30)
    service._post("search", {"search": "*"}, 30)
    assert set(ss.compute_unit_stats()) == {"page_chunks", "write", "other"}


def test_health_reports_the_compute_units_and_the_settings_in_force(service, monkeypatch):
    run(service, Session(Response(cu=6.5)))
    monkeypatch.setattr(settings, "search_max_text_recall", 500)
    body = json.loads(fa.health._function._func(__import__("azure.functions", fromlist=["x"]).HttpRequest(
        "GET", "/api/health", headers={}, body=b"")).get_body())
    assert body["ai_search_compute"]["query"]["total"] == 6.5
    assert body["search_tuning"] == {"max_text_recall": 500, "vector_k": None, "count": True, "search_fields": None}


# ---- the query itself ----

def test_the_default_query_is_unchanged(service):
    session = Session(Response(body={"value": [HIT], "@odata.count": 7}))
    result = run(service, session)
    body = session.bodies[0]
    assert body["vectorQueries"][0]["k"] == 50 and body["top"] == 5 and body["count"] is True
    assert "hybridSearch" not in body and "searchFields" not in body
    assert body["searchMode"] == "any" and result["total_matches"] == 7 and result["count"] == 1


def test_the_default_k_still_scales_with_a_large_limit(service):
    session = Session(Response())
    run(service, session, limit=8)
    assert session.bodies[0]["vectorQueries"][0]["k"] == 80


def test_max_text_recall_is_sent_when_set(service, monkeypatch):
    monkeypatch.setattr(settings, "search_max_text_recall", 500)
    session = Session(Response())
    run(service, session)
    assert session.bodies[0]["hybridSearch"] == {"maxTextRecallSize": 500}


def test_vector_k_overrides_the_default_but_never_goes_below_the_limit(service, monkeypatch):
    monkeypatch.setattr(settings, "search_vector_k", 20)
    session = Session(Response())
    run(service, session)
    assert session.bodies[0]["vectorQueries"][0]["k"] == 20
    monkeypatch.setattr(settings, "search_vector_k", 3)
    run(service, session)
    assert session.bodies[-1]["vectorQueries"][0]["k"] == 5


def test_count_can_be_turned_off(service, monkeypatch):
    monkeypatch.setattr(settings, "search_count", False)
    session = Session(Response(body={"value": [HIT]}))
    result = run(service, session)
    assert "count" not in session.bodies[0] and result["total_matches"] is None and result["count"] == 1


def test_search_fields_are_sent_when_set(service, monkeypatch):
    monkeypatch.setattr(settings, "search_fields", "title,content")
    session = Session(Response())
    run(service, session)
    assert session.bodies[0]["searchFields"] == "title,content"


def test_a_service_that_rejects_hybrid_search_options_does_not_break_search(service, monkeypatch):
    monkeypatch.setattr(settings, "search_max_text_recall", 500)
    session = Session(Response(status=400), Response(body={"value": [HIT]}, cu=9))
    result = run(service, session)
    assert result["count"] == 1
    assert "hybridSearch" in session.bodies[0] and "hybridSearch" not in session.bodies[1]
    assert ss._hybrid_option_rejected[0] is True
    run(service, session)  # remembered: later searches do not send it again
    assert "hybridSearch" not in session.bodies[-1]


def test_a_400_without_the_option_is_still_an_error(service):
    with pytest.raises(requests.HTTPError):
        run(service, Session(Response(status=400)))


def test_the_settings_parse_and_clamp(monkeypatch):
    from services import config
    monkeypatch.setenv("SEARCH_MAX_TEXT_RECALL", "99999")
    assert config._int("SEARCH_MAX_TEXT_RECALL", 0, 0, 10000) == 10000
    monkeypatch.setenv("SEARCH_VECTOR_K", "abc")
    assert config._int("SEARCH_VECTOR_K", 0, 0, 1000) == 0


# ---- the opt-in result cache ----

@pytest.fixture
def cached(service, monkeypatch):
    monkeypatch.setattr(ss, "_query_cache", {})
    monkeypatch.setattr(ss, "_query_stats", {"hits": 0, "misses": 0, "saved": 0.0})
    monkeypatch.setattr(settings, "search_cache_seconds", 600)
    monkeypatch.setattr(settings, "search_cache_max_entries", 500)
    return service


def ask(service, session, query="week 3 agenda", concepts=("circle",), domains=("sujeeth.org",), limit=5):
    service.session = session
    return service.search(query, concepts=list(concepts), domains=list(domains), limit=limit)


def test_the_cache_is_off_by_default(service):
    session = Session(Response(body={"value": [HIT]}, cu=10))
    ask(service, session)
    ask(service, session)
    assert len(session.bodies) == 2 and ss.query_cache_stats()["enabled"] is False


def test_a_repeated_question_is_answered_from_memory(cached):
    session = Session(Response(body={"value": [HIT]}, cu=10))
    first = ask(cached, session)
    second = ask(cached, session)
    assert len(session.bodies) == 1 and second["results"] == first["results"]
    stats = ss.query_cache_stats()
    assert (stats["hits"], stats["misses"], stats["hit_rate"], stats["entries"]) == (1, 1, 0.5, 1)
    assert stats["compute_units_saved"] == 10.0


def test_case_spacing_and_trailing_punctuation_do_not_matter_but_words_and_digits_do(cached):
    session = Session(Response(body={"value": [HIT]}, cu=5))
    ask(cached, session, "What happens in Week 3?")
    ask(cached, session, "  what happens in   week 3 ")
    assert len(session.bodies) == 1
    ask(cached, session, "What happens in Week 4?")
    assert len(session.bodies) == 2


def test_the_callers_own_wording_comes_back_on_a_hit(cached):
    session = Session(Response(body={"value": [HIT]}))
    ask(cached, session, "Week 3 agenda?")
    assert ask(cached, session, "week 3 agenda")["query"] == "week 3 agenda"


def test_different_concepts_domains_or_limits_are_separate_entries(cached):
    session = Session(Response(body={"value": [HIT]}))
    ask(cached, session)
    ask(cached, session, concepts=("circle", "mentor"))
    ask(cached, session, domains=("sujeeth.org", "topics.sujeeth.org"))
    ask(cached, session, limit=10)
    assert len(session.bodies) == 4
    ask(cached, session, concepts=("MENTOR", "circle"))  # order and case of the sets do not matter
    assert len(session.bodies) == 4


def test_changing_a_returned_answer_does_not_change_the_cached_one(cached):
    session = Session(Response(body={"value": [HIT]}))
    first = ask(cached, session)
    first["results"].append({"title": "added by a caller"})
    first["results"][0]["spotlight"] = True
    again = ask(cached, session)
    assert len(again["results"]) == 1 and "spotlight" not in again["results"][0]


def test_a_search_that_found_nothing_is_not_kept(cached):
    session = Session(Response(body={"value": []}))
    ask(cached, session)
    ask(cached, session)
    assert len(session.bodies) == 2 and ss.query_cache_stats()["entries"] == 0


def test_a_failed_search_is_not_kept(cached):
    with pytest.raises(requests.HTTPError):
        ask(cached, Session(Response(status=500)))
    session = Session(Response(body={"value": [HIT]}))
    assert ask(cached, session)["count"] == 1 and len(session.bodies) == 1


def test_an_expired_answer_is_fetched_again(cached):
    session = Session(Response(body={"value": [HIT]}))
    ask(cached, session)
    key = next(iter(ss._query_cache))
    ss._query_cache[key] = (ss._query_cache[key][0] - 10_000, *ss._query_cache[key][1:])
    ask(cached, session)
    assert len(session.bodies) == 2


def test_the_cache_is_bounded(cached, monkeypatch):
    monkeypatch.setattr(settings, "search_cache_max_entries", 10)
    session = Session(Response(body={"value": [HIT]}))
    for i in range(40):
        ask(cached, session, f"question {i}")
    assert ss.query_cache_stats()["entries"] <= 10


def test_an_index_write_clears_the_cache(cached):
    session = Session(Response(body={"value": [HIT]}))
    ask(cached, session)
    assert ss.query_cache_stats()["entries"] == 1
    cached.session = Session(Response(body={"value": [{"key": "p-0", "status": True, "statusCode": 200}]}))
    cached._write_batch([{"id": "p-0", "@search.action": "upload"}])
    assert ss.query_cache_stats()["entries"] == 0


def test_a_hit_without_a_reported_cost_saves_nothing_and_does_not_fail(cached):
    session = Session(Response(body={"value": [HIT]}))  # no header, as on a tier that does not report it
    ask(cached, session)
    ask(cached, session)
    assert ss.query_cache_stats()["hits"] == 1 and ss.query_cache_stats()["compute_units_saved"] == 0.0


def test_the_best_bet_and_spotlight_queries_are_labelled(service, monkeypatch):
    seen = []
    monkeypatch.setattr(fa.searcher, "search", lambda *a, **k: seen.append(k.get("label")) or
                        {"results": [dict(HIT, url="https://sujeeth.org/spot")], "count": 1})
    monkeypatch.setattr(fa, "best_bet_domains", lambda manifest, domains: ["manual.example"])
    monkeypatch.setattr(fa.ontology, "search_manifest", lambda: {})
    monkeypatch.setattr(fa, "BEST_BETS_MAX", 2)
    monkeypatch.setattr(fa, "SPOTLIGHT_SLOTS", 2)
    result = {"results": [], "count": 0}
    fa.add_best_bets(result, "q", ["circle"], ["sujeeth.org"])
    fa.add_spotlights(result, "q", {"matched_concepts": ["circle", "spotlight"]}, ["sujeeth.org"])
    assert seen == ["best_bets", "spotlight"]


def test_health_reports_the_cache(cached):
    ask(cached, Session(Response(body={"value": [HIT]}, cu=3)))
    import azure.functions as func
    body = json.loads(fa.health._function._func(func.HttpRequest("GET", "/api/health", headers={}, body=b"")).get_body())
    assert body["query_cache"]["enabled"] is True and body["query_cache"]["misses"] == 1
