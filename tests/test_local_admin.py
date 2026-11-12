"""Tests for the admin reads answered from the local index: tags, counts, page tags, status and existing-chunk checks."""
import json
import types

import azure.functions as func
import pytest

import function_app as fa
from services import local_index as li
from services import search_service as ss
from services.config import settings
from test_local_index import (  # the fakes and helpers the main local-index tests use
    Blobs, Embedder, conf, entities, filled_store, make_doc, make_service, new_store,
)


def tagged_service(**kw):
    docs = [
        make_doc(1, 0, domain="sujeeth.org", concepts=("circle", "mentor"), content="a"),
        make_doc(1, 1, domain="sujeeth.org", concepts=("circle",), content="b"),
        make_doc(1, 2, domain="sujeeth.org", concepts=(), content="c"),
        make_doc(2, 0, domain="themuse.com", concepts=("resume", "Circle"), content="d"),
        make_doc(3, 0, domain="themuse.com", concepts=("resume",), content="e"),
        make_doc(3, 1, domain="themuse.com", concepts=("resume",), content="f"),
    ]
    return make_service(store=filled_store(docs), **kw)


def test_tag_counts_are_exact_and_have_the_shape_of_the_facet_answer(conf):
    answer = tagged_service().tag_counts()
    assert answer["source"] == "local_index" and answer["total_chunks"] == 6 and answer["untagged_chunks"] == 1
    assert {t["tag"]: t["chunks"] for t in answer["tags"]} == {"circle": 2, "resume": 3, "Circle": 1, "mentor": 1}
    assert answer["tags"][0] == {"tag": "resume", "chunks": 3}                  # most chunks first
    assert answer["domains"] == [{"domain": "sujeeth.org", "chunks": 3}, {"domain": "themuse.com", "chunks": 3}]   # a tie: by name


def test_tag_counts_for_one_domain_leave_out_the_domain_list(conf):
    answer = tagged_service().tag_counts("sujeeth.org")
    assert answer["total_chunks"] == 3 and answer["untagged_chunks"] == 1 and answer["domains"] == []
    assert {t["tag"]: t["chunks"] for t in answer["tags"]} == {"circle": 2, "mentor": 1}
    assert tagged_service().tag_counts("nowhere.org")["total_chunks"] == 0


def test_tag_counts_can_be_limited_to_the_top_few(conf):
    assert len(tagged_service().tag_counts(top=2)["tags"]) == 2


def test_chunks_can_be_counted_overall_and_per_domain(conf):
    svc = tagged_service()
    assert svc.count_chunks() == 6 and svc.count_chunks("sujeeth.org") == 3 and svc.count_chunks("THEMUSE.com") == 3
    assert svc.count_chunks("nowhere.org") == 0


def test_the_pages_that_carry_a_tag_are_ranked_by_how_many_chunks_have_it(conf):
    answer = tagged_service().pages_with_tag("RESUME")
    assert answer["tag"] == "resume" and answer["chunks_matched"] == 3 and answer["pages_found"] == 2
    assert [(p["url"], p["chunks"]) for p in answer["pages"]] == [("https://themuse.com/p3", 2), ("https://themuse.com/p2", 1)]
    assert answer["pages"][0]["domain"] == "themuse.com" and answer["pages"][0]["title"]
    assert tagged_service().pages_with_tag("circle", "sujeeth.org")["chunks_matched"] == 2
    assert len(tagged_service().pages_with_tag("resume", limit=1)["pages"]) == 1
    assert tagged_service().pages_with_tag("nothing")["pages"] == []


def test_the_tags_on_each_chunk_of_a_page_are_listed_in_page_order(conf):
    svc = tagged_service()
    answer = svc.page_tags("https://sujeeth.org/p1/")
    assert answer["url"] == "https://sujeeth.org/p1" and answer["domain"] == "sujeeth.org"
    assert answer["chunks"] == [{"chunk": 0, "tags": ["circle", "mentor"]}, {"chunk": 1, "tags": ["circle"]}, {"chunk": 2, "tags": []}]
    missing = svc.page_tags("https://sujeeth.org/nope")
    assert missing == {"url": "https://sujeeth.org/nope", "title": None, "domain": None, "chunks": []}


def test_status_counts_what_the_table_holds_now(conf):
    out = tagged_service().status()
    assert out["document_count"] == 6 and out["source"] == "local_index_table" and out["local_index"]["enabled"] is True


