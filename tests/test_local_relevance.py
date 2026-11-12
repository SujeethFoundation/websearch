"""Ranking beyond the Azure-style fusion: the question's own concepts count, and one page cannot fill the answer."""
import json

import azure.functions as func
import numpy as np
import pytest

import function_app as fa
from services import local_index as li
from services import search_service as ss
from services.config import settings
from test_local_index import conf, filled_store, index_of, make_doc, make_service  # noqa: F401
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
    monkeypatch.setattr(settings, "local_phrase_boost", 0.0)          # this test is about the concept bonus alone
    q, docs = week4_corpus()
    svc = make_service(store=filled_store(docs), embedder=VectorEmbedder(q))
    answer = svc.search(QUESTION, concepts=FILTER, domains=DOMAINS, limit=5, concept_weights=OWN)
    assert answer["results"][0]["chunk_id"] == "page1-0"
    assert answer["ranking"] == {"question_concepts": {"career_mentor": 1.1, "week_4": 1.2}, "concept_boost": 1.0,
                                 "phrase_boost": 0.0, "max_per_page": 2, "unique_pages": True}
    monkeypatch.setattr(settings, "local_concept_boost", 0.0)
    off = svc.search(QUESTION, concepts=FILTER, domains=DOMAINS, limit=5, concept_weights=OWN)
    assert off["results"][0]["chunk_id"] != "page1-0" and off["ranking"]["question_concepts"] == {}


def test_kept_answers_are_separate_for_each_way_of_ranking(conf, monkeypatch):
    monkeypatch.setattr(settings, "local_phrase_boost", 0.0)          # the concept bonus alone decides here
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


def test_the_search_route_passes_the_questions_own_concepts_as_weights(conf, monkeypatch):
    seen = {}

    class Searcher:
        def search(self, query, **kw):
            seen.update(kw, query=query)
            return {"query": query, "count": 0, "results": []}
    monkeypatch.setattr(fa, "searcher", Searcher())
    monkeypatch.setattr(fa.concept_resolver, "resolve_query", lambda **kw: dict(RESOLUTION))
    monkeypatch.setattr(fa, "add_best_bets", lambda *a, **k: None)
    monkeypatch.setattr(fa, "add_spotlights", lambda *a, **k: None)
    post = func.HttpRequest("POST", "/api/search", params={}, headers={}, body=json.dumps({"query": "what does a mentor do in week 4"}).encode())
    assert fa.search_post._function._func(post).status_code == 200
    assert seen["concept_weights"] == {"week_4": 1.2, "career_mentor": 1.0} and "backend" not in seen
    get = func.HttpRequest("GET", "/api/search", params={"q": "week 4", "backend": "azure"}, headers={}, body=b"")
    assert fa.search_get._function._func(get).status_code == 200 and "backend" not in seen       # an old caller's parameter is ignored


# ---- explaining a ranking

def test_the_explanation_matches_the_search_and_says_where_the_expected_page_stands(conf):
    q, docs = week4_corpus()
    index = index_of(docs)
    kw = dict(candidates=50, limit=5, qvec=li.quantize_int8(q), text_candidates=1000)
    plain = index.explain(li.query_terms(QUESTION), li.binary_code(q), DOMAINS, FILTER, ["sujeeth.org/p1"], **kw)
    hits, matching = index.search(li.query_terms(QUESTION), li.binary_code(q), DOMAINS, FILTER, **kw)
    assert [a["chunk"] for a in plain["answer"]] == ids(index, hits) and plain["passing_filters"] == matching == 7
    best = plain["expected"]["best"][0]
    assert plain["expected"]["chunks_in_index"] == 1 and plain["expected"]["chunks_passing_filters"] == 1
    assert best["chunk"] == "page1-0" and best["position"] > 3 and best["concept_share"] == 0.0
    assert best["keyword_rank_all"] and best["vector_rank_all"] == 7          # the farthest vector of the seven
    boosted = index.explain(li.query_terms(QUESTION), li.binary_code(q), DOMAINS, FILTER, ["sujeeth.org/p1"],
                            concept_weights=OWN, concept_boost=1.0, **kw)
    top = boosted["answer"][0]
    assert top["chunk"] == "page1-0" and top["position"] == 1 and top["concept_share"] == pytest.approx(1.2 / 2.3, abs=0.001)
    assert boosted["expected"]["best"][0]["position"] == 1 and top["tags"] == ["week_4", "circle"]


