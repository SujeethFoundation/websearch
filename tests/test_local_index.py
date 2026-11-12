"""Tests for search inside the app over chunks kept in Azure Table Storage."""
import json
import math
import threading
import time
import types

import numpy as np
import pytest
import requests

from services import embedding_service as es
from services import local_index as li
from services import queue_worker as qw
from services import search_service as ss
from services.config import settings

DIMS = 64


# ---------------------------------------------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------------------------------------------

class FakeTable:
    """Just enough of a TableClient: keyed rows, one-partition transactions of up to 100, queries by partition."""

    def __init__(self, fail_batches=False):
        self.rows = {}
        self.batch_sizes = []
        self.single_writes = 0
        self.fail_batches = fail_batches

    def create_table(self):
        pass

    def submit_transaction(self, operations):
        if self.fail_batches:
            raise RuntimeError("the batch failed")
        assert len(operations) <= 100
        assert len({op[1]["PartitionKey"] for op in operations}) == 1, "a transaction holds one partition"
        for op in operations:
            if op[0] == "delete" and (op[1]["PartitionKey"], op[1]["RowKey"]) not in self.rows:
                raise RuntimeError("a row to delete is not there, so the whole transaction fails")
        for op in operations:
            key = (op[1]["PartitionKey"], op[1]["RowKey"])
            if op[0] == "delete":
                del self.rows[key]
            else:
                self.rows[key] = dict(op[1])
        self.batch_sizes.append(len(operations))

    def upsert_entity(self, entity, mode=None):
        self.single_writes += 1
        self.rows[(entity["PartitionKey"], entity["RowKey"])] = dict(entity)

    def delete_entity(self, partition, row):
        if (partition, row) not in self.rows:
            raise KeyError("not found")
        del self.rows[(partition, row)]

    def get_entity(self, partition, row, select=None):
        entity = self.rows[(partition, row)]
        return {k: v for k, v in entity.items() if not select or k in select or k in ("PartitionKey", "RowKey")}

    def query_entities(self, flt, select=None):
        partition = flt.split("'")[1]
        return [self.get_entity(p, r, select) for (p, r) in sorted(self.rows) if p == partition]

    def list_entities(self, select=None, results_per_page=None):
        for key in sorted(self.rows):
            yield self.get_entity(key[0], key[1], select)


def new_store(chunks=None):
    return li.LocalIndexStore(chunks=chunks or FakeTable(), meta=FakeTable())


WORDS = [f"w{i}" for i in range(400)]


def make_doc(page, number, title="Page title", content=None, domain="sujeeth.org", concepts=("circle",), vector=None, rng=None):
    rng = rng or np.random.default_rng(page * 1000 + number)
    if content is None:
        content = " ".join(rng.choice(WORDS, 60))
    if vector is None:
        vector = rng.standard_normal(DIMS)
    return {"id": f"page{page}-{number}", "title": title, "url": f"https://{domain}/p{page}", "domain": domain,
            "concepts": list(concepts), "content": content, "contentVector": [float(x) for x in vector]}


def corpus(pages=20, per_page=5, seed=3):
    rng = np.random.default_rng(seed)
    domains = ["sujeeth.org", "themuse.com"]
    concepts = [["circle"], ["mentor"], ["circle", "mentor"], ["resume"]]
    docs = []
    for p in range(pages):
        for n in range(per_page):
            docs.append(make_doc(p, n, title=f"Title {p}", domain=domains[p % 2], concepts=concepts[p % 4], rng=rng))
    return docs


def entities(docs):
    return [li.entity_from_search_doc(d) for d in docs]


def index_of(docs, keep_text=True):
    return li.LocalIndex((li.row_from_entity(e) for e in entities(docs)), keep_text=keep_text)


# ---------------------------------------------------------------------------------------------------------------
# Words, hashes and bits
# ---------------------------------------------------------------------------------------------------------------

def test_tokens_lowercase_keep_digits_and_other_scripts_and_drop_stop_words():
    assert li.tokens("The Week 3 agenda, and Résumé-writing: 简历") == ["week", "3", "agenda", "résumé", "writing", "简历"]
    assert li.tokens("") == [] and li.tokens(None) == []


def test_term_hash_is_stable_and_distinct():
    assert li.term_hash("mentor") == li.term_hash("mentor")
    assert li.term_hash("mentor") != li.term_hash("mentors")
    assert 0 <= li.term_hash("mentor") < 2 ** 64


def test_analyze_counts_words_and_weights_the_title():
    tid, tcnt = li.analyze("Mentor guide", "mentor program program program", title_weight=3)
    hashes = np.frombuffer(tid, dtype="<u8")
    counts = dict(zip(hashes.tolist(), np.frombuffer(tcnt, dtype=np.uint8).tolist()))
    assert counts[li.term_hash("mentor")] == 1 + 3
    assert counts[li.term_hash("program")] == 3
    assert counts[li.term_hash("guide")] == 3
    assert list(hashes) == sorted(hashes)


def test_counts_stop_at_255():
    _, tcnt = li.analyze("", "word " * 1000)
    assert list(np.frombuffer(tcnt, dtype=np.uint8)) == [255]


def test_binary_code_is_one_bit_per_dimension():
    code = li.binary_code([1.0, -1.0, 0.0, 2.0, 0.5, -0.1, 3.0, -3.0, 1.0])
    assert len(code) == 2 and code[0] == 0b10011010 and code[1] == 0b10000000
    assert len(li.binary_code(np.ones(1536))) == 192


def test_the_query_terms_are_distinct_hashes():
    terms = li.query_terms("the Mentor mentor program")
    assert terms.dtype == np.uint64 and len(terms) == 2


# ---------------------------------------------------------------------------------------------------------------
# Table rows
# ---------------------------------------------------------------------------------------------------------------

def test_a_row_is_keyed_by_page_and_zero_padded_chunk_number():
    row = li.entity_from_search_doc(make_doc(7, 12))
    assert row["PartitionKey"] == "page7" and row["RowKey"] == "000012" and row["id"] == "page7-12"
    assert row["domain"] == "sujeeth.org" and row["concepts"] == "circle" and len(row["vec"]) == DIMS // 8
    assert len(row["tid"]) == 8 * len(row["tcnt"])


