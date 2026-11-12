"""Every result is a different page: one chunk per page, the www. and bare copies of an address as one page."""
import json

import azure.functions as func
import numpy as np
import pytest

import function_app as fa
from services import local_index as li
from services import search_service as ss
from services.best_bets import merge_best_bets, unique_pages
from services.config import settings
from test_local_index import AzureSession, azure_configured, conf, filled_store, index_of, make_doc, make_service  # noqa: F401
from test_local_rescoring import VectorEmbedder

DIMS = 64
DOMAINS = ["sujeeth.org", "www.sujeeth.org"]


def page_of(url):
    return li.page_key(url)


def crowded():
    """One page with four chunks that match best, then five pages with one chunk each (page 1 is also crawled as www.)."""
    q = np.ones(DIMS)
    docs = [make_doc(1, n, title="Career Circle", content="alpha beta gamma " * (6 - n), vector=q) for n in range(4)]
    for i in range(2, 7):
        docs.append(make_doc(i, 0, title=f"Other {i}", content="alpha beta", vector=q * (1.0 - 0.1 * i) + 0.05))
    return q, docs


def test_a_page_appears_once_as_its_best_chunk(conf):
    q, docs = crowded()
    index = index_of(docs)
    args = (li.query_terms("alpha beta gamma"), li.binary_code(q), DOMAINS, ["circle"])
    kw = dict(limit=5, qvec=li.quantize_int8(q))
    crowd, _ = index.search(*args, **kw)
    assert [index.ids[r].split("-")[0] for r, _ in crowd].count("page1") >= 3            # the problem: one page fills the answer
    unique, matching = index.search(*args, unique_pages=True, **kw)
    got = [index.ids[r] for r, _ in unique]
    assert [g.split("-")[0] for g in got] == ["page1", "page2", "page3", "page4", "page5"] and matching == 9
    assert got[0] == "page1-0"                                                          # the page's best chunk, not any chunk
    assert len({page_of(index.urls[r]) for r, _ in unique}) == 5


def test_the_www_copy_of_a_page_is_the_same_page(conf):
    q = np.ones(DIMS)
    plain = make_doc(1, 0, title="Circle", content="alpha beta", domain="sujeeth.org", vector=q)
    www = dict(plain, id="copy1-0", url=plain["url"].replace("https://sujeeth.org", "https://www.sujeeth.org"), domain="www.sujeeth.org")
    other = make_doc(2, 0, title="Other", content="alpha", domain="sujeeth.org", vector=q * 0.9)
    index = index_of([plain, www, other])
    hits, _ = index.search(li.query_terms("alpha beta"), li.binary_code(q), DOMAINS, ["circle"], limit=5, qvec=li.quantize_int8(q), unique_pages=True)
    assert len(hits) == 2 and len({page_of(index.urls[r]) for r, _ in hits}) == 2


def test_fewer_matching_pages_than_the_limit_gives_a_shorter_answer_never_a_repeat(conf):
    q = np.ones(DIMS)
    docs = [make_doc(1, n, content="alpha beta gamma", vector=q) for n in range(4)] + [make_doc(2, 0, content="alpha", vector=q)]
    index = index_of(docs)
    args = (li.query_terms("alpha beta gamma"), li.binary_code(q), DOMAINS, ["circle"])
    kw = dict(limit=5, qvec=li.quantize_int8(q))
    assert len(index.search(*args, max_per_page=1, **kw)[0]) == 5                      # the earlier preference refills with repeats
    strict, _ = index.search(*args, unique_pages=True, **kw)
    assert len(strict) == 2 and len({page_of(index.urls[r]) for r, _ in strict}) == 2


def test_the_search_service_returns_five_different_pages_by_default(conf):
    q, docs = crowded()
    svc = make_service(store=filled_store(docs), embedder=VectorEmbedder(q))
    answer = svc.search("alpha beta gamma", concepts=["circle"], domains=DOMAINS, limit=5)
    urls = [page_of(r["url"]) for r in answer["results"]]
    assert len(urls) == 5 and len(set(urls)) == 5 and answer["ranking"]["unique_pages"] is True and answer["count"] == 5


def test_it_can_be_turned_off_for_the_whole_app_or_one_request(conf, monkeypatch):
    q, docs = crowded()
    svc = make_service(store=filled_store(docs), embedder=VectorEmbedder(q))
    monkeypatch.setattr(settings, "local_max_per_page", 0)
    one = svc.search("alpha beta gamma", concepts=["circle"], domains=DOMAINS, limit=5, ranking={"unique_pages": False})
    assert len({page_of(r["url"]) for r in one["results"]}) < 5 and one["ranking"]["unique_pages"] is False
    monkeypatch.setattr(settings, "search_unique_pages", False)
    off = svc.search("alpha beta gamma", concepts=["circle"], domains=DOMAINS, limit=5)
    assert len({page_of(r["url"]) for r in off["results"]}) < 5 and off["ranking"]["unique_pages"] is False
    assert ss.SearchService.ranking_settings({"unique_pages": "yes"})["unique_pages"] is True
    assert ss.SearchService.ranking_settings({"unique_pages": "off"})["unique_pages"] is False