def test_the_explanation_says_when_the_filters_leave_the_page_out_or_it_is_not_indexed(conf):
    q, docs = week4_corpus()
    docs.append(make_doc(50, 0, title="Week 4 guide", content="week 4 mentor", domain="other.org", concepts=("unrelated",), vector=q))
    index = index_of(docs)
    out = index.explain(li.query_terms(QUESTION), li.binary_code(q), DOMAINS, FILTER, ["other.org/p50", "nowhere.example"],
                        qvec=li.quantize_int8(q))
    x = out["expected"]
    assert x["chunks_in_index"] == 1 and x["chunks_passing_filters"] == 0 and x["best"] == []
    assert "domain is not one the ontology chose" in x["filtered_out"][0]["why"] and "none of the question's concepts" in x["filtered_out"][0]["why"]
    missing = index.explain(li.query_terms(QUESTION), li.binary_code(q), DOMAINS, FILTER, ["nowhere.example"], qvec=li.quantize_int8(q))
    assert missing["expected"]["chunks_in_index"] == 0 and missing["answer"]


def test_a_chunk_that_missed_the_ranking_still_shows_how_far_off_it_was(conf):
    q = np.ones(DIMS)
    docs = [make_doc(777, 0, content="zebra", concepts=("week_4",), vector=-q)]
    docs += [make_doc(i, 0, content="apple banana", concepts=("networking",), vector=q) for i in range(2, 60)]
    index = index_of(docs)
    out = index.explain(li.query_terms("apple"), li.binary_code(q), ["sujeeth.org"], FILTER, ["sujeeth.org/p777"],
                        candidates=50, qvec=li.quantize_int8(q))
    best = out["expected"]["best"][0]
    assert best["position"] is None and best["keyword_rank"] is None and best["keyword_rank_all"] is None
    assert best["vector_rank"] is None and best["vector_rank_all"] == 59                 # last of the 59 that pass the filters


def test_the_service_explains_with_the_settings_a_real_search_uses(conf):
    q, docs = week4_corpus()
    svc = make_service(store=filled_store(docs), embedder=VectorEmbedder(q))
    out = svc.explain_local(QUESTION, FILTER, DOMAINS, ["sujeeth.org/p1"], concept_weights=OWN)
    answer = svc.search(QUESTION, concepts=FILTER, domains=DOMAINS, limit=5, concept_weights=OWN)
    assert [a["chunk"] for a in out["answer"]] == [r["chunk_id"] for r in answer["results"]]
    assert out["question_concepts"] == {"career_mentor": 1.1, "week_4": 1.2} and out["settings"]["concept_boost"] == 1.0
    assert out["settings"]["rescoring"] is True and out["expected"]["best"][0]["position"] == 1 and out["note"] is None


def test_the_explain_route_resolves_the_question_and_reports_the_matches(conf, monkeypatch):
    q, docs = week4_corpus()
    svc = make_service(store=filled_store(docs), embedder=VectorEmbedder(q))
    monkeypatch.setattr(fa, "searcher", svc)
    resolution = dict(RESOLUTION, matched_concepts=FILTER, domains=DOMAINS)
    monkeypatch.setattr(fa.concept_resolver, "resolve_query", lambda **kw: resolution)
    call = lambda body: fa.search_explain._function._func(func.HttpRequest("POST", "/api/search/explain", params={}, headers={}, body=json.dumps(body).encode()))
    assert call({}).status_code == 400
    body = json.loads(call({"query": QUESTION, "expect": "sujeeth.org/p1"}).get_body())
    assert body["expected"]["chunks_in_index"] == 1 and body["answer"][0]["chunk"] == "page1-0"
    assert {"concept": "networking", "source": "relationship", "confidence": None} in body["concept_matches"]
    monkeypatch.setattr(fa.concept_resolver, "resolve_query", lambda **kw: {"matched_concepts": [], "domains": []})
    assert "matched no concepts" in json.loads(call({"query": "zzz"}).get_body())["status"]


# ---- words side by side

WORDS_Q = "how many students completed career circles"


def phrase_corpus():
    """Two chunks with the same words. One says 'students completed career circles'; the other scatters them."""
    q = np.ones(DIMS)
    filler = [make_doc(i, 0, content=f"many students here how career there completed topic{i} circles", vector=q) for i in range(10, 16)]
    exact = make_doc(1, 0, title="Annual report", content="this year 114 students completed career circles and how many more", vector=q)
    loose = make_doc(2, 0, title="Whitepaper", content="how many career paths students choose circles of friends completed forms", vector=q)
    return q, [loose, exact] + filler


def test_a_pair_weighs_what_its_commoner_word_weighs_and_unknown_words_drop_out(conf):
    q, docs = phrase_corpus()
    index = index_of(docs)
    pairs = dict(index.question_phrases(WORDS_Q + " zebra"))
    assert set(pairs) == {"many students", "students completed", "completed career", "career circles"}   # no "how many", no pair with 'zebra'
    rare = index_of(docs + [make_doc(99, 0, content="unique rareword students")])
    weights = dict(rare.question_phrases("rareword students many"))
    assert weights["rareword students"] == pytest.approx(rare.term_idf("students")[0])          # the commoner of the two
    assert rare.term_idf("rareword")[0] > rare.term_idf("students")[0] and rare.term_idf("nosuchword") == (pytest.approx(rare.term_idf("nosuchword")[0]), 0)
    assert index.question_phrases("students students") == [] and index.question_phrases("") == []