def test_a_page_id_with_dashes_is_split_at_the_last_dash():
    assert li.split_chunk_id("abc-def-7") == ("abc-def", 7)
    for bad in ("nodash", "abc-x", "-3", ""):
        with pytest.raises(ValueError):
            li.split_chunk_id(bad)


def test_row_from_entity_reads_bytes_and_wrapped_values():
    e = li.entity_from_search_doc(make_doc(1, 0, concepts=("a", "b")))
    wrapped = dict(e, vec=types.SimpleNamespace(value=e["vec"]))
    row = li.row_from_entity(wrapped)
    assert row["vec"] == e["vec"] and row["concepts"] == ["a", "b"] and row["id"] == "page1-0"
    assert li.row_from_entity({"PartitionKey": "p", "RowKey": "000004"})["id"] == "p-4"


def test_long_text_is_cut_to_what_a_table_property_can_hold():
    row = li.entity_from_search_doc(make_doc(1, 0, content="x" * 50000))
    assert len(row["text"]) == li.MAX_TEXT_CHARS


# ---------------------------------------------------------------------------------------------------------------
# Ranking against a plain reference
# ---------------------------------------------------------------------------------------------------------------

def reference_keyword(ents, terms):
    rows = [li.row_from_entity(e) for e in ents]
    n = len(rows)
    tfs = [dict(zip(np.frombuffer(r["tid"], "<u8").tolist(), np.frombuffer(r["tcnt"], np.uint8).tolist())) for r in rows]
    dl = [float(sum(tf.values())) for tf in tfs]
    avg = sum(dl) / n
    scores = np.zeros(n, dtype=np.float32)
    for t in terms.tolist():
        df = sum(1 for tf in tfs if t in tf)
        if not df:
            continue
        idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
        for i, tf in enumerate(tfs):
            if t in tf:
                f = tf[t]
                scores[i] += np.float32(idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * dl[i] / avg)))
    return scores


def reference_distance(ents, qcode):
    q = int.from_bytes(qcode, "big")
    return np.array([bin(int.from_bytes(e["vec"], "big") ^ q).count("1") for e in ents])


def test_keyword_scores_match_a_plain_bm25():
    ents = entities(corpus())
    index = li.LocalIndex(li.row_from_entity(e) for e in ents)
    terms = li.query_terms("w3 w17 w250 w999 w44")
    assert np.allclose(index.keyword_scores(terms), reference_keyword(ents, terms), rtol=1e-4, atol=1e-6)


def test_a_word_nobody_has_scores_nothing_and_an_empty_question_scores_nothing():
    index = index_of(corpus())
    assert not index.keyword_scores(li.query_terms("zzzunknownzzz")).any()
    assert not index.keyword_scores(li.query_terms("the and of")).any()


def test_vector_half_returns_the_nearest_chunks_by_hamming_distance():
    docs = corpus()
    ents = entities(docs)
    index = li.LocalIndex(li.row_from_entity(e) for e in ents)
    q = li.binary_code(np.random.default_rng(9).standard_normal(DIMS))
    hits, matching = index.search(np.zeros(0, dtype=np.uint64), q, ["sujeeth.org", "themuse.com"],
                                  ["circle", "mentor", "resume"], candidates=10, limit=10)
    dist = reference_distance(ents, q)
    expected = sorted(range(len(ents)), key=lambda i: (dist[i], i))[:10]
    assert [row for row, _ in hits] == sorted(expected, key=lambda i: (list(expected).index(i)))
    assert matching == len(ents)


def test_fused_order_matches_a_reference_reciprocal_rank_fusion():
    docs = corpus(pages=30)
    ents = entities(docs)
    index = li.LocalIndex(li.row_from_entity(e) for e in ents)
    terms = li.query_terms("w5 w60 w120 w300")
    q = li.binary_code(np.random.default_rng(21).standard_normal(DIMS))
    domains, concepts = ["sujeeth.org", "themuse.com"], ["circle", "mentor", "resume"]
    hits, _ = index.search(terms, q, domains, concepts, candidates=15, limit=8)
    text, dist = reference_keyword(ents, terms), reference_distance(ents, q)
    by_text = [i for i in sorted(range(len(ents)), key=lambda i: (-text[i], i)) if text[i] > 0][:15]
    by_vec = sorted(range(len(ents)), key=lambda i: (dist[i], i))[:15]
    fused = {}
    for rank, i in enumerate(by_text):
        fused[i] = fused.get(i, 0) + 1 / (60 + rank + 1)
    for rank, i in enumerate(by_vec):
        fused[i] = fused.get(i, 0) + 1 / (60 + rank + 1)
    expected = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))[:8]
    assert [row for row, _ in hits] == [row for row, _ in expected]
    assert [round(s, 9) for _, s in hits] == [round(s, 9) for _, s in expected]


def test_ties_are_broken_by_position_so_results_do_not_wobble():
    values = np.array([3, 1, 1, 1, 1, 2, 1], dtype=np.int32)
    assert list(li.LocalIndex._top(values, 3, False)) == [1, 2, 3]
    assert list(li.LocalIndex._top(values, 100, False)) == [1, 2, 3, 4, 6, 5, 0]
    assert list(li.LocalIndex._top(np.zeros(0), 3, True)) == []


# ---------------------------------------------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------------------------------------------

def test_only_chunks_in_the_listed_domains_with_a_listed_concept_are_searched():
    docs = [make_doc(1, 0, domain="sujeeth.org", concepts=("circle",), content="mentor guide"),
            make_doc(2, 0, domain="themuse.com", concepts=("circle",), content="mentor guide"),
            make_doc(3, 0, domain="sujeeth.org", concepts=("resume",), content="mentor guide"),
            make_doc(4, 0, domain="sujeeth.org", concepts=(), content="mentor guide")]
    index = index_of(docs)
    terms = li.query_terms("mentor")
    hits, matching = index.search(terms, None, ["sujeeth.org"], ["circle"])
    assert [index.ids[r] for r, _ in hits] == ["page1-0"] and matching == 1
    hits, matching = index.search(terms, None, ["sujeeth.org", "themuse.com"], ["circle", "resume"])
    assert {index.ids[r] for r, _ in hits} == {"page1-0", "page2-0", "page3-0"} and matching == 3