def test_status_reports_a_failed_count_instead_of_raising(conf):
    svc = tagged_service()
    svc.local._store.chunks.list_entities = lambda **kw: (_ for _ in ()).throw(RuntimeError("storage down"))
    out = svc.status()
    assert out["document_count"] is None and "storage down" in out["error"]


# ---- existing chunks and the missing-only backfill

def test_the_table_can_say_which_chunk_ids_it_already_holds(conf):
    store = filled_store([make_doc(1, n) for n in range(3)] + [make_doc(2, 0)])
    assert store.existing_ids(["page1-0", "page1-2", "page1-7", "page2-0", "page9-0", "garbage"]) == {"page1-0", "page1-2", "page2-0"}
    assert store.existing_ids([]) == set()


def test_the_table_counts_all_chunks_or_one_domains(conf):
    store = filled_store([make_doc(1, 0), make_doc(2, 0, domain="themuse.com")])
    assert store.count() == 2 and store.count("themuse.com") == 1 and store.count("nowhere") == 0


def test_a_missing_only_backfill_writes_only_what_the_table_lacks(conf, monkeypatch):
    monkeypatch.setattr(settings, "embedding_configured", True, raising=False)
    store = filled_store([make_doc(1, 0), make_doc(1, 1)])
    svc = make_service(store=store)
    for n in range(4):
        d = make_doc(1, n)
        doc = {k: v for k, v in d.items() if k != "contentVector"}
        svc.storage.put(f"documents/{doc['id']}.json", doc)
        svc.storage.put(svc.vector_key(doc["id"]), {"id": doc["id"], "sha": svc._content_sha(doc["content"]),
                                                   "model": settings.embedding_deployment, "dims": 64,
                                                   "v": svc._pack_vector(d["contentVector"])})
    out = svc.index_blobs([f"documents/page1-{n}.json" for n in range(4)], only_missing=True)
    assert out == {"read": 4, "already_indexed": 2, "indexed": 2}
    assert store.count() == 4


def test_a_retag_updates_the_table(conf):
    store = filled_store([make_doc(1, 0, concepts=("circle",), content="mentor guide")])
    svc = make_service(store=store)
    doc = {k: v for k, v in make_doc(1, 0, concepts=("circle",), content="mentor guide").items() if k != "contentVector"}
    svc.storage.put("documents/page1-0.json", doc)
    svc.storage.put(svc.vector_key("page1-0"), {"id": "page1-0", "sha": svc._content_sha(doc["content"]),
                                               "model": settings.embedding_deployment, "dims": 64,
                                               "v": svc._pack_vector(make_doc(1, 0)["contentVector"])})
    assert svc.update_concepts([("page1-0", ["resume"])], [doc]) == (1, 0)
    svc.local.reload()
    assert svc.page_tags("https://sujeeth.org/p1")["chunks"][0]["tags"] == ["resume"]
    assert svc.search("mentor", concepts=["resume"], domains=["sujeeth.org"])["count"] == 1


# ---- the routes

def request(method, route, params=None, body=None):
    return func.HttpRequest(method, f"/api/{route}", params=params or {}, headers={}, body=json.dumps(body or {}).encode())


@pytest.fixture
def routes(conf, monkeypatch):
    svc = tagged_service()
    monkeypatch.setattr(fa, "searcher", svc)
    return svc


def test_the_tags_route_reads_the_local_index(routes):
    body = json.loads(fa.index_tags._function._func(request("GET", "index/tags")).get_body())
    assert body["source"] == "local_index" and body["total_chunks"] == 6
    body = json.loads(fa.index_tags._function._func(request("GET", "index/tags", {"domain": "sujeeth.org"})).get_body())
    assert body["total_chunks"] == 3
    body = json.loads(fa.index_tags._function._func(request("GET", "index/tags", {"tag": "resume", "limit": "1"})).get_body())
    assert body["chunks_matched"] == 3 and len(body["pages"]) == 1
    body = json.loads(fa.index_tags._function._func(request("GET", "index/tags", {"url": "https://sujeeth.org/p1"})).get_body())
    assert len(body["chunks"]) == 3


