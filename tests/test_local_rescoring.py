"""Rescoring: the 1-bit codes pick a shortlist, 8-bit vectors rank it. And page-level shadow comparison."""
import json
import time

import azure.functions as func
import numpy as np

import function_app as fa
from services import local_index as li
from services.config import settings
from test_local_index import conf, filled_store, index_of, make_doc, make_service  # noqa: F401  (conf is a fixture)

NO_TERMS = np.zeros(0, dtype=np.uint64)


def unit(v):
    v = np.asarray(v, dtype=np.float64)
    return v / np.linalg.norm(v)


def trap_vectors(dims=64):
    """A question q, a chunk A that is really close to it, and a chunk B whose signs match q better but whose size does
    not: B differs from q in 4 signs (large values), A in 6 signs (tiny values). Hamming distance prefers B, cosine A."""
    q = np.ones(dims)
    a = np.ones(dims)
    a[:6] = -0.01
    b = np.full(dims, 0.01)
    b[:4] = -5.0
    return q, a, b


def trap_index(**kw):
    q, a, b = trap_vectors()
    docs = [make_doc(1, 0, vector=a, content="alpha"), make_doc(2, 0, vector=b, content="beta")]
    return q, index_of(docs, **kw)


def ids(index, hits):
    return [index.ids[row] for row, _ in hits]


def test_quantizing_to_8_bits_keeps_the_direction_and_ignores_scale():
    rng = np.random.default_rng(1)
    v = rng.standard_normal(1536)
    q = np.frombuffer(li.quantize_int8(v), dtype=np.int8).astype(np.float64)
    assert q.shape == (1536,) and abs(q).max() == 127
    assert float(unit(q) @ unit(v)) > 0.9995
    assert li.quantize_int8(v) == li.quantize_int8(v * 37.5)
    assert li.quantize_int8(np.zeros(8)) == bytes(8)


def test_rows_carry_the_8_bit_vector_and_read_it_back(conf):
    doc = make_doc(1, 0)
    entity = li.entity_from_search_doc(doc)
    assert len(entity["v8"]) == len(doc["contentVector"]) and len(entity["vec"]) == len(doc["contentVector"]) // 8
    assert li.row_from_entity(entity)["v8"] == entity["v8"]


def test_the_codes_alone_rank_the_trap_wrongly_and_rescoring_gets_it_right(conf):
    q, index = trap_index()
    qcode, qvec = li.binary_code(q), li.quantize_int8(q)
    by_codes, _ = index.search(NO_TERMS, qcode, ["sujeeth.org"], ["circle"], candidates=2, limit=2)
    assert ids(index, by_codes) == ["page2-0", "page1-0"]                      # Hamming 4 beats 6
    rescored, _ = index.search(NO_TERMS, qcode, ["sujeeth.org"], ["circle"], candidates=2, limit=2, qvec=qvec)
    assert ids(index, rescored) == ["page1-0", "page2-0"]                      # cosine puts the real match first


def test_rescoring_is_all_or_nothing(conf):
    q, a, b = trap_vectors()
    entities = [li.entity_from_search_doc(make_doc(1, 0, vector=a, content="alpha")),
                li.entity_from_search_doc(make_doc(2, 0, vector=b, content="beta"))]
    entities[1]["v8"] = b""                                                    # a row written before vectors were kept
    index = li.LocalIndex(li.row_from_entity(e) for e in entities)
    assert index.vec8 is None and index.rows_without_vectors == 1
    hits, _ = index.search(NO_TERMS, li.binary_code(q), ["sujeeth.org"], ["circle"], candidates=2, limit=2, qvec=li.quantize_int8(q))
    assert ids(index, hits) == ["page2-0", "page1-0"]                          # quietly the codes alone


def test_rescoring_can_be_turned_off(conf, monkeypatch):
    monkeypatch.setattr(settings, "local_rescore", False)
    q, index = trap_index()
    assert index.vec8 is None
    hits, _ = index.search(NO_TERMS, li.binary_code(q), ["sujeeth.org"], ["circle"], candidates=2, limit=2, qvec=li.quantize_int8(q))
    assert ids(index, hits) == ["page2-0", "page1-0"]


def test_only_the_shortlist_the_codes_pick_is_rescored(conf, monkeypatch):
    q, index = trap_index()
    qcode, qvec = li.binary_code(q), li.quantize_int8(q)
    monkeypatch.setattr(settings, "local_rescore_pool", 1)
    rows = np.array([0, 1], dtype=np.int64)
    assert index.vector_ranking(rows, qcode, qvec, 1, pool=1) == [1]            # the code shortlist is B alone; A is never seen
    assert index.vector_ranking(rows, qcode, qvec, 1, pool=2) == [0]            # a wider shortlist reaches A, the better match
    assert index.vector_ranking(rows, qcode, qvec, 2, pool=1) == [0, 1]         # the shortlist is never smaller than what is asked for