def test_filters_ignore_case_and_unknown_values_match_nothing():
    index = index_of([make_doc(1, 0, domain="Sujeeth.org", concepts=("Circle",), content="mentor")])
    terms = li.query_terms("mentor")
    assert index.search(terms, None, ["SUJEETH.ORG"], ["circle"])[1] == 1
    assert index.search(terms, None, ["other.org"], ["circle"]) == ([], 0)
    assert index.search(terms, None, ["sujeeth.org"], ["nothing"]) == ([], 0)
    assert index.search(terms, None, [], ["circle"]) == ([], 0)


def test_results_keep_the_original_concept_spelling():
    index = index_of([make_doc(1, 0, concepts=("Career_Development", "mentor"), content="x")])
    assert index.row_dict(0)["concepts"] == ["Career_Development", "mentor"]


def test_more_than_sixty_four_concepts_work():
    docs = [make_doc(i, 0, concepts=(f"c{i}",), content="mentor") for i in range(150)]
    index = index_of(docs)
    assert index.concept_words == 3
    hits, _ = index.search(li.query_terms("mentor"), None, ["sujeeth.org"], ["c130"])
    assert [index.ids[r] for r, _ in hits] == ["page130-0"]


# ---------------------------------------------------------------------------------------------------------------
# Building: bad rows, empty, sizes, pages
# ---------------------------------------------------------------------------------------------------------------

def test_unusable_rows_are_left_out_and_counted():
    good = li.row_from_entity(li.entity_from_search_doc(make_doc(1, 0)))
    no_vec = dict(good, id="page9-0", vec=b"")
    bad_words = dict(good, id="page9-1", tid=b"123")
    mismatched = dict(good, id="page9-2", tcnt=b"")
    wrong_width = dict(good, id="page9-3", vec=b"\x00" * 3)
    index = li.LocalIndex([good, no_vec, bad_words, mismatched, wrong_width])
    assert index.n == 1 and index.skipped == 4 and index.ids == ["page1-0"]


def test_an_empty_index_answers_with_nothing():
    index = li.LocalIndex([])
    assert index.n == 0 and index.search(li.query_terms("mentor"), b"\0" * 8, ["a"], ["b"]) == ([], 0)


def test_memory_estimate_grows_with_the_corpus_and_text_can_be_left_out():
    small, big = index_of(corpus(pages=4)), index_of(corpus(pages=40))
    assert big.memory_bytes() > 5 * small.memory_bytes()
    assert index_of(corpus(pages=40), keep_text=False).memory_bytes() < big.memory_bytes()
    assert index_of(corpus(pages=4), keep_text=False).texts is None


def test_pages_are_ordered_by_chunk_number_not_by_text_order():
    docs = [make_doc(1, 10), make_doc(1, 2), make_doc(1, 0)]
    index = index_of(docs)
    assert [index.ids[i] for i in index.pages["page1"]] == ["page1-0", "page1-2", "page1-10"]
    assert [index.ids[i] for i in index.by_url["https://sujeeth.org/p1"]] == ["page1-0", "page1-2", "page1-10"]


# ---------------------------------------------------------------------------------------------------------------
# The table store
# ---------------------------------------------------------------------------------------------------------------

def test_rows_are_written_in_transactions_of_one_page_and_at_most_one_hundred_rows():
    chunks = FakeTable()
    store = new_store(chunks)
    docs = [make_doc(1, n) for n in range(230)] + [make_doc(2, n) for n in range(3)]
    assert store.upsert_entities(entities(docs)) == 233
    assert sorted(chunks.batch_sizes) == [3, 30, 100, 100] and len(chunks.rows) == 233


def test_a_failed_transaction_falls_back_to_single_writes():
    chunks = FakeTable(fail_batches=True)
    new_store(chunks).upsert_entities(entities([make_doc(1, n) for n in range(5)]))
    assert chunks.single_writes == 5 and len(chunks.rows) == 5


def test_rewriting_a_row_replaces_it():
    chunks = FakeTable()
    store = new_store(chunks)
    store.upsert_entities(entities([make_doc(1, 0, content="first words")]))
    store.upsert_entities(entities([make_doc(1, 0, content="second words")]))
    assert len(chunks.rows) == 1 and chunks.rows[("page1", "000000")]["text"] == "second words"


def test_delete_ignores_rows_that_are_not_there():
    chunks = FakeTable()
    store = new_store(chunks)
    store.upsert_entities(entities([make_doc(1, n) for n in range(4)]))
    store.delete_ids(["page1-1", "page1-9", "page5-0", "garbage"])
    assert sorted(r for _, r in chunks.rows) == ["000000", "000002", "000003"]
    store.delete_ids(["page1-0", "page1-2", "page1-3"])
    assert not chunks.rows


def test_a_pages_chunk_ids_come_from_its_partition():
    store = new_store()
    store.upsert_entities(entities([make_doc(1, n) for n in range(3)] + [make_doc(2, 0)]))
    assert sorted(store.page_ids("page1")) == ["page1-0", "page1-1", "page1-2"]
    assert store.page_ids("nothing") == []


def test_single_chunk_text_can_be_fetched_and_a_missing_one_is_none():
    store = new_store()
    store.upsert_entities(entities([make_doc(1, 3, content="the fourth chunk")]))
    assert store.get_text("page1-3") == "the fourth chunk" and store.get_text("page1-8") is None


def test_the_version_row_changes_every_time_and_is_none_before_any_write():
    store = new_store()
    assert store.get_version() is None
    first = store.bump_version()
    assert store.get_version() == first and store.bump_version() != first


def test_a_scan_can_leave_out_the_text():
    store = new_store()
    store.upsert_entities(entities([make_doc(1, 0)]))
    assert "text" in next(iter(store.scan(True))) and "text" not in next(iter(store.scan(False)))


