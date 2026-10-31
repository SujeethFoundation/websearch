from services.crawler import Crawler

def test_normalize_url_drops_fragment_and_tracking():
    value = Crawler._normalize_url("https://EXAMPLE.org/a#section?utm_source=x")
    assert value == "https://example.org/a"

def test_same_host_rejects_external_and_http():
    assert Crawler._same_host("https://example.org/a", "example.org")
    assert not Crawler._same_host("https://other.org/a", "example.org")
    assert not Crawler._same_host("http://example.org/a", "example.org")
