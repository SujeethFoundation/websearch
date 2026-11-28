"""The shared result table: an answer one instance finished is read by another instead of searching again."""
import time

import numpy as np

from services import local_index as li
from services import search_service as ss
from services.config import settings
from services.result_store import ResultStore
from test_index_snapshot import FakeContainer  # noqa: F401
from test_local_index import DIMS, conf, make_service, new_store  # noqa: F401
from test_local_rescoring import VectorEmbedder
from test_search_many import big_corpus, service_jobs


class FakeTable:
    def __init__(self):
        self.rows, self.fail = {}, False

    def get_entity(self, partition, row):
        if self.fail:
            raise RuntimeError("down")
        if (partition, row) not in self.rows:
            class ResourceNotFoundError(Exception):
                pass
            raise ResourceNotFoundError()
        return dict(self.rows[(partition, row)])

    def upsert_entity(self, entity, mode=None):
        if self.fail:
            raise RuntimeError("down")
        self.rows[(entity["PartitionKey"], entity["RowKey"])] = dict(entity)


KEY = ("how do i network", ("circle",), ("sujeeth.org",), 5, (), ())


def test_an_answer_put_by_one_store_is_read_by_another_and_only_for_its_index_version():
    table = FakeTable()
    ResultStore(connection_string="x", ttl_seconds=60, table=table).put("v1", KEY, {"count": 2, "results": [1, 2]}, wait=True)
    reader = ResultStore(connection_string="x", ttl_seconds=60, table=table)
    assert reader.get("v1", KEY) == {"count": 2, "results": [1, 2]}
    assert reader.get("v2", KEY) is None                       # a crawl made a new index version: a miss by itself


def test_an_old_row_misses_and_a_failing_table_is_skipped():
    table = FakeTable()
    store = ResultStore(connection_string="x", ttl_seconds=60, table=table)
    store.put("v1", KEY, {"count": 1}, wait=True)
    for row in table.rows.values():
        row["ts"] = int(time.time()) - 600
    assert store.get("v1", KEY) is None
    table.fail = True
    assert store.get("v1", KEY) is None and store.get("v1", KEY) is None
    assert store.stats["errors"] == 1 and store.stats["skipped"] == 1


def test_off_by_default_and_for_oversized_answers():
    assert not ResultStore(connection_string="x", ttl_seconds=0).enabled
    assert not ResultStore(connection_string="", ttl_seconds=60).enabled
    store = ResultStore(connection_string="x", ttl_seconds=60, table=FakeTable())
    store.put("v1", KEY, {"blob": np.random.default_rng(1).bytes(200000).hex()}, wait=True)
    assert store.stats["too_big"] == 1 and store.get("v1", KEY) is None


def test_a_second_instance_answers_from_the_table_without_embedding_or_searching(conf, monkeypatch):
    monkeypatch.setattr(settings, "search_cache_seconds", 0)
    ss._query_cache_clear()
    docs = big_corpus()
    store = new_store()
    store.upsert_entities([li.entity_from_search_doc(d) for d in docs])
    store.bump_version()
    table = FakeTable()
    first_embedder = VectorEmbedder(np.random.default_rng(5).standard_normal(DIMS))
    second_embedder = VectorEmbedder(np.random.default_rng(5).standard_normal(DIMS))
    first = make_service(store=store, embedder=first_embedder)
    second = make_service(store=store, embedder=second_embedder)
    for svc in (first, second):
        svc.results = ResultStore(connection_string="x", ttl_seconds=60, table=table)
    question = "w3 w14 w27 mentor circle"
    answered = first.search_many(question, service_jobs())
    for _ in range(50):                                          # the write is in the background
        if first.results.stats["writes"] >= 3:
            break
        time.sleep(0.05)
    assert first.results.stats["writes"] == 3
    again = second.search_many(question, service_jobs())
    assert again == answered
    assert second_embedder.calls == [] and second.local.describe()["searches"] == 0


class SweepTable(FakeTable):
    def query_entities(self, query_filter, select=None):
        cutoff = int(query_filter.split()[-1])
        return [{"PartitionKey": p, "RowKey": r} for (p, r), v in list(self.rows.items()) if int(v.get("ts") or 0) < cutoff]

    def delete_entity(self, partition, row):
        del self.rows[(partition, row)]


def test_a_sweep_removes_only_rows_past_their_lifetime_and_grace():
    from services.result_store import sweep_expired
    table, now = SweepTable(), int(time.time())
    table.rows[("a", "fresh")] = {"ts": now}
    table.rows[("a", "old")] = {"ts": now - 600 - 3600 - 10}
    table.rows[("a", "barely")] = {"ts": now - 600 - 60}
    assert sweep_expired(table, 600) == 1 and set(r for _, r in table.rows) == {"fresh", "barely"}


def test_a_sweep_stops_at_the_row_limit():
    from services.result_store import sweep_expired
    table = SweepTable()
    for n in range(5):
        table.rows[("p", str(n))] = {"ts": 1}
    assert sweep_expired(table, 600, max_rows=3) == 3 and len(table.rows) == 2


def test_a_background_write_starts_one_sweep_when_due():
    table = SweepTable()
    table.rows[("p", "stale")] = {"ts": 1}
    store = ResultStore("x", "t", 600, table=table)
    store.put("v1", KEY, {"results": []})
    for _ in range(100):
        if store.stats.get("swept"):
            break
        time.sleep(0.02)
    assert store.stats.get("swept") == 1 and ("p", "stale") not in table.rows
    store.put("v1", ("another",), {"results": []})
    time.sleep(0.1)
    assert store.stats.get("swept") == 1