# ---------------------------------------------------------------------------------------------------------------
# The loader
# ---------------------------------------------------------------------------------------------------------------

@pytest.fixture
def conf(monkeypatch):
    monkeypatch.setattr(settings, "embedding_dimensions", DIMS)
    monkeypatch.setattr(settings, "search_vector_field", "contentVector")
    monkeypatch.setattr(settings, "search_backend", "local")
    monkeypatch.setattr(settings, "local_index_write", True)
    monkeypatch.setattr(settings, "ai_search_writes", False)
    monkeypatch.setattr(settings, "local_index_text_in_memory", True)
    monkeypatch.setattr(settings, "local_index_poll_seconds", 5)
    monkeypatch.setattr(settings, "local_index_min_reload_seconds", 0)
    monkeypatch.setattr(settings, "search_cache_seconds", 0)
    monkeypatch.setattr(ss.SearchService, "configured", property(lambda self: False))


def wait_for(predicate, seconds=5.0):
    end = time.time() + seconds
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def filled_store(docs=None):
    store = new_store()
    store.upsert_entities(entities(docs or corpus()))
    store.bump_version()
    return store


def test_the_first_search_loads_the_table_and_records_how_long_it_took(conf):
    manager = li.LocalIndexManager(store=filled_store())
    index = manager.current()
    assert index.n == 100 and manager.describe()["loaded"] and manager.describe()["loads"] == 1
    assert manager.describe()["chunks"] == 100 and manager.describe()["pages"] == 20


def test_an_empty_table_is_unavailable_not_an_empty_answer(conf):
    manager = li.LocalIndexManager(store=new_store())
    with pytest.raises(li.LocalIndexUnavailable, match="empty"):
        manager.current()


def test_a_failed_first_load_is_not_retried_on_every_request(conf):
    class Broken:
        calls = 0

        def get_version(self):
            Broken.calls += 1
            raise RuntimeError("storage is down")

    manager = li.LocalIndexManager(store=Broken())
    for _ in range(3):
        with pytest.raises(li.LocalIndexUnavailable):
            manager.current()
    assert Broken.calls == 1 and "storage is down" in manager.describe()["last_error"]


def test_a_changed_version_reloads_in_the_background_and_swaps_the_index(conf):
    store = filled_store(corpus(pages=4))
    manager = li.LocalIndexManager(store=store)
    old = manager.current()
    store.upsert_entities(entities(corpus(pages=8)))
    store.bump_version()
    manager._last_check = float("-inf")
    assert manager.current() is old                       # the search that notices does not wait
    assert wait_for(lambda: manager._index is not old) and manager._index.n == 40


def test_an_unchanged_version_does_not_reload(conf):
    manager = li.LocalIndexManager(store=filled_store())
    manager.current()
    manager._last_check = float("-inf")
    manager.current()
    time.sleep(0.2)
    assert manager.describe()["loads"] == 1


def test_reloads_wait_for_the_minimum_interval(conf, monkeypatch):
    monkeypatch.setattr(settings, "local_index_min_reload_seconds", 3600)
    store = filled_store(corpus(pages=4))
    manager = li.LocalIndexManager(store=store)
    manager.current()
    store.upsert_entities(entities(corpus(pages=8)))
    store.bump_version()
    manager._last_check = float("-inf")
    manager.current()
    time.sleep(0.3)
    assert manager.describe()["loads"] == 1


def test_a_failed_refresh_keeps_the_index_it_has(conf):
    store = filled_store()
    manager = li.LocalIndexManager(store=store)
    old = manager.current()
    store.bump_version()
    store.chunks.list_entities = lambda **kw: (_ for _ in ()).throw(RuntimeError("scan failed"))
    manager._last_check = float("-inf")
    manager.current()
    assert wait_for(lambda: manager.describe()["last_error"] is not None)
    assert manager.current() is old


def test_reload_rebuilds_now(conf):
    store = filled_store(corpus(pages=4))
    manager = li.LocalIndexManager(store=store)
    manager.current()
    store.upsert_entities(entities(corpus(pages=6)))
    manager.reload()
    assert manager.current().n == 30


def test_the_text_can_stay_in_the_table(conf, monkeypatch):
    monkeypatch.setattr(settings, "local_index_text_in_memory", False)
    manager = li.LocalIndexManager(store=filled_store())
    assert manager.current().texts is None and manager.describe()["text_in_memory"] is False


# ---------------------------------------------------------------------------------------------------------------
# The search service
# ---------------------------------------------------------------------------------------------------------------

class Embedder:
    def __init__(self, fail=False):
        self.fail, self.calls, self.query_calls = fail, [], []

    def embed(self, texts):
        self.calls.append(len(texts))
        return [list(np.random.default_rng(len(t)).standard_normal(DIMS)) for t in texts]

    def embed_query(self, text):
        self.query_calls.append(text)
        if self.fail:
            raise RuntimeError("the embedding service is down")
        return list(np.random.default_rng(abs(hash(text)) % 1000).standard_normal(DIMS))


class Blobs:
    def __init__(self):
        self.blobs = {}

    def get(self, name):
        return json.loads(json.dumps(self.blobs[name])) if name in self.blobs else None

    def put(self, name, obj):
        self.blobs[name] = json.loads(json.dumps(obj))

    def delete(self, name):
        self.blobs.pop(name, None)


class AzureResp:
    def __init__(self, code=200, body=None):
        self.status_code, self._body, self.headers = code, body or {}, {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)


class AzureSession:
    def __init__(self, hits=None):
        self.hits, self.posts = hits or [], []

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))
        if "/docs/index" in url:
            return AzureResp(200, {"value": [{"key": a["id"], "status": True, "statusCode": 200} for a in json["value"]]})
        return AzureResp(200, {"value": self.hits, "@odata.count": len(self.hits)})


def make_service(conf_unused=None, store=None, embedder=None, session=None, azure=False):
    svc = object.__new__(ss.SearchService)
    svc.session, svc.storage = session or AzureSession(), Blobs()
    svc.embedder = embedder or Embedder()
    svc.local = li.LocalIndexManager(store=store or filled_store())
    return svc