def test_the_share_counts_pairs_found_side_by_side_in_the_text_or_the_title(conf):
    index = index_of([make_doc(1, 0, title="Thank-you email templates", content="write a good note after the interview", vector=np.ones(DIMS)),
                      make_doc(2, 0, title="Other", content="email thank someone you often note good", vector=np.ones(DIMS))])
    pairs = [("thank you", 1.0), ("you email", 1.0), ("good note", 2.0)]
    assert index.phrase_share(0, pairs) == pytest.approx(1.0)          # 'thank-you' counts as 'thank you'; the title counts
    assert index.phrase_share(1, pairs) == 0.0                           # the same words, never side by side
    assert index.phrase_share(0, []) == 0.0


def test_the_chunk_that_says_it_in_the_questions_words_ranks_first(conf):
    q, docs = phrase_corpus()
    index = index_of(docs)
    args = (li.query_terms(WORDS_Q), li.binary_code(q), ["sujeeth.org"], ["circle"])
    kw = dict(candidates=50, limit=3, qvec=li.quantize_int8(q), text_candidates=1000)
    plain, _ = index.search(*args, **kw)
    boosted, _ = index.search(*args, question=WORDS_Q, phrase_boost=1.0, **kw)
    assert ids(index, boosted)[0] == "page1-0" and dict(boosted)[index.ids.index("page1-0")] > dict(plain).get(index.ids.index("page1-0"), 0)
    assert index.search(*args, question=WORDS_Q, phrase_boost=0.0, **kw)[0] == plain


def test_only_the_best_candidates_are_checked_and_no_text_in_memory_means_no_bonus(conf):
    q, docs = phrase_corpus()
    index = index_of(docs)
    args = (li.query_terms(WORDS_Q), li.binary_code(q), ["sujeeth.org"], ["circle"])
    kw = dict(candidates=50, qvec=li.quantize_int8(q), text_candidates=1000, question=WORDS_Q, phrase_boost=1.0)
    _, ordered, parts = index._rank(*args[:2], args[2], args[3], 50, kw["qvec"], 1000, None, 0.0, WORDS_Q, 1.0, 1)
    assert len(parts["phrase"]) <= 1                                    # a pool of one: only the top candidate is looked at
    without_text = li.LocalIndex([li.row_from_entity(li.entity_from_search_doc(d)) for d in docs], keep_text=False)
    plain, _ = without_text.search(*args, candidates=50, limit=3, qvec=kw["qvec"], text_candidates=1000)
    assert without_text.search(*args, limit=3, **kw)[0] == plain


def test_one_request_can_try_other_ranking_settings(conf, monkeypatch):
    q, docs = phrase_corpus()
    svc = make_service(store=filled_store(docs), embedder=VectorEmbedder(q))
    normal = svc.search(WORDS_Q, concepts=["circle"], domains=["sujeeth.org"], limit=3)
    assert normal["results"][0]["chunk_id"] == "page1-0" and normal["ranking"]["phrase_boost"] == 1.0
    off = svc.search(WORDS_Q, concepts=["circle"], domains=["sujeeth.org"], limit=3, ranking={"phrase_boost": 0, "max_per_page": "1", "junk": 5})
    assert off["ranking"] == {"question_concepts": {}, "concept_boost": 1.0, "phrase_boost": 0.0, "max_per_page": 1, "unique_pages": True}
    bad = svc.search(WORDS_Q, concepts=["circle"], domains=["sujeeth.org"], limit=3, ranking={"phrase_boost": "lots", "concept_boost": -3})
    assert bad["ranking"]["phrase_boost"] == 1.0 and bad["ranking"]["concept_boost"] == 0.0     # not a number: kept; below zero: zero
    assert ss.SearchService.ranking_settings({"phrase_boost": 9, "unknown": 1})["phrase_boost"] == 9.0


def test_tried_settings_are_kept_apart_from_normal_answers(conf, monkeypatch):
    monkeypatch.setattr(settings, "search_cache_seconds", 60)
    monkeypatch.setattr(ss, "_query_cache", {})
    q, docs = phrase_corpus()
    svc = make_service(store=filled_store(docs), embedder=VectorEmbedder(q))
    a = svc.search(WORDS_Q, concepts=["circle"], domains=["sujeeth.org"], limit=3)
    b = svc.search(WORDS_Q, concepts=["circle"], domains=["sujeeth.org"], limit=3, ranking={"phrase_boost": 0})
    assert a["ranking"]["phrase_boost"] == 1.0 and b["ranking"]["phrase_boost"] == 0.0