def test_the_tags_route_and_index_available_need_the_table(conf, monkeypatch):
    monkeypatch.setattr(fa, "searcher", types.SimpleNamespace(local=types.SimpleNamespace(enabled=False)))
    assert fa.index_available() is False
    assert fa.index_tags._function._func(request("GET", "index/tags")).status_code == 503
    monkeypatch.setattr(fa, "searcher", tagged_service())
    assert fa.index_available() is True


def test_the_backfill_route_takes_missing_or_all_and_the_old_name_local(conf, monkeypatch):
    sent = []
    monkeypatch.setattr(fa, "_enqueue", lambda message: sent.append(message))
    monkeypatch.setattr(fa, "searcher", tagged_service())
    monkeypatch.setattr(settings, "azure_storage_connection", "x", raising=False)
    monkeypatch.setattr(settings, "embedding_configured", True, raising=False)
    monkeypatch.setattr(fa.storage, "container", object(), raising=False)
    monkeypatch.setattr(fa, "queue_processor", types.SimpleNamespace(queue=None))
    post = lambda body: fa.index_backfill._function._func(request("POST", "index/backfill", body=body))
    assert post({}).status_code == 202 and sent[-1]["mode"] == "missing" and sent[-1]["task_type"] == "index_backfill_page"
    assert post({"mode": "all"}).status_code == 202 and sent[-1]["mode"] == "all"
    reply = post({"mode": "local", "domains": ["Sujeeth.org"]})
    assert reply.status_code == 202 and sent[-1]["mode"] == "all" and sent[-1]["domains"] == ["sujeeth.org"]   # the old name for "all"
    assert json.loads(reply.get_body())["mode"] == "all"
    assert post({"mode": "bogus"}).status_code == 400
    monkeypatch.setattr(settings, "embedding_configured", False, raising=False)
    assert post({}).status_code == 503
    monkeypatch.setattr(settings, "embedding_configured", True, raising=False)
    monkeypatch.setattr(fa, "searcher", types.SimpleNamespace(local=types.SimpleNamespace(enabled=False)))
    assert post({}).status_code == 503                                                     # no table, nothing to fill


# ---- the status route the helper script reads

def test_the_table_counts_chunks_per_domain_in_one_scan(conf):
    store = filled_store([make_doc(1, 0), make_doc(1, 1), make_doc(2, 0, domain="themuse.com")])
    assert store.counts_by_domain() == {"sujeeth.org": 2, "themuse.com": 1}


def test_the_status_route_reports_the_loaded_index_the_table_rows_and_the_queue(conf, monkeypatch):
    svc = tagged_service()
    monkeypatch.setattr(fa, "searcher", svc)
    queue = types.SimpleNamespace(get_queue_properties=lambda: types.SimpleNamespace(approximate_message_count=7))
    monkeypatch.setattr(fa, "queue_processor", types.SimpleNamespace(queue=queue))
    plain = json.loads(fa.local_index_status._function._func(request("GET", "local-index/status")).get_body())
    assert plain["enabled"] is True and "table_rows" not in plain and "writes" not in plain and "backend" not in plain
    counted = json.loads(fa.local_index_status._function._func(request("GET", "local-index/status", {"count": "true"})).get_body())
    assert counted["table_rows"] == 6 and counted["queue_messages"] == 7
    assert counted["table_by_domain"] == [{"domain": "sujeeth.org", "chunks": 3}, {"domain": "themuse.com", "chunks": 3}]


def test_the_status_route_survives_a_failed_count_and_a_missing_queue(conf, monkeypatch):
    svc = tagged_service()
    svc.local._store.chunks.list_entities = lambda **kw: (_ for _ in ()).throw(RuntimeError("storage down"))
    monkeypatch.setattr(fa, "searcher", svc)
    monkeypatch.setattr(fa, "queue_processor", None)
    body = json.loads(fa.local_index_status._function._func(request("GET", "local-index/status", {"count": "1"})).get_body())
    assert "storage down" in body["table_error"] and body["queue_messages"] is None and "table_rows" not in body


def test_the_status_route_says_so_when_there_is_no_table_connection(conf, monkeypatch):
    monkeypatch.setattr(fa, "searcher", types.SimpleNamespace(
        local=types.SimpleNamespace(enabled=False, describe=lambda: {"backend": "azure", "enabled": False})))
    reply = fa.local_index_status._function._func(request("GET", "local-index/status"))
    assert reply.status_code == 503