def test_the_shadow_comparison_and_the_explanation_also_return_each_page_once(conf):
    q, docs = crowded()
    svc = make_service(store=filled_store(docs), embedder=VectorEmbedder(q))
    plain = svc._search_local("alpha beta gamma", ["circle"], DOMAINS, 5, parity=True)
    assert plain["ranking"]["concept_boost"] == 0.0 and len({page_of(r["url"]) for r in plain["results"]}) == 5
    out = svc.explain_local("alpha beta gamma", ["circle"], DOMAINS, ["sujeeth.org/p1"])
    assert len({page_of(a["url"]) for a in out["answer"]}) == len(out["answer"]) == 5 and out["settings"]["unique_pages"] is True


# ---- Azure AI Search returns chunks

def hit(chunk, page, score, domain="sujeeth.org"):
    return {"id": chunk, "title": f"Page {page}", "url": f"https://{domain}/{page}", "domain": domain, "concepts": ["circle"],
            "content": f"text of {chunk}", "@search.score": score}


@pytest.fixture
def azure(conf, monkeypatch):
    azure_configured(monkeypatch)
    monkeypatch.setattr(settings, "search_backend", "azure")
    monkeypatch.setattr(settings, "ai_search_writes", True)
    monkeypatch.setattr(settings, "search_vector_k", 0)
    monkeypatch.setattr(settings, "search_cache_seconds", 0)
    return monkeypatch


def test_azure_is_asked_for_more_chunks_and_the_best_of_each_page_is_kept(azure):
    hits = [hit("a-0", "a", 0.9), hit("a-1", "a", 0.8), hit("a-2", "a", 0.7), hit("b-0", "b", 0.6), hit("a-3", "a", 0.5),
            hit("c-0", "c", 0.4), hit("d-0", "d", 0.3), hit("d-1", "d", 0.2), hit("e-0", "e", 0.1), hit("f-0", "f", 0.05)]
    session = AzureSession(hits=hits)
    svc = make_service(session=session)
    answer = svc.search("alpha", concepts=["circle"], domains=["sujeeth.org"], limit=5)
    body = session.posts[-1][1]
    assert body["top"] == 30 and body["vectorQueries"][0]["k"] == 50                   # 5 results asked for as 30 chunks
    assert [r["chunk_id"] for r in answer["results"]] == ["a-0", "b-0", "c-0", "d-0", "e-0"] and answer["count"] == 5
    assert answer["unique_pages"] is True


def test_azures_www_and_bare_copies_of_a_page_are_one_page_and_fewer_pages_mean_fewer_results(azure):
    hits = [hit("a-0", "circle", 0.9, "www.sujeeth.org"), hit("a-1", "circle/", 0.8, "sujeeth.org"), hit("b-0", "other", 0.7)]
    session = AzureSession(hits=hits)
    svc = make_service(session=session)
    answer = svc.search("circle", concepts=["circle"], domains=["sujeeth.org", "www.sujeeth.org"], limit=5)
    assert [r["chunk_id"] for r in answer["results"]] == ["a-0", "b-0"]


def test_the_overfetch_is_capped_and_never_below_the_limit(azure):
    session = AzureSession(hits=[hit("a-0", "a", 0.9)])
    svc = make_service(session=session)
    svc.search("alpha", concepts=["circle"], domains=["sujeeth.org"], limit=10)
    assert session.posts[-1][1]["top"] == 50                                            # 10 x 6 is capped at 50
    azure.setattr(settings, "search_unique_overfetch", 1)
    svc.search("alpha again", concepts=["circle"], domains=["sujeeth.org"], limit=5)
    assert session.posts[-1][1]["top"] == 5


def test_text_is_read_only_for_the_chunks_that_are_kept(azure):
    azure.setattr(settings, "search_blob_backed", True)
    hits = [hit("a-0", "a", 0.9), hit("a-1", "a", 0.8), hit("b-0", "b", 0.7), hit("b-1", "b", 0.6), hit("c-0", "c", 0.5)]
    for h in hits:
        h.pop("content")
    asked = []
    svc = make_service(session=AzureSession(hits=hits))
    svc._blob_content = lambda ids: asked.append(list(ids)) or {i: f"text {i}" for i in ids}
    answer = svc.search("alpha", concepts=["circle"], domains=["sujeeth.org"], limit=3)
    assert asked == [["a-0", "b-0", "c-0"]] and [r["snippet"] for r in answer["results"]] == ["text a-0", "text b-0", "text c-0"]


def test_azure_asks_for_exactly_the_limit_when_unique_pages_is_off(azure):
    azure.setattr(settings, "search_unique_pages", False)
    session = AzureSession(hits=[hit("a-0", "a", 0.9), hit("a-1", "a", 0.8)])
    svc = make_service(session=session)
    answer = svc.search("alpha", concepts=["circle"], domains=["sujeeth.org"], limit=5)
    assert session.posts[-1][1]["top"] == 5 and len(answer["results"]) == 2 and answer["unique_pages"] is False


# ---- best bets, spotlights and the final answer