def azure_configured(monkeypatch):
    monkeypatch.setattr(ss.SearchService, "configured", property(lambda self: True))
    monkeypatch.setattr(settings, "search_endpoint", "https://x", raising=False)
    monkeypatch.setattr(settings, "search_index_name", "sf", raising=False)
    monkeypatch.setattr(settings, "search_api_version", "v", raising=False)
    monkeypatch.setattr(settings, "search_blob_backed", False)


ASK = dict(concepts=["circle", "mentor", "resume"], domains=["sujeeth.org", "themuse.com"])


def test_a_local_search_returns_the_same_shape_as_the_azure_search(conf):
    svc = make_service()
    answer = svc.search("w5 w60 w120", limit=5, **ASK)
    assert set(answer) >= {"query", "count", "total_matches", "search_mode", "embedding_enabled", "results"}
    assert answer["count"] == 5 and answer["search_mode"] == "hybrid" and answer["backend"] == "local"
    assert answer["total_matches"] == 100
    assert set(answer["results"][0]) == {"title", "url", "snippet", "domain", "concepts", "score", "source", "chunk_id"}
    first = answer["results"][0]
    assert first["source"] == "website" and 0 < first["score"] < 1 and len(first["snippet"]) <= 700
    assert svc.local.describe()["searches"] == 1


def test_the_search_embeds_the_question_without_the_indexing_throttle(conf):
    embedder = Embedder()
    make_service(embedder=embedder).search("how do I become a mentor", **ASK)
    assert embedder.query_calls == ["how do I become a mentor"] and embedder.calls == []


def test_if_the_question_cannot_be_embedded_it_is_still_answered_by_keywords(conf):
    svc = make_service(embedder=Embedder(fail=True))
    answer = svc.search("w5 w60", **ASK)
    assert answer["search_mode"] == "keyword" and answer["embedding_enabled"] is False and answer["count"] > 0
    assert svc.local.describe()["keyword_only_searches"] == 1


def test_a_search_needs_concepts_and_domains_as_before(conf):
    with pytest.raises(ValueError):
        make_service().search("mentor", concepts=[], domains=["sujeeth.org"])


def test_snippets_come_from_the_table_when_text_is_not_kept_in_memory(conf, monkeypatch):
    monkeypatch.setattr(settings, "local_index_text_in_memory", False)
    answer = make_service().search("w5 w60", **ASK)
    assert answer["results"] and all(r["snippet"] for r in answer["results"])


def test_the_result_cache_still_works_in_front_of_the_local_search(conf, monkeypatch):
    monkeypatch.setattr(settings, "search_cache_seconds", 600)
    monkeypatch.setattr(ss, "_query_cache", {})
    monkeypatch.setattr(ss, "_query_stats", {"hits": 0, "misses": 0, "saved": 0.0})
    embedder = Embedder()
    svc = make_service(embedder=embedder)
    first = svc.search("w5 w60", **ASK)
    second = svc.search("W5   w60?", **ASK)
    assert second["results"] == first["results"] and len(embedder.query_calls) == 1


def test_when_the_local_index_is_empty_the_search_falls_back_to_azure(conf, monkeypatch):
    azure_configured(monkeypatch)
    hit = {"id": "page1-0", "title": "T", "url": "https://sujeeth.org/p1", "domain": "sujeeth.org", "concepts": ["circle"],
           "content": "azure text", "@search.score": 0.5}
    svc = make_service(store=new_store(), session=AzureSession([hit]))
    answer = svc.search("mentor", **ASK)
    assert answer["results"][0]["snippet"] == "azure text" and "backend" not in answer
    assert svc.local.describe()["fallbacks_to_ai_search"] == 1


def test_without_the_fallback_an_unavailable_index_is_an_error(conf, monkeypatch):
    azure_configured(monkeypatch)
    monkeypatch.setattr(settings, "local_search_fallback", False)
    with pytest.raises(RuntimeError, match="not available"):
        make_service(store=new_store()).search("mentor", **ASK)


def test_local_search_works_when_azure_ai_search_is_not_configured_at_all(conf):
    assert make_service().search("w5", **ASK)["count"] > 0


def test_azure_backend_is_unchanged_and_does_not_touch_the_local_index(conf, monkeypatch):
    azure_configured(monkeypatch)
    monkeypatch.setattr(settings, "search_backend", "azure")
    session = AzureSession([{"id": "page1-0", "title": "T", "url": "u", "domain": "d", "concepts": ["c"], "content": "x", "@search.score": 1}])
    svc = make_service(session=session)
    assert svc.search("mentor", **ASK)["count"] == 1
    assert svc.local.describe()["loaded"] is False and len(session.posts) == 1


def test_shadow_mode_answers_from_azure_and_compares_a_sample_locally(conf, monkeypatch):
    azure_configured(monkeypatch)
    monkeypatch.setattr(settings, "local_shadow_sample", 1.0)
    local_answer = make_service().search("w5 w60", **ASK)           # the backend is local here
    monkeypatch.setattr(settings, "search_backend", "shadow")
    ids = [r["chunk_id"] for r in local_answer["results"]]
    hits = [{"id": i, "title": "T", "url": "u", "domain": "sujeeth.org", "concepts": ["circle"], "content": "x", "@search.score": 1} for i in ids]
    svc = make_service(session=AzureSession(hits))
    answer = svc.search("w5 w60", **ASK)
    assert "backend" not in answer                                  # the caller got the Azure answer
    assert wait_for(lambda: svc.local.describe()["shadow"]["compared"] == 1)
    shadow = svc.local.describe()["shadow"]
    assert shadow["average_overlap"] == 1.0 and shadow["top_result_agrees"] == 1.0


def test_shadow_mode_records_disagreement_and_failures(conf, monkeypatch):
    azure_configured(monkeypatch)
    monkeypatch.setattr(settings, "search_backend", "shadow")
    monkeypatch.setattr(settings, "local_shadow_sample", 1.0)
    hits = [{"id": "elsewhere-0", "title": "T", "url": "u", "domain": "d", "concepts": ["c"], "content": "x", "@search.score": 1}]
    svc = make_service(session=AzureSession(hits))
    svc.search("w5 w60", **ASK)
    assert wait_for(lambda: svc.local.describe()["shadow"]["compared"] == 1)
    assert svc.local.describe()["shadow"]["average_overlap"] == 0.0
    broken = make_service(store=new_store(), session=AzureSession(hits))
    broken.search("w5 w60", **ASK)
    assert wait_for(lambda: broken.local.describe()["shadow"]["failed"] == 1)


