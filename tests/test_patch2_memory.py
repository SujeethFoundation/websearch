"""Patch 2: page and job rows are read a page at a time, old jobs are cleaned up, the ontology is cached,
long documents stop early, and the legacy crawler and routes are gone."""
import json
import types
from datetime import datetime, timedelta, timezone

import pytest

from services.config import settings
from services.crawl_state import CrawlState
from services.ontology_client import OntologyClient
from services.page_worker import PageWorker
from services.sitemap import collect_sitemap_urls


def iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


# ---------------------------------------------------------------- fake tables
class Table:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.queries = []
        self.deleted = []
        self.transactions = []

    def query_entities(self, query_filter=None, select=None, results_per_page=None):
        self.queries.append({"filter": query_filter, "select": select, "per_page": results_per_page})
        # a lazy pager, like the SDK's: nothing is read until it is iterated
        return (dict(r) for r in self.rows)

    def delete_entity(self, pk, rk):
        self.deleted.append((pk, rk))

    def submit_transaction(self, ops):
        assert len(ops) <= 100, "an Azure table transaction holds at most 100 operations"
        self.transactions.append(list(ops))

    def upsert_entity(self, entity, mode=None):
        self.deleted.append(("upsert", dict(entity)))


def make_state(jobs=None, pages=None, manifest=None):
    state = CrawlState.__new__(CrawlState)
    state.jobs, state.pages, state.manifest = Table(jobs), Table(pages), Table(manifest)
    return state


# ---------------------------------------------------------------- crawl_state
def test_page_rows_are_read_lazily_a_page_at_a_time_with_only_the_columns_asked_for():
    state = make_state(pages=[{"status": "processed"}] * 3)
    rows = state.iter_page_records("j1", select=["status"], where="status eq 'failed'")
    assert state.pages.queries[0]["per_page"] == 500 and state.pages.queries[0]["select"] == ["status"]
    assert "PartitionKey eq 'j1' and (status eq 'failed')" == state.pages.queries[0]["filter"]
    assert next(iter(rows)) == {"status": "processed"}


def test_page_records_limit_stops_reading():
    state = make_state(pages=[{"url": str(i)} for i in range(50)])
    assert len(state.page_records("j1", limit=7)) == 7


def test_reconcile_counts_pages_without_keeping_them(monkeypatch):
    pages = [{"status": "processed"}] * 4 + [{"status": "queued"}] * 2 + [{"status": "failed"}]
    state = make_state(pages=pages)
    state.get_job = lambda job_id: {"status": "running", "pending": 9}
    changes = []
    state._change = lambda job_id, **kw: changes.append(kw)
    result = state.reconcile_job("j1")
    assert result["page_count"] == 7 and result["expected_pending"] == 2
    assert state.pages.queries[0]["select"] == ["status"]
    assert changes[0]["pages_discovered"] == 7 and changes[0]["pages_failed"] == 1


def test_recovery_reads_only_pages_that_could_need_it():
    state = make_state(pages=[{"url": "https://a.org/1", "depth": 1, "status": "queued"}])
    state.get_job = lambda job_id: {"status": "running", "domain": "a.org"}
    state._requeue_if_stale = lambda *a, **k: True
    state._publish = lambda *a, **k: None
    state.reconcile_job = lambda job_id: None
    out = state.recover_job("j1")
    q = state.pages.queries[0]
    assert out["requeued"] == 1 and q["select"] == ["url", "depth", "status"]
    for status in ("queued", "processing", "publish_failed", "failed"):
        assert f"status eq '{status}'" in q["filter"]
    assert "processed" not in q["filter"] and "unchanged" not in q["filter"]


def test_open_domains_ignores_a_job_nobody_has_touched_for_days():
    state = make_state(jobs=[{"domain": "A.org", "status": "running"}])
    assert state.open_domains() == {"a.org"}
    flt = state.jobs.queries[0]["filter"]
    cutoff = flt.split("updated_utc ge '")[1].rstrip("'")
    age = datetime.now(timezone.utc) - datetime.fromisoformat(cutoff)
    assert abs(age - timedelta(days=settings.open_job_stale_days)) < timedelta(minutes=1)


