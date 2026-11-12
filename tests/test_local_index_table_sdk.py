"""The local index against a real Table Storage endpoint (the Azurite emulator, or a test storage account).

Skipped unless LOCAL_INDEX_TEST_CONNECTION is set to a storage connection string. To run it against Azurite:
    npm install -g azurite
    azurite-table --silent --location /tmp/azurite
    LOCAL_INDEX_TEST_CONNECTION="DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;TableEndpoint=http://127.0.0.1:10002/devstoreaccount1;" python -m pytest -q tests/test_local_index_table_sdk.py
It checks what the fakes in test_local_index.py cannot: that the real SDK accepts the rows (binary values, transactions
of one partition), returns them unchanged, and handles deletes of rows that are not there.
"""
import os
import uuid

import numpy as np
import pytest

from services import local_index as li
from services import search_service as ss
from services.config import settings

CONNECTION = os.getenv("LOCAL_INDEX_TEST_CONNECTION", "")
pytestmark = pytest.mark.skipif(not CONNECTION, reason="set LOCAL_INDEX_TEST_CONNECTION to run against Table Storage")

DIMS = 1536


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(settings, "embedding_dimensions", DIMS)
    monkeypatch.setattr(settings, "search_vector_field", "contentVector")
    return li.LocalIndexStore(connection_string=CONNECTION, table_name="t" + uuid.uuid4().hex[:12])


def doc(page, n, domain="sujeeth.org", concepts=("circle",), seed=0):
    rng = np.random.default_rng(hash((page, n, seed)) % 2 ** 32)
    return {"id": f"{page}-{n}", "title": f"Title {page}", "url": f"https://{domain}/{page}", "domain": domain,
            "concepts": list(concepts), "content": f"mentor guide number {n} " + "career advice " * 30,
            "contentVector": [float(x) for x in rng.standard_normal(DIMS)]}


def test_rows_survive_a_round_trip_through_the_real_sdk_unchanged(store):
    docs = [doc(f"page{p}", n) for p in range(3) for n in range(150)]   # 150 rows a page: two transactions each
    sent = {e["id"]: e for e in (li.entity_from_search_doc(d) for d in docs)}
    store.upsert_entities(list(sent.values()))
    rows = [li.row_from_entity(e) for e in store.scan(True)]
    assert len(rows) == 450
    for row in rows:
        assert row["vec"] == sent[row["id"]]["vec"] and len(row["vec"]) == DIMS // 8
        assert row["tid"] == sent[row["id"]]["tid"] and row["tcnt"] == sent[row["id"]]["tcnt"]
        assert row["text"] == sent[row["id"]]["text"] and row["concepts"] == ["circle"]
    assert "text" not in next(iter(store.scan(False)))


def test_deletes_ignore_rows_that_are_not_there_and_pages_can_be_listed(store):
    store.upsert_entities([li.entity_from_search_doc(doc("page1", n)) for n in range(5)])
    assert sorted(store.page_ids("page1")) == [f"page1-{n}" for n in range(5)]
    store.delete_ids(["page1-0", "page1-1", "page1-99", "nothere-3"])
    assert sorted(store.page_ids("page1")) == ["page1-2", "page1-3", "page1-4"]
    assert store.get_text("page1-2").startswith("mentor guide") and store.get_text("page1-0") is None


def test_the_version_row_round_trips(store):
    assert store.get_version() is None
    token = store.bump_version()
    assert store.get_version() == token


def test_write_through_search_and_delete_work_end_to_end(store, monkeypatch):
    class Blobs:
        def get(self, name):
            return None

        def delete(self, name):
            pass

    class Embedder:
        def embed_query(self, text):
            return list(np.random.default_rng(5).standard_normal(DIMS))

    monkeypatch.setattr(settings, "search_backend", "local")
    monkeypatch.setattr(settings, "local_index_write", True)
    monkeypatch.setattr(settings, "ai_search_writes", False)
    monkeypatch.setattr(settings, "search_cache_seconds", 0)
    monkeypatch.setattr(ss.SearchService, "configured", property(lambda self: False))
    svc = object.__new__(ss.SearchService)
    svc.storage, svc.embedder, svc.local = Blobs(), Embedder(), li.LocalIndexManager(store=store)
    docs = [doc("pageA", n) for n in range(6)] + [doc("pageB", n, domain="themuse.com", concepts=("resume",)) for n in range(4)]
    svc._upload([svc._search_doc({k: v for k, v in d.items() if k != "contentVector"}, d["contentVector"]) for d in docs])
    answer = svc.search("mentor guide", concepts=["circle"], domains=["sujeeth.org"], limit=5)
    assert answer["count"] == 5 and {r["chunk_id"].split("-")[0] for r in answer["results"]} == {"pageA"}
    rows, focus, total = svc.fetch_chunks(url="https://sujeeth.org/pageA", max_chars=100000)
    assert total == 6 and [r["id"] for r in rows] == [f"pageA-{n}" for n in range(6)]
    assert svc.delete_ids([f"pageA-{n}" for n in range(6)]) == 6
    svc.local.reload()
    assert svc.local.current().n == 4


def test_existing_ids_and_counts_work_against_the_real_sdk(store):
    store.upsert_entities([li.entity_from_search_doc(doc("p1", n)) for n in range(4)]
                          + [li.entity_from_search_doc(doc("p2", 0, domain="themuse.com"))])
    assert store.existing_ids(["p1-0", "p1-3", "p1-9", "p2-0", "p7-0"]) == {"p1-0", "p1-3", "p2-0"}
    assert store.count() == 5 and store.count("themuse.com") == 1 and store.count("nowhere.org") == 0
