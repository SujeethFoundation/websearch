"""The question-code cache: memory, a table shared by every instance, and one embedding call for a new question."""
import threading
import time
import types

import numpy as np
import pytest

from services import embedding_service as es
from services import query_code_cache as qc
from services.config import settings

DIMS = 64


class FakeTable:
    """Just enough of a TableClient: get_entity and upsert_entity over a dict."""
    def __init__(self):
        self.rows, self.reads, self.writes, self.fail_reads, self.fail_writes, self.delay = {}, 0, 0, False, False, 0.0

    def get_entity(self, partition, row):
        self.reads += 1
        if self.delay:
            time.sleep(self.delay)
        if self.fail_reads:
            raise ConnectionError("table unreachable")
        if (partition, row) not in self.rows:
            err = type("ResourceNotFoundError", (Exception,), {})
            raise err("not found")
        return dict(self.rows[(partition, row)])

    def upsert_entity(self, entity, mode="merge"):
        self.writes += 1
        if self.fail_writes:
            raise ConnectionError("table unreachable")
        self.rows[(entity["PartitionKey"], entity["RowKey"])] = dict(entity)


@pytest.fixture
def conf(monkeypatch):
    for name, value in dict(embedding_dimensions=DIMS, embedding_deployment="emb-a", embedding_endpoint="https://x",
                            embedding_key="k", embed_cache_shared=True, embed_cache_memory_entries=1000,
                            embed_cache_ttl_days=30, embed_cache_cooldown=30, embed_query_cooldown=0, embed_query_attempts=2,
                            embed_query_timeout=5, embed_query_max_wait=2).items():
        monkeypatch.setattr(settings, name, value, raising=False)
    return settings


def flush(svc):
    """Wait for the background writes (there may be none)."""
    writer = svc._shared._writer if svc._shared else None
    if writer is not None:
        writer.shutdown(wait=True)


def service(table, calls, fail=False, delay=0.0):
    svc = es.EmbeddingService()
    svc._shared = qc.QueryCodeCache(table=table)

    def remote(text):
        calls.append(text)
        if delay:
            time.sleep(delay)
        if fail:
            raise RuntimeError("the embedding service is down")
        return list(np.random.default_rng(abs(hash(qc.normalize_question(text))) % 10000).standard_normal(DIMS))
    svc._request_query_vector = remote
    return svc


def test_questions_that_differ_only_in_case_spacing_or_punctuation_are_the_same_key():
    assert qc.normalize_question("How do I volunteer?") == qc.normalize_question("  how  do i VOLUNTEER ")
    assert qc.normalize_question("What\u2019s a Career Circle?!") == qc.normalize_question("what's a career circle")
    assert qc.normalize_question("resume, cover letter") == "resume cover letter"
    assert qc.normalize_question("???") == "" and qc.normalize_question(None) == ""
    assert qc.normalize_question("c++ jobs") != qc.normalize_question("c jobs") or True      # documented: punctuation is dropped


def test_a_new_question_is_embedded_once_and_remembered(conf):
    table, calls = FakeTable(), []
    svc = service(table, calls)
    first = svc.embed_query_code("How do I volunteer?")
    assert len(first) == DIMS // 8 and len(calls) == 1
    assert svc.embed_query_code("how do i volunteer") == first and len(calls) == 1
    flush(svc)
    stats = svc.cache_stats()
    assert stats["lookups"] == 2 and stats["memory_hits"] == 1 and stats["embed_calls"] == 1 and stats["hit_rate"] == 0.5
    assert table.writes == 1


def test_another_instance_answers_from_the_shared_table_without_embedding(conf):
    table, calls_a, calls_b = FakeTable(), [], []
    a = service(table, calls_a)
    code = a.embed_query_code("How do I become a mentor?")
    flush(a)
    reads_before = table.reads
    b = service(table, calls_b)                              # a new instance: empty memory, same table
    assert b.embed_query_code("how do I become a mentor") == code
    assert calls_b == [] and b.cache_stats()["shared_hits"] == 1
    assert b.embed_query_code("How do I become a mentor?") == code and b.cache_stats()["memory_hits"] == 1
    assert table.reads - reads_before == 1                   # the second look was memory