def test_manifest_rows_are_read_a_page_at_a_time_and_a_returning_page_is_current_again():
    state = make_state()
    state.manifest_for_domain("a.org", select=["url"])
    assert state.manifest.queries[0]["per_page"] == 500
    state.manifest_put("a.org", "https://a.org/x", last_seen_job="j2")
    assert state.manifest.deleted[-1][1]["status"] == "current"
    state.manifest_mark_stale({"PartitionKey": "p", "RowKey": "r"})
    marked = state.manifest.deleted[-1][1]
    assert marked["status"] == "stale" and marked["RowKey"] == "r" and "stale_detected_utc" in marked


def job_row(job_id, domain, created_days, updated_days):
    return {"RowKey": job_id, "domain": domain, "created_utc": iso(created_days), "updated_utc": iso(updated_days)}


def test_old_jobs_and_their_page_rows_are_deleted_but_each_domains_newest_job_stays():
    jobs = [job_row("a-old", "a.org", 90, 90), job_row("a-mid", "a.org", 60, 60), job_row("a-new", "a.org", 40, 40),
            job_row("b-only", "b.org", 80, 80), job_row("c-recent", "c.org", 5, 5), job_row("c-older", "c.org", 50, 50)]
    state = make_state(jobs=jobs, pages=[{"PartitionKey": "x", "RowKey": str(i)} for i in range(230)])
    result = state.purge_old_jobs(days=30)
    gone = {rk for _, rk in state.jobs.deleted}
    assert gone == {"a-old", "a-mid", "c-older"}            # a-new, b-only and c-recent are each domain's newest
    assert result["jobs_deleted"] == 3 and result["page_rows_deleted"] == 3 * 230
    assert all(len(t) <= 100 for t in state.pages.transactions) and len(state.pages.transactions) == 3 * 3


def test_cleanup_deletes_at_most_max_jobs_per_run_oldest_first():
    jobs = [job_row(f"j{i}", f"d{i}.org", 100 - i, 100 - i) for i in range(5)] + [job_row("keep", "z.org", 1, 1)]
    state = make_state(jobs=jobs)
    result = state.purge_old_jobs(days=30, max_jobs=2)
    # every domain has one job, so each domain's newest is kept and nothing is eligible
    assert result["jobs_deleted"] == 0
    jobs += [job_row(f"extra{i}", "d0.org", 200 + i, 200 + i) for i in range(4)]
    state = make_state(jobs=jobs)
    result = state.purge_old_jobs(days=30, max_jobs=2)
    assert [rk for _, rk in state.jobs.deleted] == ["extra3", "extra2"] and result["eligible_remaining"] == 2


def test_a_job_that_is_still_moving_is_not_deleted_however_old_it_started():
    state = make_state(jobs=[job_row("old-but-busy", "a.org", 90, 0), job_row("newest", "a.org", 1, 1)])
    assert state.purge_old_jobs(days=30)["jobs_deleted"] == 0


# ---------------------------------------------------------------- page worker
class FakeSearch:
    def __init__(self):
        self.deleted = []

    def delete_url(self, url):
        self.deleted.append(url)
        return 2


class FinalizeState:
    def __init__(self, rows):
        self.rows, self.select, self.marked, self.changes = rows, None, [], []

    def get_job(self, job_id):
        return {"status": "completed", "pages_failed": 0, "pages_skipped": 0, "pages_processed": 3, "page_limit": 100}

    def manifest_for_domain(self, domain, select=None):
        self.select = select
        return iter(self.rows)

    def manifest_mark_stale(self, record):
        self.marked.append(record["RowKey"])

    def _change(self, job_id, **kw):
        self.changes.append(kw)


