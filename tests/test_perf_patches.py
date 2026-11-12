"""Tests for the search speed and caching changes: question embedding (single flight, cooldown), the answer cache
(degraded answers, invalidation), the filter-row cache, the ontology cache (serve stale, one fetch) and the concept
resolver (precomputed aliases, background rebuild)."""
import copy
import random
import threading
import time
import types

import numpy as np
import pytest

from services import concept_resolver as cr
from services import embedding_service as es
from services import local_index as li
from services import search_service as ss
from services.config import settings
from services.ontology_client import OntologyClient

# Shared fakes and fixtures from the local index tests.
from test_local_index import (  # noqa: F401  (conf is a fixture)
    ASK, DIMS, Embedder, conf, corpus, embedding_service, entities, filled_store, make_service, wait_for,
)


# ---------------------------------------------------------------------------------------------------------------
# Question embeddings
# ---------------------------------------------------------------------------------------------------------------

def test_requests_for_the_same_new_question_share_one_embedding_call(monkeypatch):
    service, _ = embedding_service(monkeypatch)
    calls, gate = [], threading.Event()

    def slow(text):
        calls.append(text)
        gate.wait(2)
        return [0.1] * DIMS

    service._request_query_vector = slow
    results = []
    threads = [threading.Thread(target=lambda: results.append(service.embed_query("How do I find a mentor?")))
               for _ in range(6)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    gate.set()
    for t in threads:
        t.join(3)
    assert len(calls) == 1 and len(results) == 6


def test_waiting_requests_get_the_error_of_the_one_call_that_failed(monkeypatch):
    service, _ = embedding_service(monkeypatch)
    monkeypatch.setattr(settings, "embed_query_cooldown", 0)
    gate = threading.Event()

    def boom(text):
        gate.wait(2)
        raise RuntimeError("the embedding service is down")

    service._request_query_vector = boom
    errors = []

    def ask():
        try:
            service.embed_query("same question")
        except Exception as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=ask) for _ in range(4)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    gate.set()
    for t in threads:
        t.join(3)
    assert errors == ["the embedding service is down"] * 4


def test_after_a_failure_questions_skip_the_endpoint_until_the_cooldown_ends(monkeypatch):
    service, _ = embedding_service(monkeypatch)
    monkeypatch.setattr(settings, "embed_query_cooldown", 30)
    calls = []

    def boom(text):
        calls.append(text)
        raise RuntimeError("down")

    service._request_query_vector = boom
    with pytest.raises(RuntimeError, match="down"):
        service.embed_query("first")
    for question in ("second", "third"):
        with pytest.raises(RuntimeError, match="paused"):
            service.embed_query(question)
    assert calls == ["first"]                       # the later ones never reached the endpoint
    service._down_until = 0.0                       # the cooldown is over
    service._request_query_vector = lambda text: [0.1] * DIMS
    assert len(service.embed_query("fourth")) == DIMS


def test_a_remembered_question_is_still_answered_during_the_cooldown(monkeypatch):
    service, _ = embedding_service(monkeypatch)
    first = service.embed_query("known question")
    service._down_until = time.monotonic() + 60
    assert service.embed_query("Known   question?") == first


def test_the_cooldown_can_be_turned_off(monkeypatch):
    service, _ = embedding_service(monkeypatch)
    monkeypatch.setattr(settings, "embed_query_cooldown", 0)
    calls = []
    service._request_query_vector = lambda text: calls.append(text) or (_ for _ in ()).throw(RuntimeError("down"))
    for _ in range(3):
        with pytest.raises(RuntimeError, match="down"):
            service.embed_query("same")
    assert len(calls) == 3


# ---------------------------------------------------------------------------------------------------------------
# The answer cache
# ---------------------------------------------------------------------------------------------------------------

@pytest.fixture
def cache_on(conf, monkeypatch):
    monkeypatch.setattr(settings, "search_cache_seconds", 600)
    monkeypatch.setattr(settings, "search_cache_max_entries", 500)
    monkeypatch.setattr(ss, "_query_cache", {})
    monkeypatch.setattr(ss, "_query_stats", {"hits": 0, "misses": 0, "saved": 0.0})
    monkeypatch.setattr(ss, "_query_generation", [0])


def cached_service(embedder=None):
    svc = make_service(embedder=embedder)
    svc.local.on_swap = ss._query_cache_clear   # what SearchService.__init__ does
    return svc


def test_a_keyword_only_answer_is_not_kept(cache_on):
    embedder = Embedder(fail=True)
    svc = cached_service(embedder)
    first = svc.search("w5 w60", **ASK)
    assert first["search_mode"] == "keyword" and ss.query_cache_stats()["entries"] == 0
    embedder.fail = False                                   # the embedding service is back
    second = svc.search("w5 w60", **ASK)
    assert second["search_mode"] == "hybrid" and ss.query_cache_stats()["entries"] == 1


