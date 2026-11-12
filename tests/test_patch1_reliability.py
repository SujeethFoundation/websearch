"""Patch 1: job_id on job rows, one job-start path, deleted pages leave the index, robots cache, missing-only reconcile."""
import time
import types

import pytest

from services.config import settings
from services.crawl_state import CrawlState
from services.page_worker import PageWorker
from services.search_service import SearchService


class FakeResponse:
    def __init__(self, status=200, text="", url="https://example.org/a"):
        self.status_code, self.text, self.url, self.headers, self.closed = status, text, url, {}, False

    def close(self):
        self.closed = True


# ---------------------------------------------------------------- crawl_state
class FakeJobsTable:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def query_entities(self, flt, select=None):
        self.calls.append((flt, select))
        return [dict(r) for r in self.rows]

    def get_entity(self, pk, rk):
        return {"PartitionKey": pk, "RowKey": rk, "status": "running"}


def make_state(rows):
    state = CrawlState.__new__(CrawlState)
    state.jobs = FakeJobsTable(rows)
    return state


def test_job_rows_carry_job_id_so_the_recovery_timer_can_read_it():
    state = make_state([{"RowKey": "j1", "created_utc": "2026-11-01T00:00:00+00:00"},
                        {"RowKey": "j2", "created_utc": "2026-11-05T00:00:00+00:00"}])
    assert [j["job_id"] for j in state.list_jobs(10)] == ["j2", "j1"]
    assert all(j["job_id"] == j["RowKey"] for j in state.list_jobs_since(30))
    assert state.get_job("j9")["job_id"] == "j9"


def test_open_domains_is_filtered_by_the_table_and_reads_two_columns():
    state = make_state([{"RowKey": "j1", "domain": "Sujeeth.org", "status": "running"}])
    assert state.open_domains() == {"sujeeth.org"}
    flt, select = state.jobs.calls[0]
    assert "status ne 'completed'" in flt and "status ne 'cancelled'" in flt
    assert select == ["domain", "status"]


def test_list_jobs_since_filters_by_creation_date_in_the_table():
    state = make_state([])
    state.list_jobs_since(30)
    assert "created_utc ge '" in state.jobs.calls[0][0]


# ---------------------------------------------------------------- function_app job start
@pytest.fixture
def fa(monkeypatch):
    import function_app
    created = []

    class State:
        def __init__(self, open_domains=()):
            self.open = set(open_domains)
            self.failed = []
            self.enqueue_ok = True

        def open_domains(self):
            return set(self.open)

        def create_job(self, domain, cap, depth, concepts):
            created.append(domain)
            return "job-" + domain

        def enqueue_pages(self, job_id, domain, items):
            return 1 if self.enqueue_ok else 0

        def fail_job(self, job_id, error):
            self.failed.append((job_id, error))

    onto = types.SimpleNamespace(
        crawl_manifest=lambda: {"crawl_domains": [{"domain": "a.org"}, {"domain": "b.org"}]},
        domain_concepts=lambda d: ["c"])
    monkeypatch.setattr(function_app, "ontology", onto)
    monkeypatch.setattr(function_app, "State", State, raising=False)
    function_app._created = created
    return function_app


def test_start_crawls_skips_a_domain_with_an_open_job(fa, monkeypatch):
    state = fa.State(open_domains={"a.org"})
    monkeypatch.setattr(fa, "crawl_state", state)
    created, skipped = fa._start_crawls("Test", lambda d: True)
    assert [c["domain"] for c in created] == ["b.org"]
    assert [s["domain"] for s in skipped] == ["a.org"]


def test_start_crawls_never_starts_two_jobs_for_one_domain_in_one_call(fa, monkeypatch):
    state = fa.State()
    monkeypatch.setattr(fa, "crawl_state", state)
    entries = [{"domain": "a.org"}, {"domain": "A.org"}]
    created, skipped = fa._start_crawls("Test", lambda d: True, entries)
    assert len(created) == 1 and len(skipped) == 1


def test_start_crawls_fails_the_job_if_the_homepage_cannot_be_queued(fa, monkeypatch):
    state = fa.State()
    state.enqueue_ok = False
    monkeypatch.setattr(fa, "crawl_state", state)
    created, skipped = fa._start_crawls("Test", lambda d: d == "a.org")
    assert not created and state.failed[0][0] == "job-a.org" and skipped[0]["job_id"] == "job-a.org"


# ---------------------------------------------------------------- page worker
class FakeOntology:
    def crawl_manifest(self):
        return {"crawl_domains": [{"domain": "example.org"}]}


class FakeState:
    def __init__(self, previous=None):
        self.previous, self.completed, self.manifest_deleted = previous, [], []

    def get_job(self, job_id):
        return {"status": "running", "max_depth": 5, "allowed_concepts_json": "[]"}

    def page(self, job_id, url):
        return None

    def claim_page(self, job_id, url):
        return "token"

    def release_page_claim(self, *a, **k):
        pass

    def complete_page(self, job_id, url, status="processed", page_fields=None, claim_token=None, **increments):
        self.completed.append({"status": status, "fields": page_fields or {}, **increments})
        return True

    def manifest_get(self, domain, url):
        return self.previous

    def manifest_delete(self, domain, url):
        self.manifest_deleted.append((domain, url))

    def manifest_put(self, *a, **k):
        pass

    def enqueue_page(self, *a, **k):
        return True


class FakeSearch:
    def __init__(self):
        self.deleted = []

    def delete_url(self, url):
        self.deleted.append(url)
        return 3

    def delete_page(self, url, chunk_count=None):       # what the page worker calls (the real one deletes by id when it knows the count)
        return self.delete_url(url)