class FakeOntology:
    def crawl_manifest(self):
        return {"crawl_domains": [{"domain": "example.org"}]}


def test_stale_cleanup_skips_current_and_already_stale_rows_and_reads_five_columns():
    rows = [
        {"RowKey": "seen", "url": "https://example.org/seen", "last_seen_job": "j", "status": "current"},
        {"RowKey": "gone", "url": "https://example.org/gone", "last_seen_job": "old", "status": "current"},
        {"RowKey": "was-stale", "url": "https://example.org/was", "last_seen_job": "older", "status": "stale"},
        {"RowKey": "legacy", "url": "https://example.org/legacy", "last_seen_job": "old"},  # written before status existed
    ]
    state, search = FinalizeState(rows), FakeSearch()
    worker = PageWorker(FakeOntology(), None, search, None, state)
    worker._finalize_if_safe("j", "example.org")
    assert search.deleted == ["https://example.org/gone", "https://example.org/legacy"]
    assert state.marked == ["gone", "legacy"]
    assert set(state.select) == {"PartitionKey", "RowKey", "url", "last_seen_job", "status"}
    assert state.changes[0]["stale_pages_removed"] == 2 and state.changes[0]["stale_chunks_removed"] == 4


def test_a_failure_to_mark_a_row_stale_does_not_stop_the_cleanup():
    rows = [{"RowKey": f"r{i}", "url": f"https://example.org/{i}", "last_seen_job": "old"} for i in range(3)]
    state, search = FinalizeState(rows), FakeSearch()
    state.manifest_mark_stale = lambda record: (_ for _ in ()).throw(RuntimeError("table busy"))
    worker = PageWorker(FakeOntology(), None, search, None, state)
    worker._finalize_if_safe("j", "example.org")
    assert len(search.deleted) == 3


def test_a_long_document_stops_being_read_well_past_what_the_chunk_cap_keeps(monkeypatch):
    monkeypatch.setattr(settings, "max_chunks_per_page", 2)
    monkeypatch.setattr(settings, "chunk_chars", 100)
    read = []

    def pages():
        for i in range(1000):
            read.append(i)
            yield "x" * 50

    text = PageWorker._join_capped(pages())
    assert len(read) < 12 and len(text) >= 2 * 100            # stopped long before 1000 pages
    monkeypatch.setattr(settings, "max_chunks_per_page", 0)  # 0 means no limit
    read.clear()
    PageWorker._join_capped(pages())
    assert len(read) == 1000


# ---------------------------------------------------------------- sitemap
def test_duplicate_sitemap_urls_do_not_use_up_the_page_limit(monkeypatch):
    monkeypatch.setattr(settings, "max_pages", 3)
    ns = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
    body = (f"<urlset {ns}>" + "<url><loc>https://example.org/a</loc></url>" * 5
            + "<url><loc>https://example.org/b</loc></url><url><loc>https://example.org/c</loc></url></urlset>").encode()

    class R:
        status_code, headers, closed = 200, {}, False
        def close(self): pass
        def iter_content(self, chunk_size=65536): yield body

    session = types.SimpleNamespace(get=lambda url, **kw: R() if url.endswith("/sitemap.xml") and "www" not in url
                                    else types.SimpleNamespace(status_code=404, close=lambda: None, headers={}))
    found, _, _ = collect_sitemap_urls(session, "example.org", [], is_public=lambda h: True)
    assert found == ["https://example.org/a", "https://example.org/b", "https://example.org/c"]


# ---------------------------------------------------------------- ontology cache
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "ontology_api_url", "https://onto.example")
    client = OntologyClient()
    client.calls = []

    def get(path):
        client.calls.append(path)
        if client.fail:
            raise RuntimeError("ontology down")
        return {"crawl_domains": [{"domain": "a.org"}], "associated_concepts": ["c1", {"id": "c2"}]}

    client.fail = False
    client.get = get
    return client