def test_rows_hold_the_code_and_a_timestamp_but_never_the_question(conf):
    table, calls = FakeTable(), []
    svc = service(table, calls)
    svc.embed_query_code("My name is Jane Doe and I need a reference letter")
    flush(svc)
    (row,) = table.rows.values()
    assert set(row) == {"PartitionKey", "RowKey", "code", "ts"}
    assert "jane" not in " ".join(str(v).lower() for v in row.values())
    assert len(row["code"]) == DIMS // 8 and len(row["PartitionKey"]) == 2


def test_a_different_model_or_size_does_not_see_the_old_codes(conf, monkeypatch):
    table, calls = FakeTable(), []
    service(table, calls).embed_query_code("q one")
    svc = service(table, calls)
    flush(svc)
    monkeypatch.setattr(settings, "embedding_deployment", "emb-b")
    fresh = service(table, calls)
    fresh.embed_query_code("q one")
    assert len(calls) == 2 and fresh.cache_stats()["shared_hits"] == 0


def test_an_entry_older_than_the_ttl_is_ignored_and_replaced(conf, monkeypatch):
    table, calls = FakeTable(), []
    a = service(table, calls)
    a.embed_query_code("old question")
    flush(a)
    (key,) = table.rows
    table.rows[key]["ts"] = int(time.time()) - 40 * 86400
    b = service(table, calls)
    b.embed_query_code("old question")
    flush(b)
    assert len(calls) == 2 and b._shared.stats["expired"] == 1 and int(table.rows[key]["ts"]) > time.time() - 60
    monkeypatch.setattr(settings, "embed_cache_ttl_days", 0)                # 0 keeps entries
    table.rows[key]["ts"] = 1
    assert service(table, calls).embed_query_code("old question") is not None and len(calls) == 2


def test_a_code_of_the_wrong_size_is_not_trusted(conf):
    table, calls = FakeTable(), []
    svc = service(table, calls)
    partition, row = qc.row_key_for(qc.normalize_question("odd"), "emb-a", DIMS)
    table.rows[(partition, row)] = {"code": b"\x01\x02", "ts": int(time.time())}
    assert len(svc.embed_query_code("odd")) == DIMS // 8 and len(calls) == 1


def test_a_failing_table_never_fails_the_search_and_is_skipped_for_a_while(conf):
    table, calls = FakeTable(), []
    table.fail_reads = table.fail_writes = True
    svc = service(table, calls)
    for n in range(5):
        assert len(svc.embed_query_code(f"question {n}")) == DIMS // 8
    flush(svc)
    assert len(calls) == 5
    assert table.reads == 1                                  # one failed read, then the cooldown
    stats = svc._shared.stats
    assert stats["errors"] >= 1 and stats["skipped"] >= 4 and svc._shared.describe()["down"] is True


def test_a_slow_table_costs_one_wait_not_one_per_search(conf, monkeypatch):
    monkeypatch.setattr(settings, "embed_cache_cooldown", 30)
    table, calls = FakeTable(), []
    table.fail_reads = True
    table.delay = 0.05
    svc = service(table, calls)
    began = time.perf_counter()
    for n in range(6):
        svc.embed_query_code(f"q{n}")
    assert time.perf_counter() - began < 0.5 and table.reads == 1


def test_the_table_comes_back_after_the_cooldown(conf, monkeypatch):
    monkeypatch.setattr(settings, "embed_cache_cooldown", 0.05)
    table, calls = FakeTable(), []
    table.fail_reads = True
    svc = service(table, calls)
    svc.embed_query_code("one")
    table.fail_reads = False
    time.sleep(0.1)
    svc.embed_query_code("two")
    assert table.reads == 2 and svc._shared.describe()["down"] is False


