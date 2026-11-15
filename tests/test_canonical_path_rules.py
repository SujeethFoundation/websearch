"""A page's canonical tag must not move it to a URL the crawl path rules refuse."""
import pytest

from services.config import settings
from services.page_worker import PageWorker


class FakeResponse:
    def __init__(self, status=200, body=b"", url="https://example.org/advice/a"):
        self.status_code, self.url, self._body, self.closed = status, url, body, False
        self.headers = {"content-type": "text/html"} if body else {}

    @property
    def text(self):
        return self._body.decode("utf-8", errors="replace")

    def iter_content(self, chunk_size=65536):
        yield self._body

    def raise_for_status(self):
        pass

    def close(self):
        self.closed = True


class FakeState:
    def __init__(self):
        self.manifest = []
        self.enqueued = []

    def get_job(self, job_id):
        return {"status": "running", "max_depth": 5, "allowed_concepts_json": "[]"}

    def page(self, job_id, url):
        return None

    def claim_page(self, job_id, url):
        return "token"

    def release_page_claim(self, *a, **k):
        pass

    def complete_page(self, *a, **k):
        return True

    def manifest_get(self, domain, url):
        return None

    def manifest_put(self, domain, url, **k):
        self.manifest.append(url)

    def enqueue_page(self, job_id, domain, url, depth):
        self.enqueued.append(url)
        return True


class FakeOntology:
    def crawl_manifest(self):
        return {"crawl_domains": [{"domain": "example.org",
                                   "crawl_policy": {"include_paths": ["/advice"], "exclude_paths": ["/advice/news"]}}]}


class FakeResolver:
    def tag_document(self, title, content, allowed):
        return ["c"]


class FakeSearch:
    def __init__(self):
        self.documents = []

    def delete_url(self, url):
        return 0

    def upsert_or_defer(self, documents, persist_blobs=True):
        self.documents.extend(documents)
        return len(documents), []


def crawl(canonical, links=""):
    head = f'<link rel="canonical" href="{canonical}">' if canonical else ""
    body = f"<html><head><title>T</title>{head}</head><body><p>{'word ' * 400}</p>{links}</body></html>"
    state, search = FakeState(), FakeSearch()
    worker = PageWorker(FakeOntology(), None, search, FakeResolver(), state)
    worker.safe_public_host = lambda host: True
    page = FakeResponse(body=body.encode())
    worker.safe_get = lambda url, domain, headers=None, stream=False: (
        FakeResponse(status=404, url=url) if url.endswith("/robots.txt") else page)
    worker.defer_indexing = lambda names: None
    worker.process({"job_id": "j", "domain": "example.org", "url": "https://example.org/advice/a", "depth": 1})
    return state, search


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    monkeypatch.setattr(settings, "crawl_delay", 0)


def stored_urls(search):
    return {d["url"] for d in search.documents}


def test_a_canonical_outside_the_allow_list_is_ignored():
    state, search = crawl("https://example.org/author/a")
    assert stored_urls(search) == {"https://example.org/advice/a"}
    assert state.manifest == ["https://example.org/advice/a"]


def test_a_canonical_under_an_excluded_path_is_ignored():
    _, search = crawl("https://example.org/advice/news/a")
    assert stored_urls(search) == {"https://example.org/advice/a"}


def test_a_canonical_the_rules_allow_still_wins():
    _, search = crawl("https://example.org/advice/b")
    assert stored_urls(search) == {"https://example.org/advice/b"}


def test_a_relative_canonical_outside_the_allow_list_is_ignored():
    _, search = crawl("/author/a")
    assert stored_urls(search) == {"https://example.org/advice/a"}


def test_a_page_with_no_canonical_keeps_its_own_url():
    _, search = crawl(None)
    assert stored_urls(search) == {"https://example.org/advice/a"}


def test_links_are_still_resolved_from_the_stored_url():
    state, _ = crawl("https://example.org/author/a", links='<a href="c">next</a>')
    assert "https://example.org/advice/c" in state.enqueued