def clustered(n=2500, dims=256, clusters=60, seed=5):
    rng = np.random.default_rng(seed)
    mean = rng.normal(0, 0.15, dims)
    scale = np.exp(rng.normal(0, 0.5, dims))
    centers = rng.normal(0, 1, (clusters, dims)) * scale
    make = lambda c: unit(centers[c] * 0.8 + rng.normal(0, 1, dims) * scale * 0.6 + mean)
    vectors = np.array([make(rng.integers(0, clusters)) for _ in range(n)])
    queries = np.array([make(rng.integers(0, clusters)) for _ in range(120)])
    return vectors, queries


def test_rescoring_agrees_with_exact_cosine_far_more_often_than_the_codes_do(conf):
    vectors, queries = clustered()
    docs = [make_doc(i, 0, vector=v, content=f"chunk {i}") for i, v in enumerate(vectors)]
    index = index_of(docs)
    rows = np.arange(index.n, dtype=np.int64)
    agree_codes = agree_rescored = 0
    for q in queries:
        exact = int(np.argmax(vectors @ q))
        qcode, qvec = li.binary_code(q), li.quantize_int8(q)
        agree_codes += index.vector_ranking(rows, qcode, None, 1)[0] == exact
        agree_rescored += index.vector_ranking(rows, qcode, qvec, 1, pool=100)[0] == exact
    assert agree_rescored / len(queries) >= 0.9
    assert agree_rescored - agree_codes >= 0.3 * len(queries)


def test_the_index_counts_the_vectors_in_its_memory(conf):
    docs = [make_doc(i, 0) for i in range(10)]
    with_vectors = index_of(docs)
    rows = [li.row_from_entity(li.entity_from_search_doc(d)) for d in docs]
    for r in rows:
        r["v8"] = b""
    without = li.LocalIndex(rows)
    assert with_vectors.memory_bytes() - without.memory_bytes() == 10 * 64 + 10 * 4


class VectorEmbedder:
    """Like EmbeddingService.embed_query_vectors, with one chosen vector for every question."""
    def __init__(self, vector):
        self.vector = vector
        self.calls = []

    def embed_query_vectors(self, text):
        self.calls.append(text)
        return li.binary_code(self.vector), li.quantize_int8(self.vector)


def test_a_local_search_rescores_when_the_index_and_the_question_have_vectors(conf):
    q, a, b = trap_vectors()
    docs = [make_doc(1, 0, vector=a, content="alpha"), make_doc(2, 0, vector=b, content="beta")]
    svc = make_service(store=filled_store(docs), embedder=VectorEmbedder(q))
    answer = svc.search("zzzz", concepts=["circle"], domains=["sujeeth.org"], limit=2)
    assert [r["chunk_id"] for r in answer["results"]] == ["page1-0", "page2-0"]
    d = svc.local.describe()
    assert d["rescoring"]["active"] is True and d["rescoring"]["rescored_searches"] == 1 and d["rescoring"]["rows_without_vectors"] == 0


def test_status_says_rescoring_is_inactive_when_rows_lack_vectors(conf):
    docs = [make_doc(i, 0) for i in range(4)]
    store = filled_store(docs)
    for key, row in store.chunks.rows.items():
        row.pop("v8", None)
    svc = make_service(store=store)
    svc.search("alpha", concepts=["circle"], domains=["sujeeth.org"], limit=2)
    r = svc.local.describe()["rescoring"]
    assert r["enabled"] is True and r["active"] is False and r["rows_without_vectors"] == 4 and r["rescored_searches"] == 0


# ---- shadow comparison at page level

def wait_compared(svc, count, seconds=5.0):
    end = time.time() + seconds
    while time.time() < end:
        if svc.local.describe()["shadow"]["compared"] >= count:
            return
        time.sleep(0.02)
    raise AssertionError("the shadow comparison did not finish")


def azure_answer(*pairs):
    return {"results": [{"chunk_id": c, "url": u, "title": f"Title {u[-2:]}", "score": 1.0 / (i + 1)} for i, (c, u) in enumerate(pairs)]}


def test_a_different_chunk_of_the_same_page_agrees_at_page_level_and_leaves_no_example(conf, monkeypatch):
    monkeypatch.setattr(settings, "local_shadow_sample", 1.0)
    svc = make_service()
    local = svc._search_local("mentor", ["circle"], ["sujeeth.org"], 5)
    top_url = local["results"][0]["url"]
    svc._maybe_shadow("mentor", ["circle"], ["sujeeth.org"], 5, azure_answer(("other-9", top_url)))
    wait_compared(svc, 1)
    s = svc.local.describe()["shadow"]
    assert s["top_result_agrees"] == 0.0 and s["top_page_agrees"] == 1.0 and s["average_page_overlap"] == 1.0
    assert svc.local.shadow_diffs() == []


def test_a_different_top_page_is_kept_as_an_example_with_both_answers(conf, monkeypatch):
    monkeypatch.setattr(settings, "local_shadow_sample", 1.0)
    svc = make_service()
    svc._maybe_shadow("mentor", ["circle"], ["sujeeth.org"], 5, azure_answer(("x-1", "https://elsewhere.org/a1"), ("x-2", "https://elsewhere.org/a2")))
    wait_compared(svc, 1)
    s = svc.local.describe()["shadow"]
    assert s["top_page_agrees"] == 0.0 and s["average_page_overlap"] == 0.0
    (diff,) = svc.local.shadow_diffs()
    assert diff["query"] == "mentor" and [r["rank"] for r in diff["azure"]] == [1, 2]
    assert diff["azure"][0]["url"] == "https://elsewhere.org/a1" and diff["local"] and diff["local"][0]["url"].startswith("https://sujeeth.org/")