def make_worker(page_status, previous=None, robots=None):
    state, search = FakeState(previous), FakeSearch()
    worker = PageWorker(FakeOntology(), None, search, None, state)
    worker.safe_public_host = lambda host: True
    worker.fetches = []

    def safe_get(url, domain, headers=None, stream=False):
        worker.fetches.append(url)
        if url.endswith("/robots.txt"):
            return robots() if robots else FakeResponse(404, url=url)
        return FakeResponse(page_status, url=url)

    worker.safe_get = safe_get
    return worker, state, search


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    monkeypatch.setattr(settings, "crawl_delay", 0)


def run(worker, url="https://example.org/a", depth=1):
    return worker.process({"job_id": "j", "domain": "example.org", "url": url, "depth": depth})


@pytest.mark.parametrize("status", [404, 410])
def test_a_page_we_had_indexed_that_is_now_gone_is_removed_and_not_counted_as_skipped(status):
    worker, state, search = make_worker(status, previous={"content_hash": "x"})
    run(worker)
    assert search.deleted == ["https://example.org/a"]
    assert state.manifest_deleted == [("example.org", "https://example.org/a")]
    assert state.completed[-1]["status"] == "skipped" and "pages_skipped" not in state.completed[-1]


def test_a_first_time_404_is_still_counted_as_skipped_and_deletes_nothing():
    worker, state, search = make_worker(404, previous=None)
    run(worker)
    assert not search.deleted and state.completed[-1]["pages_skipped"] == 1


def test_a_403_is_skipped_and_never_deleted():
    worker, state, search = make_worker(403, previous={"content_hash": "x"})
    run(worker)
    assert not search.deleted and state.completed[-1]["pages_skipped"] == 1


def test_robots_txt_is_fetched_once_per_host_not_once_per_page():
    worker, state, search = make_worker(404)
    for n in range(5):
        run(worker, url=f"https://example.org/p{n}")
    assert sum(f.endswith("/robots.txt") for f in worker.fetches) == 1


def test_robots_cache_expires():
    worker, *_ = make_worker(404)
    run(worker, url="https://example.org/p1")
    key = next(iter(worker._robots_cache))
    expires, parser, text = worker._robots_cache[key]
    worker._robots_cache[key] = (time.monotonic() - 1, parser, text)
    run(worker, url="https://example.org/p2")
    assert sum(f.endswith("/robots.txt") for f in worker.fetches) == 2


def test_a_robots_disallow_still_blocks_the_page():
    worker, state, search = make_worker(200, robots=lambda: FakeResponse(200, "User-agent: *\nDisallow: /private\n"))
    run(worker, url="https://example.org/private/x")
    assert state.completed[-1]["fields"]["reason"] == "robots_disallowed"
    assert not any(f.endswith("/private/x") for f in worker.fetches)


def test_robots_401_or_403_denies_the_page():
    worker, state, _ = make_worker(200, robots=lambda: FakeResponse(403))
    run(worker)
    assert state.completed[-1]["fields"]["reason"] == "robots_denied"


def test_robots_5xx_raises_so_the_queue_retries_and_is_not_cached():
    worker, state, _ = make_worker(200, robots=lambda: FakeResponse(503))
    with pytest.raises(RuntimeError):
        run(worker)
    assert not worker._robots_cache


def test_sitemap_seeds_still_come_from_the_cached_robots_text(monkeypatch):
    seen = {}

    def fake_collect(session, domain, seeds, **kwargs):
        seen["seeds"] = seeds
        return [], 0, []

    monkeypatch.setattr("services.page_worker.collect_sitemap_urls", fake_collect)
    worker, state, _ = make_worker(404, robots=lambda: FakeResponse(200, "Sitemap: https://example.org/s.xml\n"))
    run(worker, url="https://example.org/", depth=0)
    assert seen["seeds"] == ["https://example.org/s.xml"]


# ---------------------------------------------------------------- nightly reconcile
def make_search(present, docs):
    s = SearchService.__new__(SearchService)
    s.downloaded = []

    def get(name):
        s.downloaded.append(name)
        return docs.get(name)

    s.storage = types.SimpleNamespace(get=get)
    s._existing_ids = lambda ids: {i for i in ids if i in present}
    s.upserted = []
    s.upsert_many = lambda d, persist_blobs=False: s.upserted.extend(x["id"] for x in d) or len(d)
    return s


def test_missing_mode_downloads_only_chunks_that_are_not_indexed():
    docs = {"documents/B-0.json": {"id": "B-0", "content": "x", "domain": "d"}}
    s = make_search(present={"A-0"}, docs=docs)
    out = s.index_blobs(["documents/A-0.json", "documents/B-0.json"], only_missing=True)
    assert s.downloaded == ["documents/B-0.json"]
    assert out == {"read": 2, "already_indexed": 1, "indexed": 1} and s.upserted == ["B-0"]


def test_all_mode_still_reindexes_everything():
    docs = {f"documents/{i}-0.json": {"id": f"{i}-0", "content": "x", "domain": "d"} for i in "AB"}
    s = make_search(present={"A-0"}, docs=docs)
    out = s.index_blobs(list(docs), only_missing=False)
    assert out["indexed"] == 2 and len(s.downloaded) == 2


def test_missing_mode_with_a_domain_filter_keeps_the_filter():
    docs = {"documents/A-0.json": {"id": "A-0", "content": "x", "domain": "keep.org"},
            "documents/B-0.json": {"id": "B-0", "content": "x", "domain": "skip.org"}}
    s = make_search(present=set(), docs=docs)
    s.index_blobs(list(docs), only_missing=True, domains=["keep.org"])
    assert s.upserted == ["A-0"]