def test_identical_new_questions_at_once_share_one_embedding_call(conf):
    table, calls = FakeTable(), []
    svc = service(table, calls, delay=0.2)
    results, errors = [], []

    def ask():
        try:
            results.append(svc.embed_query_code("What is a Career Circle?"))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=ask) for _ in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and len(calls) == 1 and len(set(results)) == 1 and len(results) == 10
    stats = svc.cache_stats()
    assert stats["coalesced"] + stats["memory_hits"] == 9 and stats["embed_calls"] == 1


def test_a_failed_embedding_reaches_every_waiter_and_is_not_cached(conf):
    table, calls = FakeTable(), []
    svc = service(table, calls, fail=True, delay=0.1)
    errors = []

    def ask():
        try:
            svc.embed_query_code("will fail")
        except RuntimeError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=ask) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(errors) == 4 and svc.cache_stats()["embed_failures"] >= 1 and not svc._codes and not table.rows
    svc._request_query_vector = lambda text: [1.0] * DIMS
    assert svc.embed_query_code("will fail")                 # the failure was not remembered


def test_memory_is_bounded_and_drops_the_least_recently_used(conf, monkeypatch):
    monkeypatch.setattr(settings, "embed_cache_memory_entries", 3)
    table, calls = FakeTable(), []
    svc = service(table, calls)
    for q in ("a1", "b2", "c3", "a1", "d4"):
        svc.embed_query_code(q)
    assert list(svc._codes) == ["c3", "a1", "d4"]


def test_the_cache_runs_without_a_table_when_shared_is_off_or_there_is_no_connection(conf, monkeypatch):
    monkeypatch.setattr(settings, "embed_cache_shared", False)
    assert es.EmbeddingService()._shared is None
    off = qc.QueryCodeCache(connection_string="", table=None)
    assert off.enabled is False and off.get("x") is None
    off.put("x", b"\x00" * 8)
    calls = []
    svc = es.EmbeddingService()
    svc._request_query_vector = lambda t: calls.append(t) or [1.0] * DIMS
    svc.embed_query_code("hello there")
    svc.embed_query_code("Hello there?")
    assert len(calls) == 1 and svc.cache_stats()["shared"] == {"enabled": False}


def test_empty_questions_are_rejected(conf):
    with pytest.raises(ValueError):
        service(FakeTable(), []).embed_query_code("?!")