def test_only_the_latest_examples_are_kept_newest_first(conf):
    manager = li.LocalIndexManager(store=filled_store())
    for n in range(li.SHADOW_DIFFS_KEPT + 7):
        manager.record_shadow(0.0, False, page_overlap=0.0, top1_page_same=False, example={"query": f"q{n}"})
    diffs = manager.shadow_diffs(100)
    assert len(diffs) == li.SHADOW_DIFFS_KEPT and diffs[0]["query"] == f"q{li.SHADOW_DIFFS_KEPT + 6}"
    assert [d["query"] for d in manager.shadow_diffs(2)] == [f"q{li.SHADOW_DIFFS_KEPT + 6}", f"q{li.SHADOW_DIFFS_KEPT + 5}"]


def test_old_style_shadow_records_still_count(conf):
    manager = li.LocalIndexManager(store=filled_store())
    manager.record_shadow(0.5, True)
    s = manager.describe()["shadow"]
    assert s["average_overlap"] == 0.5 and s["average_page_overlap"] == 0.5 and s["top_page_agrees"] == 1.0


def test_the_shadow_diffs_route(conf, monkeypatch):
    svc = make_service()
    svc.local.record_shadow(0.0, False, page_overlap=0.0, top1_page_same=False, example={"query": "how do i volunteer", "azure": [], "local": []})
    monkeypatch.setattr(fa, "searcher", svc)
    req = func.HttpRequest("GET", "/api/local-index/shadow-diffs", params={"limit": "5"}, headers={}, body=b"")
    body = json.loads(fa.local_index_shadow_diffs._function._func(req).get_body())
    assert body["count"] == 1 and body["diffs"][0]["query"] == "how do i volunteer" and body["shadow"]["compared"] == 1
    bad = func.HttpRequest("GET", "/api/local-index/shadow-diffs", params={"limit": "x"}, headers={}, body=b"")
    assert json.loads(fa.local_index_shadow_diffs._function._func(bad).get_body())["count"] == 1


def test_the_full_table_read_asks_for_the_vector_column_only_when_rescoring_is_on(conf, monkeypatch):
    seen = []

    class Spy:
        def list_entities(self, select=None, results_per_page=None):
            seen.append(list(select))
            return iter(())
    store = li.LocalIndexStore(chunks=Spy(), meta=object())
    list(store.scan())
    monkeypatch.setattr(settings, "local_rescore", False)
    list(store.scan())
    assert "v8" in seen[0] and "v8" not in seen[1]


def test_a_table_round_trip_keeps_the_vectors_so_a_loaded_index_rescores(conf):
    q, a, b = trap_vectors()
    store = filled_store([make_doc(1, 0, vector=a, content="alpha"), make_doc(2, 0, vector=b, content="beta")])
    index = li.LocalIndexManager(store=store).current()
    assert index.vec8 is not None and index.vec_dims == 64


def test_running_the_local_backfill_writes_the_8_bit_vectors_so_the_index_can_rescore(conf, monkeypatch):
    from test_local_index import backfill_service
    svc, store, names = backfill_service()
    monkeypatch.setattr(settings, "embedding_configured", True, raising=False)
    # rows written before vectors were kept: take them out, then run the backfill again
    svc.local_backfill(names)
    for row in store.chunks.rows.values():
        row.pop("v8", None)
    assert li.LocalIndexManager(store=store).current().vec8 is None
    svc.local_backfill(names)
    index = li.LocalIndexManager(store=store).current()
    assert index.vec8 is not None and index.rows_without_vectors == 0 and index.vec8.shape == (index.n, 64)


def test_a_question_made_of_common_words_still_finds_chunks_unless_they_are_turned_off(conf, monkeypatch):
    docs = [make_doc(1, 0, content="what is the plan for the week", title="Week plan"),
            make_doc(2, 0, content="mentor guide and resume tips", title="Guide")]
    index = index_of(docs)
    hits, matching = index.search(li.query_terms("what is the"), None, ["sujeeth.org"], ["circle"], candidates=5, limit=5)
    assert [index.ids[r] for r, _ in hits] == ["page1-0"]                      # indexed words match, like Azure's standard analyzer
    monkeypatch.setattr(settings, "local_index_stopwords", "english")
    hits, _ = index.search(li.query_terms("what is the"), None, ["sujeeth.org"], ["circle"], candidates=5, limit=5)
    assert [index.ids[r] for r, _ in hits] == ["page1-0"]                      # "what" is not on the list, so it still matches
    hits, _ = index.search(li.query_terms("is the"), None, ["sujeeth.org"], ["circle"], candidates=5, limit=5)
    assert hits == []                                                          # a question of only listed words searches for nothing