def test_a_successful_manifest_is_reused(client):
    client.crawl_manifest(); client.crawl_manifest(); client.search_manifest(); client.search_manifest()
    assert client.calls == ["/api/ontology/crawl-manifest", "/api/ontology/search-manifest"]


def test_domain_concepts_are_reused_and_each_domain_is_cached_separately(client):
    assert client.domain_concepts("a.org") == ["c1", "c2"]
    client.domain_concepts("a.org"); client.domain_concepts("B.org")
    assert len(client.calls) == 2


def test_a_failure_is_raised_and_never_cached(client):
    client.fail = True
    with pytest.raises(RuntimeError):
        client.crawl_manifest()
    client.fail = False
    assert client.crawl_manifest()["crawl_domains"]
    assert len(client.calls) == 2


def test_the_cache_expires_and_can_be_switched_off(client, monkeypatch):
    client.crawl_manifest()
    key = "crawl-manifest"
    client._cache[key] = (client._cache[key][0] - 10_000, client._cache[key][1])
    client.crawl_manifest()
    assert len(client.calls) == 2
    monkeypatch.setattr(settings, "ontology_cache_seconds", 0)
    client.crawl_manifest(); client.crawl_manifest()
    assert len(client.calls) == 4


# ---------------------------------------------------------------- function_app
@pytest.fixture
def fa(monkeypatch):
    import function_app
    return function_app


def call(function, **kw):
    """Run a decorated HTTP route and return (status, json)."""
    req = types.SimpleNamespace(params=kw.get("params", {}), route_params=kw.get("route_params", {}),
                                get_json=lambda: kw.get("body", {}))
    resp = function._function.get_user_function()(req)
    return resp.status_code, json.loads(resp.get_body())


def test_the_legacy_routes_and_the_old_crawler_are_gone(fa):
    for name in ("refresh", "reindex", "enrich", "crawler", "Crawler"):
        assert not hasattr(fa, name), name
    import importlib.util
    assert importlib.util.find_spec("services.crawler") is None


def test_settings_reach_the_search_code_through_one_place(fa):
    assert fa.SPOTLIGHT_SLOTS == settings.spotlight_slots and fa.BEST_BETS_MAX == settings.best_bets_max
    assert fa._weekly_allowlist() is settings.weekly_crawl_domains
    assert fa._domains({"domains": [" A.org ", "", "b.org"]}) == ["a.org", "b.org"]


class PagesState:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def get_job(self, job_id):
        return {"job_id": job_id} if job_id != "missing" else None

    def iter_page_records(self, job_id, select=None, where=None):
        self.calls.append(("iter", select, where))
        return iter(self.rows)

    def page_records(self, job_id, select=None, where=None, limit=None):
        self.calls.append(("page_records", select, where, limit))
        return list(self.rows)[:limit] if limit else list(self.rows)


def test_pages_route_can_summarise_without_returning_every_row(fa, monkeypatch):
    rows = ([{"status": "skipped", "reason": "http_404"}] * 3 + [{"status": "processed"}] * 5
            + [{"status": "skipped", "reason": "robots_disallowed"}])
    monkeypatch.setattr(fa, "crawl_state", PagesState(rows))
    status, body = call(fa.crawl_pages, route_params={"job_id": "j"}, params={"summary": "true"})
    assert status == 200 and body["count"] == 9 and "pages" not in body
    assert body["groups"][0] == {"status": "processed", "reason": "", "count": 5}
    assert {"status": "skipped", "reason": "http_404", "count": 3} in body["groups"]