def test_a_best_bet_removes_the_same_page_from_the_ordinary_results_even_as_www():
    best = [{"url": "https://www.sujeeth.org/circle", "title": "Circle"}]
    normal = [{"url": "https://sujeeth.org/circle/", "title": "Circle"}, {"url": "https://sujeeth.org/volunteer", "title": "Volunteer"}]
    merged = merge_best_bets(normal, best, 3)
    assert [m["url"] for m in merged] == ["https://www.sujeeth.org/circle", "https://sujeeth.org/volunteer"] and merged[0]["best_bet"] is True


def test_unique_pages_keeps_the_first_of_each_page_and_leaves_items_without_an_address_alone():
    items = [{"url": "https://www.sujeeth.org/a", "n": 1}, {"url": "http://sujeeth.org/a/#top", "n": 2}, {"url": "https://x.org/b", "n": 3},
             {"url": "", "chunk_id": "c-1", "n": 4}, {"url": "", "chunk_id": "c-2", "n": 5}, {"url": "", "chunk_id": "c-1", "n": 6}]
    assert [i["n"] for i in unique_pages(items)] == [1, 3, 4, 5] and unique_pages(None) == []


RESOLUTION = {"matched_concepts": ["circle"], "domains": ["sujeeth.org"], "ontology_version": "1", "concept_matches": []}


def run(monkeypatch, results, unique=True):
    class Searcher:
        def search(self, query, **kw):
            return {"query": query, "count": len(results), "results": [dict(r) for r in results]}
    monkeypatch.setattr(fa, "searcher", Searcher())
    monkeypatch.setattr(fa.concept_resolver, "resolve_query", lambda **kw: dict(RESOLUTION))
    monkeypatch.setattr(settings, "search_unique_pages", unique)

    def spotlight(result, query, resolution, domains):
        result["results"] = [{"url": "https://www.sujeeth.org/spotlight/x", "spotlight": True}] + result["results"]
    monkeypatch.setattr(fa, "add_best_bets", lambda *a, **k: None)
    monkeypatch.setattr(fa, "add_spotlights", spotlight)
    return fa.run_search("week 4")


def test_the_final_answer_has_each_page_once_whatever_was_added_to_it(conf, monkeypatch):
    rows = [{"url": "https://sujeeth.org/spotlight/x/"}, {"url": "https://sujeeth.org/a"}, {"url": "https://www.sujeeth.org/a"}]
    out = run(monkeypatch, rows)
    assert [r["url"] for r in out["results"]] == ["https://www.sujeeth.org/spotlight/x", "https://sujeeth.org/a"] and out["count"] == 2
    assert out["results"][0]["spotlight"] is True                                      # the curated copy is the one that stays
    assert len(run(monkeypatch, rows, unique=False)["results"]) == 4


# ---- the explanation counts pages, like the answer

def test_the_explanation_places_a_page_where_the_answer_does_and_lists_it_once(conf):
    q, docs = crowded()
    svc = make_service(store=filled_store(docs), embedder=VectorEmbedder(q))
    answer = svc.search("alpha beta gamma", concepts=["circle"], domains=DOMAINS, limit=5)
    order = [page_of(r["url"]) for r in answer["results"]]
    for expect, page in (("sujeeth.org/p1", "sujeeth.org/p1"), ("sujeeth.org/p3", "sujeeth.org/p3"), ("sujeeth.org/p5", "sujeeth.org/p5")):
        out = svc.explain_local("alpha beta gamma", ["circle"], DOMAINS, [expect])
        best = out["expected"]["best"]
        assert len(best) == 1 and best[0]["position"] == order.index(page) + 1 == best[0]["page_position"]      # the answer's place, not the chunk's
    one = svc.explain_local("alpha beta gamma", ["circle"], DOMAINS, ["sujeeth.org/p1"])["expected"]
    assert one["chunks_passing_filters"] == 4 and one["pages_passing_filters"] == 1 and one["pages_in_index"] == 1
    assert one["best"][0]["chunk"] == "page1-0" and one["best"][0]["other_chunks"] == 3


def test_a_page_behind_its_better_chunk_has_no_place_of_its_own(conf):
    q, docs = crowded()
    index = index_of(docs)
    out = index.explain(li.query_terms("alpha beta gamma"), li.binary_code(q), DOMAINS, ["circle"], ["sujeeth.org/p1"], limit=5,
                        qvec=li.quantize_int8(q), unique_pages=True, max_chunks=6)
    assert [a["page_position"] for a in out["answer"]] == [1, 2, 3, 4, 5] and [a["position"] for a in out["answer"]] == [1, 2, 3, 4, 5]
    chunk_level = index.explain(li.query_terms("alpha beta gamma"), li.binary_code(q), DOMAINS, ["circle"], ["sujeeth.org/p1"], limit=5,
                                qvec=li.quantize_int8(q), unique_pages=False)
    assert [a["position"] for a in chunk_level["answer"]] == [1, 2, 3, 4, 5]                      # unchanged when not unique
    assert [e["chunk"] for e in chunk_level["expected"]["best"]] == ["page1-0", "page1-1", "page1-2", "page1-3"]
    assert chunk_level["expected"]["best"][0]["other_chunks"] == 0
