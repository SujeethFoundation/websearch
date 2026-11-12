"""Ranking beyond the Azure-style fusion: the question's own concepts count, and one page cannot fill the answer."""
import json

import azure.functions as func
import numpy as np
import pytest

import function_app as fa
from services import local_index as li
from services import search_service as ss
from services.config import settings
from test_local_index import AzureSession, azure_configured, conf, filled_store, index_of, make_doc, make_service  # noqa: F401
from test_local_rescoring import VectorEmbedder

DIMS = 64
NO_TERMS = np.zeros(0, dtype=np.uint64)
DOMAINS = ["sujeeth.org", "www.sujeeth.org", "www.themuse.com"]


def ids(index, hits):
    return [index.ids[row] for row, _ in hits]


def week4_corpus():
    """The live case: the Foundation's own Week 4 chunk is tagged week_4; six third-party chunks about mentors pass the
    filter only through 'networking', a concept the ontology added by relationship, and match the words and the vector better."""
    rng = np.random.default_rng(8)
    q = rng.standard_normal(DIMS)
    near = lambda scale: q + scale * rng.standard_normal(DIMS)
    docs = [make_doc(1, 0, title="Career Circle", content="week 4 networking strategies work out loud students", domain="sujeeth.org",
                     concepts=("week_4", "circle"), vector=near(1.6))]
    for i in range(2, 8):
        docs.append(make_doc(i, 0, title=f"How to keep a mentor {i}", content="what a mentor does every week mentor mentor do",
                             domain="www.themuse.com", concepts=("networking", "career_development"), vector=near(0.3)))
    return q, docs


QUESTION = "what does a mentor do in week 4"
FILTER = ["week_4", "career_mentor", "networking"]
OWN = {"week_4": 1.2, "career_mentor": 1.1}                       # what the question itself named; networking came by relationship


def run(index, q, **kw):
    return index.search(li.query_terms(QUESTION), li.binary_code(q), DOMAINS, FILTER, candidates=50, limit=5,
                        qvec=li.quantize_int8(q), text_candidates=1000, **kw)[0]


def test_one_key_for_a_page_however_it_was_reached():
    same = {li.page_key(u) for u in ("https://www.sujeeth.org/circle", "https://sujeeth.org/circle/", "http://SUJEETH.org/circle#week-4",
                                     "sujeeth.org/circle")}
    assert same == {"sujeeth.org/circle"}
    assert li.page_key("https://sujeeth.org/circle") != li.page_key("https://sujeeth.org/circles")
    assert li.page_key("https://topics.sujeeth.org/") == "topics.sujeeth.org" and li.page_key(None) == ""
    assert li.page_key("https://x.org/a?b=1") == "x.org/a?b=1"                # the query string is part of the page


def test_without_the_bonus_the_third_party_chunks_outrank_the_foundations_week_4_chunk(conf):
    q, docs = week4_corpus()
    index = index_of(docs)
    assert ids(index, run(index, q))[0] != "page1-0" and "page1-0" not in ids(index, run(index, q))[:3]


def test_a_chunk_tagged_with_what_the_question_named_ranks_first(conf):
    q, docs = week4_corpus()
    index = index_of(docs)
    boosted = run(index, q, concept_weights=OWN, concept_boost=1.0)
    assert ids(index, boosted)[0] == "page1-0"
    assert len(boosted) == 5                                           # the others are still returned, just lower


def test_no_weights_or_a_zero_boost_changes_nothing(conf):
    q, docs = week4_corpus()
    index = index_of(docs)
    base = run(index, q)
    assert run(index, q, concept_weights=OWN, concept_boost=0.0) == base
    assert run(index, q, concept_weights={}, concept_boost=1.0) == base
    assert run(index, q, concept_weights={"unknown_concept": 1.0}, concept_boost=1.0) == base


def test_the_bonus_grows_with_the_share_of_the_questions_concepts_a_chunk_carries(conf):
    v = np.ones(DIMS)
    docs = [make_doc(1, 0, content="same words here", concepts=("week_4",), vector=v),
            make_doc(2, 0, content="same words here", concepts=("week_4", "career_mentor"), vector=v),
            make_doc(3, 0, content="same words here", concepts=("networking",), vector=v)]
    index = index_of(docs)
    rows = np.arange(3)
    share = index.concept_coverage(rows, OWN).tolist()
    assert share[0] == pytest.approx(1.2 / 2.3) and share[1] == pytest.approx(1.0) and share[2] == 0.0
    hits, _ = index.search(li.query_terms("same words"), li.binary_code(v), ["sujeeth.org"], FILTER, limit=3,
                           qvec=li.quantize_int8(v), concept_weights=OWN, concept_boost=1.0)
    assert ids(index, hits) == ["page2-0", "page1-0", "page3-0"]