def test_shadow_sampling_can_be_off(conf, monkeypatch):
    azure_configured(monkeypatch)
    monkeypatch.setattr(settings, "search_backend", "shadow")
    monkeypatch.setattr(settings, "local_shadow_sample", 0.0)
    svc = make_service(session=AzureSession([{"id": "a-0", "title": "T", "url": "u", "domain": "d", "concepts": [], "content": "x", "@search.score": 1}]))
    svc.search("w5", **ASK)
    time.sleep(0.2)
    assert svc.local.describe()["shadow"]["compared"] == 0 and svc.local.describe()["loaded"] is False


# ---- reading pages

def page_service(conf_unused=None):
    docs = [make_doc(1, n, title="A page", content=f"chunk number {n} " + "word " * 40) for n in range(6)]
    docs += [make_doc(2, 0, title="Other", content="other page text")]
    return make_service(store=filled_store(docs))


def test_a_whole_page_is_read_by_url_in_page_order(conf):
    rows, focus, total = page_service().fetch_chunks(url="https://sujeeth.org/p1", max_chars=100000)
    assert focus is None and total == 6 and [r["id"] for r in rows] == [f"page1-{n}" for n in range(6)]
    assert rows[0]["content"].startswith("chunk number 0") and rows[0]["domain"] == "sujeeth.org" and rows[0]["concepts"] == ["circle"]


def test_url_variants_with_and_without_a_trailing_slash_or_fragment_match(conf):
    svc = page_service()
    for variant in ("https://sujeeth.org/p1/", "https://sujeeth.org/p1#section", "https://sujeeth.org/p1"):
        assert svc.fetch_chunks(url=variant, max_chars=100000)[2] == 6
    assert svc.fetch_chunks(url="https://sujeeth.org/nope", max_chars=1000) == ([], None, None)


def test_a_page_read_stops_loading_text_once_max_chars_is_reached(conf):
    docs = [make_doc(1, n, content=f"chunk {n} " + "word " * 40) for n in range(20)]
    rows, _, total = make_service(store=filled_store(docs)).fetch_chunks(url="https://sujeeth.org/p1", max_chars=100)
    assert total == 20 and len(rows) == 20
    assert rows[0]["content"] and rows[7]["content"]              # text is read eight chunks at a time...
    assert rows[8]["content"] == "" and rows[-1]["content"] == ""  # ...and reading stops once max_chars is reached


def test_a_chunk_is_read_with_its_neighbours(conf):
    rows, focus, total = page_service().fetch_chunks(chunk_id="page1-3", neighbors=1)
    assert focus == 3 and total == 6 and [r["id"] for r in rows] == ["page1-2", "page1-3", "page1-4"]
    rows, focus, _ = page_service().fetch_chunks(chunk_id="page1-0", neighbors=3)
    assert [r["id"] for r in rows] == ["page1-0", "page1-1", "page1-2", "page1-3"]
    rows, _, _ = page_service().fetch_chunks(chunk_id="page1-3", neighbors=99)
    assert len(rows) == 6                                     # neighbours are capped at three each side


def test_bad_page_reads_are_rejected_as_before(conf):
    with pytest.raises(ValueError, match="chunk_id must look like"):
        page_service().fetch_chunks(chunk_id="garbage")
    with pytest.raises(ValueError, match="Provide url or chunk_id"):
        page_service().fetch_chunks()


def test_a_local_page_read_feeds_assemble_document(conf):
    rows, focus, _ = page_service().fetch_chunks(chunk_id="page1-3", neighbors=1)
    chosen, truncated = ss.assemble_document(rows, focus, 100000)
    assert [r["id"] for r in chosen] == ["page1-2", "page1-3", "page1-4"] and not truncated


def test_a_page_read_falls_back_to_azure_when_the_index_is_unavailable(conf, monkeypatch):
    azure_configured(monkeypatch)
    row = {"id": "page1-0", "title": "T", "url": "https://sujeeth.org/p1", "domain": "sujeeth.org", "concepts": [], "content": "azure"}
    svc = make_service(store=new_store(), session=AzureSession([row]))
    rows, _, _ = svc.fetch_chunks(url="https://sujeeth.org/p1", max_chars=1000)
    assert rows[0]["content"] == "azure"


# ---- writing

def search_docs(svc, docs):
    return [svc._search_doc({k: v for k, v in d.items() if k != "contentVector"}, d["contentVector"]) for d in docs]


def test_chunks_go_to_the_table_and_the_version_changes_when_azure_writes_are_off(conf):
    store = new_store()
    svc = make_service(store=store)
    svc._upload(search_docs(svc, [make_doc(1, n) for n in range(3)]))
    assert len(store.chunks.rows) == 3 and store.get_version() and svc.session.posts == []


def test_with_both_writes_on_chunks_go_to_azure_and_the_table(conf, monkeypatch):
    azure_configured(monkeypatch)
    monkeypatch.setattr(settings, "ai_search_writes", True)
    store = new_store()
    svc = make_service(store=store)
    svc._upload(search_docs(svc, [make_doc(1, n) for n in range(3)]))
    assert len(store.chunks.rows) == 3 and len(svc.session.posts) == 1


def test_a_table_failure_is_only_logged_while_azure_still_takes_the_writes(conf, monkeypatch):
    azure_configured(monkeypatch)
    monkeypatch.setattr(settings, "ai_search_writes", True)
    store = new_store(FakeTable())
    store.chunks.submit_transaction = lambda ops: (_ for _ in ()).throw(RuntimeError("table down"))
    store.chunks.upsert_entity = lambda e, mode=None: (_ for _ in ()).throw(RuntimeError("table down"))
    svc = make_service(store=store)
    svc._upload(search_docs(svc, [make_doc(1, 0)]))            # does not raise
    assert len(svc.session.posts) == 1


