import services.concept_resolver as cr
from services.ontology_client import OntologyClient
from tests.test_relationship_effects import CLASSES, PREDICATES, RELATIONSHIPS


class Slim:
    """An ontology app that has the slim route. Any other route would be a second call, so it fails the test."""
    def __init__(self):
        self.calls = []

    def catalog(self):
        self.calls.append("catalog")
        return {"ontology_version": "t", "classes": CLASSES, "predicates": PREDICATES,
                "relationships": RELATIONSHIPS, "preferred_terminology": []}

    def ontology(self):
        raise AssertionError("the full ontology must not be read when the slim catalog answers")

    classes = terminology = relationships = ontology

    def resolve_domains(self, concepts, purpose="search"):
        return {"domains": [{"domain": "sujeeth.org"}], "ontology_version": "t"}


class Old:
    """An ontology app from before the slim route: catalog() answers None."""
    def __init__(self):
        self.calls = []

    def catalog(self):
        return None

    def ontology(self):
        self.calls.append("ontology")
        return {"classes": CLASSES, "predicates": PREDICATES, "ontology_version": "t"}

    def classes(self):
        self.calls.append("classes")
        return {"classes": CLASSES}

    def terminology(self):
        self.calls.append("terminology")
        return {"terminology": []}

    def relationships(self):
        self.calls.append("relationships")
        return {"relationships": RELATIONSHIPS}

    def resolve_domains(self, concepts, purpose="search"):
        return {"domains": [{"domain": "sujeeth.org"}], "ontology_version": "t"}


def test_the_catalog_is_built_from_one_call():
    ontology = Slim()
    result = cr.ConceptResolver(ontology).resolve_query("networking")
    assert ontology.calls == ["catalog"]
    assert result["ontology_version"] == "t"


def test_an_older_ontology_app_is_read_the_old_way_with_the_same_result():
    old, slim = Old(), Slim()
    assert cr.ConceptResolver(old).resolve_query("networking") == cr.ConceptResolver(slim).resolve_query("networking")
    assert old.calls == ["ontology", "terminology", "relationships"]


def test_client_returns_none_when_the_route_is_missing_or_broken(monkeypatch):
    client = OntologyClient()
    monkeypatch.setattr(client, "get", lambda path: (_ for _ in ()).throw(RuntimeError("404")))
    assert client.catalog() is None
    monkeypatch.setattr(client, "get", lambda path: {"error": "x"})
    assert client.catalog() is None
    good = {"classes": [], "ontology_version": "1"}
    monkeypatch.setattr(client, "get", lambda path: good)
    assert client.catalog() is good