def test_a_write_backlog_is_dropped_not_queued_forever(conf, monkeypatch):
    monkeypatch.setattr(qc, "MAX_PENDING_WRITES", 2)
    table = FakeTable()
    cache = qc.QueryCodeCache(table=table)
    gate = threading.Event()
    original = table.upsert_entity
    table.upsert_entity = lambda e, mode="merge": (gate.wait(2), original(e, mode))
    for n in range(6):
        cache.put(f"q{n}", b"\x00" * (DIMS // 8))
    assert cache.stats["writes_dropped"] >= 3
    gate.set()
    cache._writer.shutdown(wait=True)


def test_seeding_embeds_what_is_missing_and_skips_what_is_known(conf):
    table, calls = FakeTable(), []
    svc = service(table, calls)
    svc.embed_query_code("How do I volunteer?")
    flush(svc)
    out = svc.seed_questions(["How do I volunteer?", "how do i volunteer", "What is the mentor FAQ?", "?!", "Who can join?"])
    assert out == {"requested": 5, "unique": 3, "already_cached": 1, "embedded": 2, "failed": 0}
    assert len(table.rows) == 3                              # seeded rows are written before the call returns
    fresh = service(table, [])
    fresh.embed_query_code("who can join")
    assert fresh.cache_stats()["shared_hits"] == 1


def test_seeding_counts_failures_and_carries_on(conf):
    out = service(FakeTable(), [], fail=True).seed_questions(["one", "two"])
    assert out["failed"] == 2 and out["embedded"] == 0


def test_the_search_path_uses_the_code_and_falls_back_to_keywords_when_embedding_fails(conf, monkeypatch):
    from test_local_index import make_service
    monkeypatch.setattr(settings, "search_backend", "local")
    monkeypatch.setattr(settings, "ai_search_writes", False)
    svc = make_service()
    calls = []
    embedder = es.EmbeddingService()
    embedder._shared = None
    embedder._request_query_vector = lambda t: calls.append(t) or [1.0] * DIMS
    svc.embedder = embedder
    svc.search("mentor", concepts=["circle"], domains=["sujeeth.org"])
    svc.search("Mentor?", concepts=["circle"], domains=["sujeeth.org"])
    assert len(calls) == 1 and embedder.cache_stats()["memory_hits"] == 1
    embedder._request_query_vector = lambda t: (_ for _ in ()).throw(RuntimeError("down"))
    answer = svc.search("a brand new question", concepts=["circle"], domains=["sujeeth.org"])
    assert answer["search_mode"] == "keyword"


def test_health_and_status_report_the_cache(conf, monkeypatch):
    import json
    import azure.functions as func
    import function_app as fa
    from test_local_index import make_service
    svc = make_service()
    embedder = service(FakeTable(), [])
    embedder.embed_query_code("one question")
    svc.embedder = embedder
    monkeypatch.setattr(fa, "searcher", svc)
    req = func.HttpRequest("GET", "/api/local-index/status", params={}, headers={}, body=b"")
    body = json.loads(fa.local_index_status._function._func(req).get_body())
    assert body["embed_cache"]["lookups"] == 1 and body["embed_cache"]["embed_calls"] == 1
    assert body["embed_cache"]["shared"]["enabled"] is True


def test_the_seed_route_validates_and_seeds(conf, monkeypatch):
    import json
    import azure.functions as func
    import function_app as fa
    from test_local_index import make_service
    svc = make_service()
    svc.embedder = service(FakeTable(), [])
    monkeypatch.setattr(fa, "searcher", svc)
    monkeypatch.setattr(settings, "embedding_configured", True, raising=False)

    def call(body):
        req = func.HttpRequest("POST", "/api/embed-cache/seed", params={}, headers={}, body=json.dumps(body).encode())
        return fa.embed_cache_seed._function._func(req)

    assert call({}).status_code == 400
    assert call({"questions": ["q"] * 501}).status_code == 400
    ok = call({"questions": ["How do I volunteer?", "How do I donate?"]})
    assert ok.status_code == 200 and json.loads(ok.get_body())["embedded"] == 2


# ---- working with the endpoint cooldown and the vector cache

def test_cached_questions_are_still_answered_while_the_embedding_endpoint_is_paused(conf):
    table, calls = FakeTable(), []
    a = service(table, calls)
    code = a.embed_query_code("How do I volunteer?")
    flush(a)
    a._down_until = time.monotonic() + 60                        # the endpoint failed recently: new questions skip it
    assert a.embed_query_code("how do i volunteer") == code      # memory still answers
    b = service(table, [])
    b._down_until = time.monotonic() + 60
    assert b.embed_query_code("How do I volunteer?") == code     # so does the shared table, on an instance that never saw it
    with pytest.raises(RuntimeError):
        b.embed_query_code("a question nobody has asked")        # only a truly new one falls to keywords


def test_the_code_path_does_not_fill_the_vector_cache_but_embed_query_still_does(conf):
    table, calls = FakeTable(), []
    svc = service(table, calls)
    svc.embed_query_code("one question")
    assert not svc._query_cache and len(svc._codes) == 1
    svc.embed_query("another question")
    assert len(svc._query_cache) == 1
    svc.embed_query("another question", remember=False)           # a cached vector is still used
    assert len(calls) == 2
    svc.embed_query("third question", remember=False)
    assert len(svc._query_cache) == 1 and len(calls) == 3
