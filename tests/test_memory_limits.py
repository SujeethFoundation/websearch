"""Memory guards: capped downloads, streamed sitemaps, grouped chunk indexing."""
import tracemalloc
import xml.etree.ElementTree as ET

import pytest

from services.config import settings
from services.crawler import Crawler
from services.limits import TooLarge, chunk_groups, iter_sitemap_locs, read_capped
from services.page_worker import PageWorker


# ---------------------------------------------------------------- helpers
class FakeResponse:
    def __init__(self, status=200, headers=None, body=b"", url="https://example.org/a", block=65536):
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self.url = url
        self.closed = False
        self.read_called = False
        self._block = block

    @property
    def text(self):
        return self._body.decode("utf-8", errors="replace")

    def iter_content(self, chunk_size=65536):
        self.read_called = True
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start:start + chunk_size]

    def raise_for_status(self):
        pass

    def close(self):
        self.closed = True


class FakeState:
    def __init__(self):
        self.completed = []

    def get_job(self, job_id):
        return {"status": "running", "max_depth": 5, "allowed_concepts_json": "[]"}

    def page(self, job_id, url):
        return None

    def claim_page(self, job_id, url):
        return "token"

    def release_page_claim(self, *args, **kwargs):
        pass

    def complete_page(self, job_id, url, status="processed", page_fields=None, claim_token=None, **increments):
        self.completed.append({"status": status, "fields": page_fields or {}, **increments})
        return True

    def manifest_get(self, domain, url):
        return None

    def manifest_put(self, *args, **kwargs):
        pass

    def enqueue_page(self, *args, **kwargs):
        return True


class FakeOntology:
    def crawl_manifest(self):
        return {"crawl_domains": []}


class FakeResolver:
    def tag_document(self, title, content, allowed):
        return ["c"]


class FakeSearch:
    def __init__(self, busy=False):
        self.calls = []
        self.busy = busy

    def delete_url(self, url):
        return 0

    def upsert_or_defer(self, documents, persist_blobs=True):
        self.calls.append(len(documents))
        names = ["documents/" + d["id"] + ".json" for d in documents]
        return (0, names) if self.busy else (len(documents), [])


def make_worker(page, busy=False):
    state, search = FakeState(), FakeSearch(busy=busy)
    worker = PageWorker(FakeOntology(), None, search, FakeResolver(), state)
    worker.safe_public_host = lambda host: True
    worker.safe_get = lambda url, domain, headers=None, stream=False: (
        FakeResponse(status=404, url=url) if url.endswith("/robots.txt") else page
    )
    worker.deferred = []
    worker.defer_indexing = lambda names: worker.deferred.append(list(names))
    return worker, state, search


def run(worker):
    return worker.process({"job_id": "j", "domain": "example.org", "url": "https://example.org/a", "depth": 1})