def test_a_hybrid_answer_is_kept(cache_on):
    embedder = Embedder()
    svc = cached_service(embedder)
    svc.search("w5 w60", **ASK)
    svc.search("W5  w60?", **ASK)
    assert len(embedder.query_calls) == 1 and ss.query_cache_stats()["hits"] == 1


def test_writing_chunks_on_this_instance_clears_the_cache(cache_on):
    svc = cached_service()
    svc.search("w5 w60", **ASK)
    assert ss.query_cache_stats()["entries"] == 1
    docs = corpus(pages=1)
    svc._local_write([{**d, settings.search_vector_field: [0.5] * DIMS} for d in docs])
    assert ss.query_cache_stats()["entries"] == 0


def test_deleting_chunks_on_this_instance_clears_the_cache(cache_on):
    svc = cached_service()
    svc.search("w5 w60", **ASK)
    assert ss.query_cache_stats()["entries"] == 1
    svc._local_delete([d["id"] for d in corpus(pages=1)])
    assert ss.query_cache_stats()["entries"] == 0


def test_a_rebuilt_index_clears_the_cache(cache_on):
    store = filled_store(corpus(pages=4))
    svc = make_service(store=store)
    svc.local.on_swap = ss._query_cache_clear
    svc.search("w5 w60", **ASK)
    assert ss.query_cache_stats()["entries"] == 1
    store.upsert_entities(entities(corpus(pages=6)))
    svc.local.reload()
    assert ss.query_cache_stats()["entries"] == 0


def test_an_answer_from_before_a_clear_is_not_stored_after_it(cache_on):
    svc = cached_service()
    original = svc._search_local

    def slow_then_cleared(*a, **k):
        answer = original(*a, **k)
        ss._query_cache_clear()          # the index was replaced while this search was running
        return answer

    svc._search_local = slow_then_cleared
    svc.search("w5 w60", **ASK)
    assert ss.query_cache_stats()["entries"] == 0


def test_cache_hits_do_not_share_state_with_each_other(cache_on):
    svc = cached_service()
    first = svc.search("w5 w60", **ASK)
    second = svc.search("w5 w60", **ASK)
    second["results"].append({"title": "added by a caller"})
    third = svc.search("w5 w60", **ASK)
    assert len(third["results"]) == len(first["results"])


def test_a_failed_listener_does_not_stop_a_reload(conf):
    manager = li.LocalIndexManager(store=filled_store())
    manager.on_swap = lambda: 1 / 0
    assert manager.current().n == 100


# ---------------------------------------------------------------------------------------------------------------
# The filter-row cache
# ---------------------------------------------------------------------------------------------------------------

def test_the_filter_rows_are_kept_per_snapshot_and_match_the_mask(conf):
    index = li.LocalIndexManager(store=filled_store()).current()
    doms, cons = ["sujeeth.org", "themuse.com"], ["circle", "mentor"]
    rows = index._rows(doms, cons)
    assert np.array_equal(rows, np.flatnonzero(index._mask(doms, cons)))
    assert index._rows(["themuse.com", "sujeeth.org"], ["MENTOR", "circle"]) is rows   # order and case do not matter
    assert len(index._rows_cache) == 1


def test_the_filter_row_cache_is_bounded(conf, monkeypatch):
    index = li.LocalIndexManager(store=filled_store()).current()
    monkeypatch.setattr(li.LocalIndex, "ROWS_CACHE_MAX", 3)
    for i in range(10):
        index._rows(["sujeeth.org"], [f"c{i}"])
    assert len(index._rows_cache) <= 3


def test_a_new_snapshot_starts_with_an_empty_filter_cache(conf):
    store = filled_store()
    manager = li.LocalIndexManager(store=store)
    manager.current()._rows(["sujeeth.org"], ["circle"])
    manager.reload()
    assert manager.current()._rows_cache == {}


# ---------------------------------------------------------------------------------------------------------------
# The ontology cache
# ---------------------------------------------------------------------------------------------------------------

class SlowOntology(OntologyClient):
    def __init__(self):
        super().__init__()
        self.calls, self.fail, self.gate = [], False, None

    def get(self, path):
        self.calls.append(path)
        if self.gate:
            self.gate.wait(2)
        if self.fail:
            raise RuntimeError("the ontology app is down")
        return {"version": len(self.calls)}


@pytest.fixture
def ttl(monkeypatch):
    monkeypatch.setattr(settings, "ontology_cache_seconds", 120)
    monkeypatch.setattr(settings, "ontology_cache_max_entries", 100)


def expire(client):
    with client._cache_lock:
        for key, (_, value) in list(client._cache.items()):
            client._cache[key] = (time.monotonic() - 1, value)


