"""Tests for the sized connection pools."""
import requests

from services import config
from services.config import settings
from services.embedding_service import EmbeddingService
from services.http_pool import pooled_session
from services.ontology_client import OntologyClient
from services.search_service import SearchService


def pool_size(session, url="https://example.test/"):
    adapter = session.get_adapter(url)
    return adapter._pool_maxsize, adapter._pool_connections


def test_the_default_pool_is_larger_than_requests_ten():
    assert settings.http_pool_size == 64
    assert pool_size(pooled_session()) == (64, 64)
    assert pool_size(pooled_session(), "http://example.test/") == (64, 64)
    assert pool_size(requests.Session())[0] == 10  # what these services used before


def test_the_pool_size_follows_the_setting(monkeypatch):
    monkeypatch.setattr(settings, "http_pool_size", 7)
    assert pool_size(pooled_session()) == (7, 7)


def test_the_ontology_search_and_embedding_clients_use_sized_pools():
    assert pool_size(OntologyClient().session)[0] == settings.http_pool_size
    assert pool_size(EmbeddingService().session)[0] == settings.http_pool_size
    assert pool_size(SearchService(storage=None).session)[0] == settings.http_pool_size


def test_a_session_keeps_its_headers():
    assert OntologyClient().session.headers["Accept"] == "application/json"


def test_the_pool_setting_is_clamped_and_falls_back(monkeypatch):
    monkeypatch.setenv("HTTP_POOL_SIZE", "0")
    assert config._int("HTTP_POOL_SIZE", 64, 1, 1000) == 1
    monkeypatch.setenv("HTTP_POOL_SIZE", "5000")
    assert config._int("HTTP_POOL_SIZE", 64, 1, 1000) == 1000
    monkeypatch.setenv("HTTP_POOL_SIZE", "abc")
    assert config._int("HTTP_POOL_SIZE", 64, 1, 1000) == 64
    monkeypatch.delenv("HTTP_POOL_SIZE")
    assert config._int("HTTP_POOL_SIZE", 64, 1, 1000) == 64