def html_page(chars, headers=None):
    body = "<html><head><title>T</title></head><body><p>" + ("word " * (chars // 5)) + "</p></body></html>"
    return FakeResponse(headers={"content-type": "text/html", **(headers or {})}, body=body.encode())


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    monkeypatch.setattr(settings, "crawl_delay", 0)


# ---------------------------------------------------------------- limits
def test_read_capped_returns_the_body_when_it_fits():
    assert bytes(read_capped(FakeResponse(body=b"x" * 1000), 1000)) == b"x" * 1000


def test_read_capped_stops_a_body_that_passes_the_cap():
    response = FakeResponse(body=b"x" * 5000)
    with pytest.raises(TooLarge):
        read_capped(response, 1000, chunk_size=100)
    assert response.closed


def test_read_capped_refuses_a_declared_length_before_reading():
    response = FakeResponse(headers={"Content-Length": "5000"}, body=b"x" * 5000)
    with pytest.raises(TooLarge):
        read_capped(response, 1000)
    assert not response.read_called and response.closed


def test_read_capped_ignores_a_bad_content_length():
    response = FakeResponse(headers={"Content-Length": "abc"}, body=b"ok")
    assert bytes(read_capped(response, 10)) == b"ok"


def test_chunk_groups_splits_into_groups_and_keeps_every_chunk():
    groups = list(chunk_groups("a" * 25, 2, group_size=4))
    assert [len(g) for g in groups] == [4, 4, 4, 1]
    assert [i for g in groups for i, _ in g] == list(range(13))
    assert "".join(c for g in groups for _, c in g) == "a" * 25


def test_chunk_groups_stops_at_the_chunk_cap_and_zero_means_no_cap():
    assert sum(len(g) for g in chunk_groups("a" * 100, 2, max_chunks=7, group_size=3)) == 7
    assert sum(len(g) for g in chunk_groups("a" * 100, 2, max_chunks=0, group_size=3)) == 50
    assert list(chunk_groups("", 2)) == []


URLSET = (b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
          b"<url><loc>https://example.org/a</loc><lastmod>2026-01-01</lastmod></url>"
          b"<url><loc> https://example.org/b </loc></url></urlset>")
INDEX = (b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
         b"<sitemap><loc>https://example.org/s1.xml</loc></sitemap></sitemapindex>")


def test_iter_sitemap_locs_reads_urlsets_and_indexes_like_a_full_parse():
    assert list(iter_sitemap_locs(URLSET)) == [("urlset", "https://example.org/a"), ("urlset", "https://example.org/b")]
    assert list(iter_sitemap_locs(INDEX)) == [("sitemapindex", "https://example.org/s1.xml")]
    full = [e.text.strip() for e in ET.fromstring(URLSET).iter() if e.tag.lower().endswith("loc")]
    assert [t for _, t in iter_sitemap_locs(URLSET)] == full


def test_iter_sitemap_locs_keeps_what_it_read_before_bad_xml():
    seen = []
    with pytest.raises(ET.ParseError):
        for item in iter_sitemap_locs(URLSET[:-12] + b"<broken"):
            seen.append(item)
    assert seen and seen[0][1] == "https://example.org/a"


def test_streaming_a_large_sitemap_uses_far_less_memory_than_a_full_parse():
    body = "".join(f"<url><loc>https://example.org/p/{i}-slug-slug-slug</loc><lastmod>2026-01-01</lastmod></url>"
                   for i in range(40000))
    data = ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + body + "</urlset>").encode()

    def peak(fn):
        tracemalloc.start()
        fn()
        _, high = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return high

    def full():
        [e.text for e in ET.fromstring(data).iter() if e.tag.lower().endswith("loc")]

    def streamed():
        for _ in iter_sitemap_locs(data):
            pass

    assert peak(streamed) < peak(full) / 4


# ---------------------------------------------------------------- crawler sitemaps
def sitemap_crawler(monkeypatch, pages):
    crawler = Crawler(None, None, None, None)
    monkeypatch.setattr(Crawler, "_public_host", staticmethod(lambda host: True))
    crawler.session.get = lambda url, **kw: pages.get(url, FakeResponse(status=404, url=url))
    return crawler


def test_sitemap_index_is_followed_and_urls_are_collected(monkeypatch):
    pages = {
        "https://example.org/sitemap.xml": FakeResponse(body=INDEX),
        "https://example.org/s1.xml": FakeResponse(body=URLSET),
    }
    found, visited, metadata, failures = sitemap_crawler(monkeypatch, pages)._sitemap_urls("example.org", [])
    assert found == ["https://example.org/a", "https://example.org/b"]
    assert metadata["https://example.org/a"] == {"sitemap": "https://example.org/s1.xml"}
    assert visited >= 2


def test_an_oversized_sitemap_is_reported_and_not_parsed(monkeypatch):
    monkeypatch.setattr(settings, "max_sitemap_bytes", 100)
    pages = {"https://example.org/sitemap.xml": FakeResponse(body=URLSET + b" " * 500)}
    found, _, _, failures = sitemap_crawler(monkeypatch, pages)._sitemap_urls("example.org", [])
    assert found == []
    assert {"url": "https://example.org/sitemap.xml", "error": "sitemap_too_large"} in failures


def test_sitemap_collection_stops_at_the_url_limit(monkeypatch):
    monkeypatch.setattr(settings, "max_sitemap_urls", 1)
    pages = {"https://example.org/sitemap.xml": FakeResponse(body=URLSET)}
    found, _, _, _ = sitemap_crawler(monkeypatch, pages)._sitemap_urls("example.org", [])
    assert found == ["https://example.org/a"]


# ---------------------------------------------------------------- page worker
def test_a_long_page_is_embedded_and_stored_in_groups():
    worker, state, search = make_worker(html_page(1800 * 120 - 100))
    run(worker)
    assert search.calls == [50, 50, 20]
    done = state.completed[-1]
    assert done["status"] == "processed" and done["chunks_indexed"] == 120
    assert "truncated_at_chunk_cap" not in done["fields"]


def test_a_page_past_the_chunk_cap_is_cut_and_flagged(monkeypatch):
    monkeypatch.setattr(settings, "max_chunks_per_page", 3)
    worker, state, search = make_worker(html_page(1800 * 10))
    run(worker)
    assert sum(search.calls) == 3
    done = state.completed[-1]
    assert done["chunks_indexed"] == 3 and done["fields"]["truncated_at_chunk_cap"] is True


def test_a_busy_index_defers_every_group_in_one_task():
    worker, state, search = make_worker(html_page(1800 * 120 - 100), busy=True)
    run(worker)
    assert len(worker.deferred) == 1 and len(worker.deferred[0]) == 120
    assert state.completed[-1]["fields"]["index_deferred"] is True


def test_a_declared_length_over_the_html_cap_is_skipped_without_reading(monkeypatch):
    monkeypatch.setattr(settings, "max_html_bytes", 1000)
    page = html_page(500, headers={"Content-Length": "999999"})
    worker, state, _ = make_worker(page)
    run(worker)
    assert state.completed[-1]["status"] == "skipped" and state.completed[-1]["fields"]["reason"] == "page_too_large"
    assert not page.read_called


def test_a_body_over_the_html_cap_is_skipped(monkeypatch):
    monkeypatch.setattr(settings, "max_html_bytes", 1000)
    worker, state, _ = make_worker(html_page(5000))
    run(worker)
    assert state.completed[-1]["fields"]["reason"] == "page_too_large"


def test_an_unsupported_file_type_is_skipped_without_downloading_it():
    page = FakeResponse(headers={"content-type": "image/png"}, body=b"\x89PNG" * 1000)
    worker, state, _ = make_worker(page)
    run(worker)
    assert state.completed[-1]["fields"]["reason"] == "unsupported_content_type"
    assert not page.read_called and page.closed


def test_html_and_document_caps_are_never_above_the_overall_page_cap():
    assert settings.max_html_bytes <= settings.max_page_bytes
    assert settings.max_doc_bytes <= settings.max_page_bytes