def test_concurrent_first_requests_make_one_fetch(ttl):
    client = SlowOntology()
    client.gate = threading.Event()
    out = []
    threads = [threading.Thread(target=lambda: out.append(client.search_manifest())) for _ in range(6)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    client.gate.set()
    for t in threads:
        t.join(3)
    assert len(client.calls) == 1 and len(out) == 6


def test_an_expired_answer_is_served_at_once_and_refreshed_in_the_background(ttl):
    client = SlowOntology()
    assert client.search_manifest() == {"version": 1}
    expire(client)
    client.gate = threading.Event()                 # the refresh will take a while
    began = time.monotonic()
    assert client.search_manifest() == {"version": 1}   # the old answer, without waiting for the refresh
    assert time.monotonic() - began < 0.5
    client.gate.set()
    assert wait_for(lambda: client.search_manifest() == {"version": 2})


def test_a_failed_refresh_keeps_serving_the_old_answer(ttl):
    client = SlowOntology()
    client.search_manifest()
    expire(client)
    client.fail = True
    assert client.search_manifest() == {"version": 1}
    time.sleep(0.2)
    assert client.search_manifest() == {"version": 1}
    client.fail = False
    assert wait_for(lambda: client.search_manifest()["version"] > 1)   # a later request refreshed it


def test_a_failed_first_fetch_is_raised_and_not_kept(ttl):
    client = SlowOntology()
    client.fail = True
    with pytest.raises(RuntimeError):
        client.search_manifest()
    client.fail = False
    assert client.search_manifest()["version"] == 2


# ---------------------------------------------------------------------------------------------------------------
# The concept resolver
# ---------------------------------------------------------------------------------------------------------------

def reference_score(query, item, terminology):
    """The scoring as it was before the aliases were prepared in advance."""
    query_norm, query_tokens = cr.normalize(query), cr.tokens(query)
    concept_id = str(item.get("id") or item.get("name") or "").strip()
    values = [item.get("name", ""), item.get("id", ""), item.get("definition", "")]
    for field in ("aliases", "synonyms", "alternative_terms", "alternate_names", "query_phrases"):
        v = item.get(field, [])
        values.extend([v] if isinstance(v, str) else v if isinstance(v, list) else [])
    for term in terminology:
        if not isinstance(term, dict):
            continue
        target = term.get("concept_id") or term.get("class_id") or term.get("concept") or term.get("id")
        if str(target).lower() == concept_id.lower():
            for field in ("preferred", "preferred_term", "term", "label"):
                if term.get(field):
                    values.append(term[field])
            for field in ("aliases", "synonyms", "alternative_terms"):
                v = term.get(field, [])
                values.extend([v] if isinstance(v, str) else v if isinstance(v, list) else [])
    best, matched = 0.0, None
    for alias in [str(v) for v in values if v]:
        alias_norm, alias_tokens = cr.normalize(alias), cr.tokens(alias)
        if not alias_norm:
            continue
        if alias_norm in query_norm:
            score = 1.0 + min(len(alias_tokens), 4) * 0.1
        else:
            overlap = len(query_tokens & alias_tokens)
            score = overlap / max(1, len(alias_tokens)) * 0.65
            if overlap == 1 and len(alias_tokens) > 2:
                score *= 0.45
        if score > best:
            best, matched = score, alias
    return best, matched


WORDS = ("mentor circle career resume interview salary week one two three agenda session volunteer student donor "
         "partner network skills job offer negotiation college major internship project").split()


def random_catalog(seed):
    rng = random.Random(seed)
    phrase = lambda lo, hi: " ".join(rng.choice(WORDS) for _ in range(rng.randint(lo, hi)))
    classes, terminology = [], []
    for i in range(25):
        cid = f"concept_{i}"
        classes.append({"id": cid, "name": phrase(1, 3).title(), "definition": phrase(4, 9),
                        "aliases": [phrase(1, 3) for _ in range(rng.randint(0, 3))],
                        "query_phrases": [phrase(2, 4)] if rng.random() < .4 else [],
                        "synonyms": phrase(1, 2) if rng.random() < .3 else []})
        if rng.random() < .6:
            terminology.append({"concept_id": cid, "preferred": phrase(1, 2), "aliases": [phrase(1, 3), phrase(2, 3)]})
    terminology.append("not a dict")
    return classes, terminology


class FakeOntology:
    def __init__(self, classes, terminology, relationships=()):
        self.classes_, self.terms, self.rels = classes, terminology, list(relationships)
        self.loads = 0
        self.gate = None
        self.fail = False

    def ontology(self):
        self.loads += 1
        if self.gate:
            self.gate.wait(2)
        if self.fail:
            raise RuntimeError("ontology down")
        return {"classes": self.classes_, "ontology_version": f"v{self.loads}"}

    def classes(self):
        return {"classes": self.classes_}

    def terminology(self):
        return {"terminology": self.terms}

    def relationships(self):
        return {"relationships": self.rels}

    def resolve_domains(self, concepts, purpose="search"):
        return {"domains": [{"domain": "sujeeth.org"}], "ontology_version": "v"}

    def validate_domains(self, domains, purpose="search"):
        return {"approved": list(domains), "rejected": []}


@pytest.mark.parametrize("seed", range(5))
def test_prepared_aliases_score_exactly_like_the_old_per_search_work(seed):
    classes, terminology = random_catalog(seed)
    resolver = cr.ConceptResolver(FakeOntology(classes, terminology))
    catalog = resolver._load_catalog()
    rng = random.Random(seed + 100)
    for _ in range(60):
        query = " ".join(rng.choice(WORDS + ["the", "how", "do", "I"]) for _ in range(rng.randint(1, 7)))
        qn, qt = cr.normalize(query), set(cr.normalize(query).split())
        got = {cid: resolver._score_indexed(qn, qt, aliases) for cid, aliases in catalog["query_index"]}
        for item in classes:
            assert got[item["id"]] == reference_score(query, item, terminology), (query, item["id"])


def test_tagging_matches_the_old_rules(seed=1):
    classes, terminology = random_catalog(seed)
    resolver = cr.ConceptResolver(FakeOntology(classes, terminology))
    allowed = [c["id"] for c in classes]
    assert resolver.tag_document("Mentor circle agenda", "week one mentor circle agenda session", allowed)
    # single words must be whole tokens; query phrases and terminology never tag page content
    solo = cr.ConceptResolver(FakeOntology(
        [{"id": "resume", "name": "Resume", "query_phrases": ["getting started"]}],
        [{"concept_id": "resume", "preferred": "cv polish"}]))
    assert solo.tag_document("t", "your resumes are great", ["resume"]) == []
    assert solo.tag_document("t", "update your resume today", ["resume"]) == ["resume"]
    assert solo.tag_document("t", "getting started with cv polish", ["resume"]) == []


def test_resolving_a_question_still_works_end_to_end():
    classes, terminology = random_catalog(2)
    resolver = cr.ConceptResolver(FakeOntology(classes, terminology))
    answer = resolver.resolve_query("how do I prepare for a salary negotiation interview")
    assert answer["status"] in ("resolved", "no_match")
    if answer["status"] == "resolved":
        assert answer["domains"] == ["sujeeth.org"] and answer["matched_concepts"]


def test_the_first_load_makes_concurrent_requests_wait_for_one_build():
    ontology = FakeOntology(*random_catalog(3))
    ontology.gate = threading.Event()
    resolver = cr.ConceptResolver(ontology)
    out = []
    threads = [threading.Thread(target=lambda: out.append(resolver._load_catalog())) for _ in range(5)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    ontology.gate.set()
    for t in threads:
        t.join(3)
    assert ontology.loads == 1 and len(out) == 5 and all(c is out[0] for c in out)


def test_an_out_of_date_catalog_is_served_while_a_new_one_is_built():
    ontology = FakeOntology(*random_catalog(4))
    resolver = cr.ConceptResolver(ontology, ttl_seconds=60)
    old = resolver._load_catalog()
    resolver._loaded_at = time.monotonic() - 120          # past its lifetime
    ontology.gate = threading.Event()
    began = time.monotonic()
    assert resolver._load_catalog() is old                # no waiting for the ontology app
    assert time.monotonic() - began < 0.5
    ontology.gate.set()
    assert wait_for(lambda: resolver._catalog is not old)


def test_a_failed_rebuild_keeps_the_old_catalog_and_tries_again_later():
    ontology = FakeOntology(*random_catalog(5))
    resolver = cr.ConceptResolver(ontology, ttl_seconds=60)
    old = resolver._load_catalog()
    resolver._loaded_at = time.monotonic() - 120
    ontology.fail = True
    assert resolver._load_catalog() is old
    assert wait_for(lambda: not resolver._build_lock.locked())
    assert resolver._catalog is old
    assert time.monotonic() - resolver._loaded_at < 60    # pushed out so the next try is about a minute away
    ontology.fail = False
    resolver._loaded_at = time.monotonic() - 120
    resolver._load_catalog()
    assert wait_for(lambda: resolver._catalog is not old)


def test_the_first_load_failing_is_raised():
    ontology = FakeOntology(*random_catalog(6))
    ontology.fail = True
    resolver = cr.ConceptResolver(ontology)
    with pytest.raises(RuntimeError, match="ontology down"):
        resolver._load_catalog()
    ontology.fail = False
    assert resolver._load_catalog()["classes"]
