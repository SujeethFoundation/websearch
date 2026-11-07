from services.urlutil import canonical_host, host_variants, is_public_host, normalize_url, same_site


def test_normalize_url_drops_fragment_and_tracking():
    assert normalize_url("https://EXAMPLE.org/a#section?utm_source=x") == "https://example.org/a"


def test_normalize_url_keeps_real_query_parameters_and_collapses_slashes():
    assert normalize_url("https://example.org//a//b?id=3&utm_medium=x&fbclid=y&gclid=z") == "https://example.org/a/b?id=3"


def test_same_site_rejects_external_and_http():
    assert same_site("https://example.org/a", "example.org")
    assert not same_site("https://other.org/a", "example.org")
    assert not same_site("http://example.org/a", "example.org")


def test_apex_and_www_are_one_site_and_credentials_are_refused():
    assert same_site("https://www.example.org/a", "example.org")
    assert same_site("https://example.org/a", "www.example.org")
    assert not same_site("https://user:pw@example.org/a", "example.org")
    assert host_variants("https://www.Example.org/x") == {"example.org", "www.example.org"}
    assert canonical_host("WWW.Example.org.") == "example.org"


def test_a_name_that_does_not_resolve_is_not_public():
    assert not is_public_host("this-host-does-not-exist.invalid")
