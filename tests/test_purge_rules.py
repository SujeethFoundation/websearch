"""Purge and purge-preview honour both lists: exclude_paths (block-list) and include_paths (allow-list).

Run from the websearch repo root: python -m pytest -q tests
"""
import json
import types

from services import queue_worker as qw
from services import search_service as ss
from services.path_rules import PathPolicy


class FakeOntology:
    def __init__(self, rows):
        self.rows = rows

    def crawl_manifest(self):
        return {"crawl_domains": self.rows}


class Store:
    def __init__(self):
        self.blobs = {}

    def get(self, name):
        return json.loads(json.dumps(self.blobs[name])) if name in self.blobs else None

    def delete(self, name):
        self.blobs.pop(name, None)

    def list_blob_name_page(self, prefix, continuation_token=None, page_size=250):
        return sorted(n for n in self.blobs if n.startswith(prefix))[:page_size], None


class Search:
    vector_key = staticmethod(ss.SearchService.vector_key)

    def __init__(self):
        self.deleted = []

    def delete_ids(self, ids):
        self.deleted.extend(ids)
        return len(ids)


class State:
    def __init__(self):
        self.manifest_deleted = []

    def manifest_delete(self, domain, url):
        self.manifest_deleted.append((domain, url))


ROWS = [
    {"domain": "indeed.com", "crawl_policy": {
        "include_paths": ["/career-advice"], "exclude_paths": ["/career-advice/news"]}},
    {"domain": "tealhq.com", "crawl_policy": {"exclude_paths": ["/job"]}},
    {"domain": "sujeeth.org", "crawl_policy": {"mode": "domain"}},
]

CHUNKS = {
    "i-home": ("indeed.com", "https://www.indeed.com/"),
    "i-guide": ("indeed.com", "https://www.indeed.com/career-advice/interviewing"),
    "i-news": ("indeed.com", "https://www.indeed.com/career-advice/news/story"),
    "i-jobs": ("indeed.com", "https://www.indeed.com/jobs?q=nurse"),
    "i-cmp": ("indeed.com", "https://www.indeed.com/cmp/Indeed/about"),
    "t-job": ("tealhq.com", "https://www.tealhq.com/job/x"),
    "t-ok": ("tealhq.com", "https://www.tealhq.com/resume-guide"),
    "s-any": ("sujeeth.org", "https://www.sujeeth.org/jobs"),
}


def build():
    store = Store()
    for doc_id, (domain, url) in CHUNKS.items():
        store.blobs[f"documents/{doc_id}-0.json"] = {"id": f"{doc_id}-0", "domain": domain, "url": url}
        store.blobs[f"vectors/{doc_id}-0.json"] = {"v": "x"}
    proc = object.__new__(qw.QueueProcessor)
    proc.storage, proc.search, proc.state = store, Search(), State()
    proc.worker = types.SimpleNamespace(path_policy=PathPolicy(FakeOntology(ROWS)))
    return proc, store


def names(store):
    return sorted(n for n in store.blobs if n.startswith("documents/"))


def test_purge_removes_chunks_outside_the_allow_list_and_under_the_block_list():
    proc, store = build()
    out = proc.purge_blobs(names(store))
    assert out["removed"] == 4
    assert sorted(proc.search.deleted) == ["i-cmp-0", "i-jobs-0", "i-news-0", "t-job-0"]
    left = sorted(n.split("/")[1][:-5] for n in store.blobs if n.startswith("documents/"))
    assert left == ["i-guide-0", "i-home-0", "s-any-0", "t-ok-0"]  # homepage and in-scope pages stay


def test_purge_removes_the_vector_blob_and_manifest_row_too():
    proc, store = build()
    proc.purge_blobs(names(store))
    assert "vectors/i-jobs-0.json" not in store.blobs and "vectors/i-guide-0.json" in store.blobs
    assert ("indeed.com", "https://www.indeed.com/jobs?q=nurse") in proc.state.manifest_deleted
    assert ("sujeeth.org", "https://www.sujeeth.org/jobs") not in proc.state.manifest_deleted


def test_purge_can_be_limited_to_one_domain():
    proc, store = build()
    out = proc.purge_blobs(names(store), ["tealhq.com"])
    assert out["removed"] == 1 and proc.search.deleted == ["t-job-0"]


def test_a_domain_with_no_rules_is_never_purged():
    proc, store = build()
    proc.purge_blobs(names(store))
    assert "documents/s-any-0.json" in store.blobs


def test_a_domain_with_only_a_block_list_still_purges_as_before():
    proc, store = build()
    out = proc.purge_blobs(names(store), ["tealhq.com"])
    assert out["removed"] == 1
    assert "documents/t-ok-0.json" in store.blobs


def test_preview_counts_without_deleting_and_shows_both_lists():
    proc, store = build()
    before = dict(store.blobs)
    out = proc.purge_preview()
    assert out["would_remove"] == 4
    assert out["includes"] == {"indeed.com": ["/career-advice"]}
    assert out["rules"]["indeed.com"] == ["/career-advice/news"]
    assert out["by_section"] == {"indeed.com/jobs": 1, "indeed.com/cmp": 1, "indeed.com/career-advice": 1, "tealhq.com/job": 1}
    assert proc.search.deleted == [] and store.blobs == before