def test_a_table_failure_is_raised_once_azure_is_off_so_the_queue_retries(conf):
    store = new_store(FakeTable())
    store.chunks.submit_transaction = lambda ops: (_ for _ in ()).throw(RuntimeError("table down"))
    store.chunks.upsert_entity = lambda e, mode=None: (_ for _ in ()).throw(RuntimeError("table down"))
    svc = make_service(store=store)
    with pytest.raises(RuntimeError, match="table down"):
        svc._upload(search_docs(svc, [make_doc(1, 0)]))


def test_the_table_is_not_written_unless_asked(conf, monkeypatch):
    azure_configured(monkeypatch)
    monkeypatch.setattr(settings, "ai_search_writes", True)
    monkeypatch.setattr(settings, "local_index_write", False)
    store = new_store()
    svc = make_service(store=store)
    svc._upload(search_docs(svc, [make_doc(1, 0)]))
    assert not store.chunks.rows and store.get_version() is None


def test_deleting_chunks_removes_them_from_the_table_too(conf):
    store = filled_store([make_doc(1, n) for n in range(4)])
    svc = make_service(store=store)
    assert svc.delete_ids(["page1-0", "page1-1"]) == 2
    assert sorted(r for _, r in store.chunks.rows) == ["000002", "000003"]


def test_delete_ids_does_nothing_when_neither_index_is_in_use(conf, monkeypatch):
    monkeypatch.setattr(settings, "local_index_write", False)
    assert make_service().delete_ids(["page1-0"]) == 0


def test_delete_url_uses_the_table_when_it_is_the_index(conf):
    import hashlib
    url = "https://sujeeth.org/p1"
    page = hashlib.sha256(url.encode()).hexdigest()
    docs = [dict(make_doc(1, n), id=f"{page}-{n}", url=url) for n in range(3)]
    store = filled_store(docs)
    svc = make_service(store=store)
    for d in docs:
        svc.storage.put("documents/" + d["id"] + ".json", {"id": d["id"]})
        svc.storage.put(svc.vector_key(d["id"]), {"id": d["id"]})
    assert svc.delete_url(url) == 3
    assert not store.chunks.rows and not svc.storage.blobs
    assert svc.delete_url(url) == 0


def test_delete_page_with_a_known_chunk_count_reaches_the_table(conf):
    import hashlib
    url = "https://sujeeth.org/p1"
    page = hashlib.sha256(url.encode()).hexdigest()
    store = filled_store([dict(make_doc(1, n), id=f"{page}-{n}") for n in range(3)])
    svc = make_service(store=store)
    assert svc.delete_page(url, chunk_count=3) == 3 and not store.chunks.rows


# ---- backfill

def blob_doc(page, n, **kw):
    d = make_doc(page, n, **kw)
    return {"id": d["id"], "title": d["title"], "url": d["url"], "domain": d["domain"], "concepts": d["concepts"],
            "content": d["content"]}, d["contentVector"]


def backfill_service(conf_unused=None, with_vectors=True):
    store = new_store()
    svc = make_service(store=store)
    names = []
    for n in range(6):
        doc, vec = blob_doc(1, n)
        svc.storage.put(f"documents/{doc['id']}.json", doc)
        names.append(f"documents/{doc['id']}.json")
        if with_vectors and n != 5:
            svc.storage.put(svc.vector_key(doc["id"]), {"id": doc["id"], "sha": svc._content_sha(doc["content"]),
                                                       "model": settings.embedding_deployment, "dims": DIMS,
                                                       "v": svc._pack_vector(vec)})
    return svc, store, names


def test_the_backfill_copies_blob_chunks_to_the_table_and_embeds_only_what_has_no_stored_vector(conf):
    svc, store, names = backfill_service()
    outcome = svc.local_backfill(names)
    assert outcome == {"read": 6, "written": 6} and len(store.chunks.rows) == 6
    assert sum(svc.embedder.calls) == 1                       # only the chunk without a stored vector
    assert store.get_version()
    assert svc.session.posts == []                             # Azure AI Search was never touched


def test_a_backfilled_chunk_can_be_found_by_search(conf):
    svc, store, names = backfill_service()
    svc.local_backfill(names)
    hit = svc.search("chunk", concepts=["circle"], domains=["sujeeth.org"], limit=5)
    assert hit["count"] > 0


def test_the_backfill_can_be_limited_to_domains_and_skips_chunks_without_text(conf):
    svc, store, names = backfill_service()
    svc.storage.put("documents/blank-0.json", {"id": "blank-0", "domain": "sujeeth.org", "content": ""})
    assert svc.local_backfill(names + ["documents/blank-0.json", "documents/gone.json"], domains=["other.org"])["written"] == 0
    assert svc.local_backfill(names + ["documents/blank-0.json", "documents/gone.json"], domains=["sujeeth.org"])["written"] == 6


def test_a_backfill_failure_is_raised_so_the_queue_retries_the_batch(conf):
    svc, store, names = backfill_service()
    store.chunks.submit_transaction = lambda ops: (_ for _ in ()).throw(RuntimeError("table down"))
    store.chunks.upsert_entity = lambda e, mode=None: (_ for _ in ()).throw(RuntimeError("table down"))
    with pytest.raises(RuntimeError, match="table down"):
        svc.local_backfill(names)


# ---- the queue

class Queue:
    def __init__(self):
        self.sent = []

    def send_message(self, content, visibility_timeout=None, **kwargs):
        self.sent.append(json.loads(content))


def processor(search):
    proc = object.__new__(qw.QueueProcessor)
    proc.queue, proc.search = Queue(), search
    proc.worker = types.SimpleNamespace(process=lambda task: {})
    return proc


def test_a_queued_local_batch_goes_to_the_table_and_not_to_azure(conf):
    class Search:
        def local_backfill(self, names, domains=None):
            return {"read": len(names), "written": len(names)}

        def index_blobs(self, *a, **k):
            raise AssertionError("Azure AI Search must not be touched in local mode")

    result = processor(Search()).process(json.dumps({"task_type": "index_blob_batch", "blob_names": ["a", "b"], "mode": "local"}))
    assert result["status"] == "local_indexed" and result["documents_indexed"] == 2