def test_pages_route_filters_and_limits_in_the_table_and_checks_its_input(fa, monkeypatch):
    state = PagesState([{"url": "u", "created_utc": "1"}])
    monkeypatch.setattr(fa, "crawl_state", state)
    status, body = call(fa.crawl_pages, route_params={"job_id": "j"}, params={"status": "FAILED", "limit": "50"})
    assert status == 200 and body["limit"] == 50
    kind, select, where, limit = state.calls[-1]
    assert where == "status eq 'failed'" and limit == 50 and "lease_token" not in select
    assert call(fa.crawl_pages, route_params={"job_id": "j"}, params={"status": "x' or 1 eq 1"})[0] == 400
    assert call(fa.crawl_pages, route_params={"job_id": "j"}, params={"limit": "many"})[0] == 400
    assert call(fa.crawl_pages, route_params={"job_id": "missing"})[0] == 404
    status, body = call(fa.crawl_pages, route_params={"job_id": "j"}, params={"limit": "999999"})
    assert state.calls[-1][3] == 10000


def test_domain_stats_never_reads_page_rows(fa, monkeypatch):
    class JobsOnly:
        def list_jobs(self, limit):
            return [
                {"job_id": "n", "domain": "A.org", "status": "completed", "created_utc": "2", "updated_utc": "3",
                 "pages_discovered": 10, "pages_processed": 8, "pages_skipped": 1, "pages_failed": 1,
                 "chunks_indexed": 40, "pending": 0},
                {"job_id": "o", "domain": "a.org", "status": "failed", "pages_discovered": 5, "pages_processed": 1},
                {"job_id": "x", "domain": "b.org", "status": "running", "pending": 4},
            ]

        def page_records(self, *a, **k):
            raise AssertionError("domain-stats must not read page rows")

        iter_page_records = page_records

    monkeypatch.setattr(fa, "crawl_state", JobsOnly())
    status, body = call(fa.crawl_domain_stats)
    a = next(d for d in body["domains"] if d["domain"] == "a.org")
    assert status == 200 and a["jobs"] == 2 and a["pages_discovered"] == 15 and a["pages_processed"] == 9
    assert a["latest_job"] == "n" and a["latest_job_counters"]["pages_discovered"] == 10
    assert a["job_statuses"] == {"completed": 1, "failed": 1}


def test_dashboard_does_not_count_every_blob(fa, monkeypatch):
    class Jobs:
        def list_jobs(self, limit):
            return [{"job_id": "j", "domain": "a.org", "status": "running"}]

    class Storage:
        mode = "azure_blob"

        def status(self, **kw):
            raise AssertionError("the dashboard must not list every blob")

    monkeypatch.setattr(fa, "crawl_state", Jobs())
    monkeypatch.setattr(fa, "storage", Storage())
    monkeypatch.setattr(fa, "searcher", types.SimpleNamespace(status=lambda: {"configured": True, "document_count": 7}))
    monkeypatch.setattr(fa, "ontology", types.SimpleNamespace(crawl_manifest=lambda: {"crawl_domains": [{"domain": "a.org"}]}))
    status, body = call(fa.crawl_dashboard)
    assert status == 200 and body["index"] == {"storage_mode": "azure_blob", "configured": True, "document_count": 7}
    assert body["domains"][0]["latest_job"]["job_id"] == "j"


def test_index_status_counts_blobs_only_when_asked(fa, monkeypatch):
    seen = []
    monkeypatch.setattr(fa, "storage", types.SimpleNamespace(status=lambda count_blobs=False: seen.append(count_blobs) or {}))
    monkeypatch.setattr(fa, "searcher", types.SimpleNamespace(status=lambda: {}))
    call(fa.index_status)
    call(fa.index_status, params={"count_blobs": "true"})
    assert seen == [False, True]


def test_cleanup_timer_respects_its_switch_and_reports(fa, monkeypatch):
    ran = []
    monkeypatch.setattr(fa, "crawl_state", types.SimpleNamespace(purge_old_jobs=lambda: ran.append(1) or {"jobs_deleted": 0}))
    run = fa.scheduled_job_cleanup._function.get_user_function()
    monkeypatch.setattr(settings, "job_cleanup_enabled", False)
    run(None)
    assert not ran
    monkeypatch.setattr(settings, "job_cleanup_enabled", True)
    run(None)
    assert ran == [1]