def test_the_explanation_lists_the_word_pairs_and_each_chunks_share(conf):
    q, docs = phrase_corpus()
    svc = make_service(store=filled_store(docs), embedder=VectorEmbedder(q))
    out = svc.explain_local(WORDS_Q, ["circle"], ["sujeeth.org"], ["sujeeth.org/p1"])
    assert out["phrases"][0]["weight"] >= out["phrases"][-1]["weight"] and {p["phrase"] for p in out["phrases"]} >= {"students completed", "career circles"}
    assert out["answer"][0]["chunk"] == "page1-0" and out["answer"][0]["phrase_share"] > 0.7 and out["answer"][0]["phrase_counted"] is True
    assert out["settings"]["phrase_boost"] == 1.0 and out["settings"]["text_in_memory"] is True
    off = svc.explain_local(WORDS_Q, ["circle"], ["sujeeth.org"], ["sujeeth.org/p1"], ranking={"phrase_boost": 0})
    assert off["settings"]["phrase_boost"] == 0.0 and off["answer"][0]["phrase_counted"] is False


def test_the_routes_pass_tried_settings_through(conf, monkeypatch):
    seen = {}

    class Searcher:
        def search(self, query, **kw):
            seen.update(kw)
            return {"query": query, "count": 0, "results": []}

        def explain_local(self, query, concepts, domains, expect, **kw):
            seen["explain"] = kw
            return {"answer": []}
    monkeypatch.setattr(fa, "searcher", Searcher())
    monkeypatch.setattr(fa.concept_resolver, "resolve_query", lambda **kw: dict(RESOLUTION))
    monkeypatch.setattr(fa, "add_best_bets", lambda *a, **k: None)
    monkeypatch.setattr(fa, "add_spotlights", lambda *a, **k: None)
    post = lambda route, body: func.HttpRequest("POST", f"/api/{route}", params={}, headers={}, body=json.dumps(body).encode())
    fa.search_post._function._func(post("search", {"query": "week 4", "ranking": {"phrase_boost": 0}}))
    assert seen["ranking"] == {"phrase_boost": 0}
    fa.search_post._function._func(post("search", {"query": "week 4", "ranking": "nonsense"}))
    assert seen["ranking"] is None
    fa.search_explain._function._func(post("search/explain", {"query": "week 4", "ranking": {"concept_boost": 2}}))
    assert seen["explain"]["ranking"] == {"concept_boost": 2}


def test_question_words_never_start_or_end_a_phrase(conf):
    docs = [make_doc(1, 0, content="how do i reach out to a professional about what does a mentor do in week 4", vector=np.ones(DIMS)),
            make_doc(2, 0, content="other words entirely", vector=np.ones(DIMS))]
    index = index_of(docs)
    assert [p for p, _ in index.question_phrases("How do I reach out to a professional?")] == ["reach out", "out to a professional"]
    assert [p for p, _ in index.question_phrases("What does a mentor do in week 4?")] == ["mentor do in week", "week 4"]
    assert index.question_phrases("how do i") == [] and index.question_phrases("what does it do") == []


def test_a_faq_style_chunk_gets_nothing_for_how_do_i(conf):
    q = np.ones(DIMS)
    docs = [make_doc(1, 0, content="how do i ask for a raise and how do i find a mentor", vector=q),
            make_doc(2, 0, content="steps to become a mentor at the foundation", vector=q)]
    index = index_of(docs)
    phrases = index.question_phrases("How do I become a mentor?")
    assert [p for p, _ in phrases] == ["become a mentor"]
    assert index.phrase_share(0, phrases) == 0.0 and index.phrase_share(1, phrases) == 1.0


def test_content_words_too_far_apart_in_the_question_are_not_a_phrase(conf):
    index = index_of([make_doc(1, 0, content="alpha one two three four five beta gamma", vector=np.ones(DIMS))])
    assert [p for p, _ in index.question_phrases("alpha is it not of the on beta gamma")] == ["beta gamma"]


def test_week_4_as_a_phrase_now_lifts_the_foundations_chunk_on_its_own(conf, monkeypatch):
    monkeypatch.setattr(settings, "local_concept_boost", 0.0)         # no concept bonus: the phrase alone
    q, docs = week4_corpus()
    svc = make_service(store=filled_store(docs), embedder=VectorEmbedder(q))
    with_phrase = svc.search(QUESTION, concepts=FILTER, domains=DOMAINS, limit=5)
    without = svc.search(QUESTION, concepts=FILTER, domains=DOMAINS, limit=5, ranking={"phrase_boost": 0})
    assert with_phrase["results"][0]["chunk_id"] == "page1-0" and without["results"][0]["chunk_id"] != "page1-0"
