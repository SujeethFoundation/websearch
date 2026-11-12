"""Tests for the cached domain resolution."""
import pytest

from services.concept_resolver import ConceptResolver
from services.config import settings
from services.ontology_client import OntologyClient


class CountingOntology(OntologyClient):
    """An ontology client whose network calls are replaced by a tiny fake that counts them."""

    def __init__(self):
        super().__init__()
        self.posts = []

    def get(self, path):
        return {"classes": [{"id": "circle", "name": "Circle", "aliases": ["career circle"]},
                            {"id": "mentor", "name": "Mentor", "aliases": ["mentor"]}],
                "terminology": [], "relationships": []}

    def post(self, path, data):
        self.posts.append((path, data))
        if path.endswith("validate-domains"):
            return {"approved": ["sujeeth.org"], "rejected": []}
        return {"domains": [{"domain": "sujeeth.org"}, {"domain": "topics.sujeeth.org"}]}

    def resolutions(self):
        return [p for p in self.posts if p[0].endswith("domain-resolution")]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "ontology_cache_seconds", 120)
    monkeypatch.setattr(settings, "ontology_cache_max_entries", 2000)
    return CountingOntology()


def test_the_same_concepts_and_purpose_make_one_request(client):
    for _ in range(5):
        assert client.resolve_domains(["circle", "mentor"], "search")["domains"][0]["domain"] == "sujeeth.org"
    assert len(client.resolutions()) == 1


def test_a_different_purpose_or_concept_order_is_a_separate_entry(client):
    client.resolve_domains(["circle", "mentor"], "search")
    client.resolve_domains(["circle", "mentor"], "crawl")
    client.resolve_domains(["mentor", "circle"], "search")
    client.resolve_domains(["circle"], "search")
    assert len(client.resolutions()) == 4


def test_a_cache_lifetime_of_zero_turns_caching_off(client, monkeypatch):
    monkeypatch.setattr(settings, "ontology_cache_seconds", 0)
    for _ in range(3):
        client.resolve_domains(["circle"], "search")
    assert len(client.resolutions()) == 3


def test_a_failed_request_is_not_cached(client, monkeypatch):
    calls = {"n": 0}
    original = client.post

    def flaky(path, data):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("ontology app unavailable")
        return original(path, data)

    monkeypatch.setattr(client, "post", flaky)
    with pytest.raises(RuntimeError):
        client.resolve_domains(["circle"], "search")
    assert client.resolve_domains(["circle"], "search")["domains"]  # tried again, then cached
    client.resolve_domains(["circle"], "search")
    assert calls["n"] == 2


def test_an_expired_entry_is_fetched_again(client):
    client.resolve_domains(["circle"], "search")
    key = "resolve:search:circle"
    client._cache[key] = (client._cache[key][0] - 1000, client._cache[key][1])
    client.resolve_domains(["circle"], "search")
    assert len(client.resolutions()) == 2


def test_the_cache_is_bounded(client, monkeypatch):
    monkeypatch.setattr(settings, "ontology_cache_max_entries", 10)
    for i in range(40):
        client.resolve_domains([f"concept{i}"], "search")
    assert len(client._cache) <= 10


def test_one_search_now_makes_one_resolution_request_not_two(client):
    """The MCP server resolves a question, then the search resolves it again with the first answer's concepts and
    domains. Both used to POST to the ontology app."""
    resolver = ConceptResolver(client)
    first = resolver.resolve_query("What is a career circle?")
    assert first["matched_concepts"] and first["domains"]
    second = resolver.resolve_query("What is a career circle?", explicit_concepts=first["matched_concepts"],
                                    explicit_domains=first["domains"])
    assert second["domains"] == first["domains"]
    assert len(client.resolutions()) == 1


def test_without_the_cache_the_same_search_made_two_requests(client, monkeypatch):
    monkeypatch.setattr(settings, "ontology_cache_seconds", 0)
    resolver = ConceptResolver(client)
    first = resolver.resolve_query("What is a career circle?")
    resolver.resolve_query("What is a career circle?", explicit_concepts=first["matched_concepts"],
                           explicit_domains=first["domains"])
    assert len(client.resolutions()) == 2