def test_the_bonus_reorders_candidates_and_never_adds_a_chunk_that_matched_nothing(conf):
    q = np.ones(DIMS)
    docs = [make_doc(1, 0, content="zebra", concepts=("week_4",), vector=-q)]
    docs += [make_doc(i, 0, content="apple banana", concepts=("networking",), vector=q) for i in range(2, 60)]
    index = index_of(docs)
    hits, _ = index.search(li.query_terms("apple"), li.binary_code(q), ["sujeeth.org"], FILTER, candidates=50, limit=5,
                           qvec=li.quantize_int8(q), concept_weights=OWN, concept_boost=5.0)
    assert "page1-0" not in ids(index, hits)                           # no keyword match and not among the 50 nearest vectors


# ---- one page cannot fill the answer

def page_corpus():
    q = np.ones(DIMS)
    docs = [make_doc(1, n, content="thank you email templates " * (5 - n), vector=q) for n in range(4)]     # four chunks of one page
    docs += [make_doc(2, 0, content="thank you email", vector=q * 0.5 + 0.1), make_doc(3, 0, content="thank you", vector=-q)]
    return q, docs


def test_at_most_two_chunks_of_one_page_by_default_so_other_pages_get_a_place(conf):
    q, docs = page_corpus()
    index = index_of(docs)
    terms, qcode, qvec = li.query_terms("thank you email templates"), li.binary_code(q), li.quantize_int8(q)
    free, _ = index.search(terms, qcode, ["sujeeth.org"], ["circle"], limit=4, qvec=qvec)
    assert [i.split("-")[0] for i in ids(index, free)] == ["page1"] * 4
    capped, matching = index.search(terms, qcode, ["sujeeth.org"], ["circle"], limit=4, qvec=qvec, max_per_page=2)
    assert [i.split("-")[0] for i in ids(index, capped)] == ["page1", "page1", "page2", "page3"] and matching == 6
    one, _ = index.search(terms, qcode, ["sujeeth.org"], ["circle"], limit=3, qvec=qvec, max_per_page=1)
    assert [i.split("-")[0] for i in ids(index, one)] == ["page1", "page2", "page3"]


def test_when_too_few_pages_match_the_remaining_places_go_to_more_chunks_of_the_same_pages(conf):
    q, docs = page_corpus()
    index = index_of(docs)
    terms, qcode, qvec = li.query_terms("thank you email templates"), li.binary_code(q), li.quantize_int8(q)
    filled, _ = index.search(terms, qcode, ["sujeeth.org"], ["circle"], limit=5, qvec=qvec, max_per_page=1)
    pages = [i.split("-")[0] for i in ids(index, filled)]
    assert len(filled) == 5 and set(pages) == {"page1", "page2", "page3"} and pages.count("page1") == 3
    assert [s for _, s in filled] == sorted((s for _, s in filled), reverse=True)        # still best first
    only_one_page = index_of([make_doc(1, n, content="alpha beta", vector=q) for n in range(6)])
    hits, _ = only_one_page.search(li.query_terms("alpha"), qcode, ["sujeeth.org"], ["circle"], limit=5, qvec=qvec, max_per_page=2)
    assert len(hits) == 5                                                                # never fewer results than without the limit


def test_the_www_copy_of_a_page_is_the_same_page_and_the_same_chunk_is_returned_once(conf):
    q = np.ones(DIMS)
    docs = []
    for n in range(2):
        plain = make_doc(1, n, content=f"week four networking strategies part {n}", domain="sujeeth.org", vector=q)
        www = dict(plain, id=f"wwwcopy1-{n}", url=plain["url"].replace("https://sujeeth.org", "https://www.sujeeth.org"), domain="www.sujeeth.org")
        docs += [plain, www]
    docs.append(make_doc(2, 0, content="networking strategies", domain="sujeeth.org", vector=q * 0.9 + 0.05))
    index = index_of(docs)
    assert index.distinct_pages == 2 and len(index.pages) == 3        # three crawled page ids, two real pages
    hits, _ = index.search(li.query_terms("networking strategies"), li.binary_code(q), DOMAINS, ["circle"], limit=5,
                           qvec=li.quantize_int8(q), max_per_page=2)
    got = [(li.page_key(index.urls[r]), int(index.chunk_numbers[r])) for r, _ in hits]
    assert len(got) == len(set(got)) == 3                              # chunk 0 and chunk 1 of the page once each, then the other page
    assert sum(1 for page, _ in got if page == "sujeeth.org/p1") == 2


# ---- through the search service and the route

def test_the_service_applies_the_settings_and_reports_how_it_ranked(conf, monkeypatch):
    q, docs = week4_corpus()
    svc = make_service(store=filled_store(docs), embedder=VectorEmbedder(q))
    answer = svc.search(QUESTION, concepts=FILTER, domains=DOMAINS, limit=5, concept_weights=OWN)
    assert answer["results"][0]["chunk_id"] == "page1-0"
    assert answer["ranking"] == {"question_concepts": {"career_mentor": 1.1, "week_4": 1.2}, "concept_boost": 1.0, "max_per_page": 2}
    monkeypatch.setattr(settings, "local_concept_boost", 0.0)
    off = svc.search(QUESTION, concepts=FILTER, domains=DOMAINS, limit=5, concept_weights=OWN)
    assert off["results"][0]["chunk_id"] != "page1-0" and off["ranking"]["question_concepts"] == {}


