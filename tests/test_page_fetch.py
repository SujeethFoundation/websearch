import pytest
from services import page_fetch
from services.page_fetch import fetch_page, PageFetchError

PAGE = ("<html><head><title>Hello</title><meta name='description' content='%s'></head><body><p>%s</p></body></html>"
        % ("A page about the Sujeeth Foundation and its mentorship program for students.", "Body text. " * 40))


class Resp:
    def __init__(self, status=200, text="", ctype="text/html; charset=utf-8", headers=None):
        self.status_code, self._t, self.headers, self.encoding = status, text, {"content-type": ctype, **(headers or {})}, "utf-8"
        self.text = text

    def iter_content(self, chunk_size=0):
        yield self._t.encode()

    def close(self):
        pass


class Session:
    def __init__(self, page, robots=Resp(200, "User-agent: *\nAllow: /", "text/plain")):
        self.headers, self.page, self.robots, self.calls = {}, page, robots, []

    def get(self, url, **kw):
        self.calls.append(url)
        return self.robots if url.endswith("/robots.txt") else self.page


def go(page, **kw):
    return fetch_page("https://example.org/a", session=Session(page, **kw), is_public=lambda h: True)


def test_crawl_returns_title_description_and_text():
    out = go(Resp(200, PAGE))
    assert out["title"] == "Hello" and out["description_source"] == "page_meta"
    assert out["description"].startswith("A page about the Sujeeth Foundation")


def test_blocked_site_is_reported_not_worked_around():
    with pytest.raises(PageFetchError, match="HTTP 403"):
        go(Resp(403, "denied"))


def test_bot_check_page_is_reported():
    with pytest.raises(PageFetchError, match="bot-check"):
        go(Resp(200, "<html><title>Just a moment</title><body>Please verify you are human</body></html>"))


def test_robots_disallow_stops_the_fetch():
    robots = Resp(200, "User-agent: *\nDisallow: /", "text/plain")
    s = Session(Resp(200, PAGE), robots)
    with pytest.raises(PageFetchError, match="robots"):
        fetch_page("https://example.org/a", session=s, is_public=lambda h: True)
    assert s.calls == ["https://example.org/robots.txt"]


def test_non_html_is_rejected():
    with pytest.raises(PageFetchError, match="HTML"):
        go(Resp(200, "%PDF", "application/pdf"))


def test_off_site_redirect_is_refused():
    with pytest.raises(PageFetchError, match="off the site"):
        go(Resp(302, "", headers={"Location": "https://other.com/x"}))