# ---------------------------------------------------------------------------------------------------------------
# Embedding a question
# ---------------------------------------------------------------------------------------------------------------

class VecResp:
    status_code = 200
    headers = {}

    def __init__(self, dims):
        self.dims = dims

    def json(self):
        return {"data": [{"index": 0, "embedding": [0.1] * self.dims}]}

    def raise_for_status(self):
        pass


def embedding_service(monkeypatch, dims=DIMS):
    monkeypatch.setattr(settings, "embedding_dimensions", DIMS)
    monkeypatch.setattr(settings, "embedding_configured", True, raising=False)
    monkeypatch.setattr(es, "_throttle", lambda: (_ for _ in ()).throw(AssertionError("a question must not wait on the indexing throttle")))
    service = es.EmbeddingService()
    calls = []
    service.session = types.SimpleNamespace(post=lambda url, params=None, json=None, timeout=None: calls.append((json, timeout)) or VecResp(dims))
    return service, calls


def test_a_question_is_embedded_once_and_remembered(monkeypatch):
    service, calls = embedding_service(monkeypatch)
    first = service.embed_query("How do I find a mentor?")
    assert service.embed_query("how do i   find a mentor?") == first and len(calls) == 1
    assert calls[0][1] == settings.embed_query_timeout         # gives up sooner than indexing does
    assert settings.embed_query_timeout < 90


class ThrottledResp:
    def __init__(self, retry_after):
        self.status_code = 429
        self.headers = {"Retry-After": str(retry_after)}

    def raise_for_status(self):
        raise RuntimeError("429 throttled")


def test_a_throttled_question_does_not_wait_out_a_long_retry_after(monkeypatch):
    service, _ = embedding_service(monkeypatch)
    calls, slept = [], []
    service.session = types.SimpleNamespace(
        post=lambda url, params=None, json=None, timeout=None: calls.append(timeout) or ThrottledResp(30))
    monkeypatch.setattr(es.time, "sleep", lambda s: slept.append(s))
    with pytest.raises(RuntimeError, match="429"):
        service.embed_query("how do I find a mentor")
    assert len(calls) == 1 and not slept                       # asked to wait 30 s: the search goes on without it


def test_a_short_throttle_is_retried_within_the_attempt_limit(monkeypatch):
    service, _ = embedding_service(monkeypatch)
    monkeypatch.setattr(settings, "embed_query_attempts", 2)
    answers = [ThrottledResp(1), VecResp(DIMS)]
    service.session = types.SimpleNamespace(post=lambda url, params=None, json=None, timeout=None: answers.pop(0))
    slept = []
    monkeypatch.setattr(es.time, "sleep", lambda s: slept.append(s))
    assert len(service.embed_query("a short wait")) == DIMS
    assert slept == [1.0]


def test_the_question_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(es, "QUERY_CACHE_ENTRIES", 3)
    service, calls = embedding_service(monkeypatch)
    for i in range(5):
        service.embed_query(f"question {i}")
    assert len(service._query_cache) == 3


def test_an_empty_question_and_a_wrong_sized_answer_are_errors(monkeypatch):
    service, _ = embedding_service(monkeypatch)
    with pytest.raises(ValueError):
        service.embed_query("   ")
    wrong, _ = embedding_service(monkeypatch, dims=DIMS + 1)
    with pytest.raises(RuntimeError, match="unexpected"):
        wrong.embed_query("mentor")


def test_indexing_embeddings_still_use_the_throttle(monkeypatch):
    monkeypatch.setattr(settings, "embedding_configured", True, raising=False)
    monkeypatch.setattr(settings, "embedding_dimensions", DIMS)
    seen = []
    monkeypatch.setattr(es, "_throttle", lambda: seen.append(1))
    service = es.EmbeddingService()
    service.session = types.SimpleNamespace(post=lambda url, params=None, json=None, timeout=None: VecResp(DIMS))
    service.embed(["one chunk"])
    assert seen == [1]


# ---------------------------------------------------------------------------------------------------------------
# Settings and the health route
# ---------------------------------------------------------------------------------------------------------------

def settings_in_a_fresh_process(**env):
    """Read the settings in a new Python process, because they are fixed when the module is first imported."""
    import os
    import subprocess
    import sys
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("SEARCH_", "AI_SEARCH", "LOCAL_"))}
    clean.update(env)
    code = ("import json; from services.config import settings as s; print(json.dumps([s.search_backend, s.ai_search_writes, "
            "s.local_index_write, s.local_shadow_sample, s.local_index_table, s.local_index_text_in_memory, s.local_search_fallback]))")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = subprocess.run([sys.executable, "-c", code], env=clean, cwd=root, capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_the_defaults_leave_behavior_exactly_as_it_was():
    assert settings_in_a_fresh_process() == ["azure", True, False, 0.0, "searchchunks", True, True]


def test_bad_setting_values_fall_back():
    got = settings_in_a_fresh_process(SEARCH_BACKEND="nonsense", LOCAL_SHADOW_SAMPLE="7", LOCAL_INDEX_TABLE="my-chunks_table!")
    assert got[0] == "azure" and got[3] == 1.0 and got[4] == "mychunkstable"
    assert settings_in_a_fresh_process(SEARCH_BACKEND=" LOCAL ", AI_SEARCH_WRITES="false", LOCAL_INDEX_WRITE="yes")[:3] == ["local", False, True]


def test_health_reports_the_local_index(conf):
    import azure.functions as func
    import function_app as fa
    request = func.HttpRequest("GET", "/api/health", headers={}, body=b"")
    body = json.loads(fa.health._function._func(request).get_body())
    assert body["search_backend"] == "local" and body["search_mode"] == "local_hybrid"
    assert body["local_index"]["backend"] == "local" and "shadow" in body["local_index"]


def test_status_includes_the_local_index_once_it_is_in_use(conf):
    svc = make_service()
    assert svc.status()["local_index"]["backend"] == "local"