def test_the_shadow_comparison_ranks_the_plain_way_so_it_still_measures_agreement(conf):
    q, docs = week4_corpus()
    svc = make_service(store=filled_store(docs), embedder=VectorEmbedder(q))
    plain = svc._search_local(QUESTION, FILTER, DOMAINS, 5, concept_weights=OWN, parity=True)
    assert plain["results"][0]["chunk_id"] != "page1-0" and plain["ranking"] == {"question_concepts": {}, "concept_boost": 0.0, "max_per_page": 0}


def test_an_engine_can_be_forced_for_one_search_and_never_falls_back(conf, monkeypatch):
    azure_configured(monkeypatch)
    q, docs = week4_corpus()
    session = AzureSession(hits=[{"id": "az-1", "title": "From Azure", "url": "https://sujeeth.org/a", "domain": "sujeeth.org",
                                  "concepts": ["week_4"], "content": "x", "@search.score": 0.03}])
    svc = make_service(store=filled_store(docs), embedder=VectorEmbedder(q), session=session)
    assert settings.search_backend == "local"
    forced = svc.search(QUESTION, concepts=FILTER, domains=DOMAINS, limit=5, backend="azure")
    assert forced["results"][0]["chunk_id"] == "az-1" and forced.get("backend") != "local"
    monkeypatch.setattr(settings, "search_backend", "azure")
    local = svc.search(QUESTION, concepts=FILTER, domains=DOMAINS, limit=5, backend="local", concept_weights=OWN)
    assert local["backend"] == "local" and local["results"][0]["chunk_id"] == "page1-0"
    assert svc.search(QUESTION, concepts=FILTER, domains=DOMAINS, limit=5, backend="nonsense")["results"][0]["chunk_id"] == "az-1"
    from test_local_index import new_store
    empty = make_service(store=new_store(), session=session)
    with pytest.raises(RuntimeError):
        empty.search(QUESTION, concepts=FILTER, domains=DOMAINS, limit=5, backend="local")      # no quiet fallback when forced


def test_kept_answers_are_separate_for_each_way_of_ranking(conf, monkeypatch):
    monkeypatch.setattr(settings, "search_cache_seconds", 60)
    monkeypatch.setattr(ss, "_query_cache", {})
    q, docs = week4_corpus()
    svc = make_service(store=filled_store(docs), embedder=VectorEmbedder(q))
    with_concepts = svc.search(QUESTION, concepts=FILTER, domains=DOMAINS, limit=5, concept_weights=OWN)
    without = svc.search(QUESTION, concepts=FILTER, domains=DOMAINS, limit=5)
    assert with_concepts["results"][0]["chunk_id"] == "page1-0" and without["results"][0]["chunk_id"] != "page1-0"
    again = svc.search(QUESTION, concepts=FILTER, domains=DOMAINS, limit=5, concept_weights=OWN)
    assert again["results"][0]["chunk_id"] == "page1-0"


RESOLUTION = {
    "matched_concepts": ["week_4", "career_mentor", "networking", "grit"], "domains": ["sujeeth.org"], "ontology_version": "1",
    "concept_matches": [
        {"concept_id": "week_4", "confidence": 1.2, "source": "query_match"},
        {"concept_id": "Career_Mentor", "confidence": None, "source": "explicit"},
        {"concept_id": "networking", "confidence": None, "source": "relationship"},
        {"concept_id": "grit", "confidence": None, "source": "relationship"},
    ],
}


def test_only_the_concepts_the_question_named_get_a_weight():
    assert fa.question_concept_weights(RESOLUTION) == {"week_4": 1.2, "career_mentor": 1.0}
    assert fa.question_concept_weights({}) == {} and fa.question_concept_weights({"concept_matches": [{"source": "query_match"}]}) == {}


def test_the_search_route_passes_the_weights_and_an_engine_choice(conf, monkeypatch):
    seen = {}

    class Searcher:
        def search(self, query, **kw):
            seen.update(kw, query=query)
            return {"query": query, "count": 0, "results": []}
    monkeypatch.setattr(fa, "searcher", Searcher())
    monkeypatch.setattr(fa.concept_resolver, "resolve_query", lambda **kw: dict(RESOLUTION))
    monkeypatch.setattr(fa, "add_best_bets", lambda *a, **k: None)
    monkeypatch.setattr(fa, "add_spotlights", lambda *a, **k: None)
    post = func.HttpRequest("POST", "/api/search", params={}, headers={}, body=json.dumps({"query": "what does a mentor do in week 4", "backend": "LOCAL"}).encode())
    assert fa.search_post._function._func(post).status_code == 200
    assert seen["concept_weights"] == {"week_4": 1.2, "career_mentor": 1.0} and seen["backend"] == "local"
    get = func.HttpRequest("GET", "/api/search", params={"q": "week 4", "backend": "azure"}, headers={}, body=b"")
    fa.search_get._function._func(get)
    assert seen["backend"] == "azure"
    plain = func.HttpRequest("GET", "/api/search", params={"q": "week 4", "backend": "other"}, headers={}, body=b"")
    fa.search_get._function._func(plain)
    assert seen["backend"] is None
