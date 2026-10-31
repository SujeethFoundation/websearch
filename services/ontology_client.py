
import requests
from services.config import settings


class OntologyClient:
    def __init__(self):
        self.base = settings.ontology_api_url
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        if settings.ontology_function_key:
            self.session.headers["x-functions-key"] = settings.ontology_function_key

    def _url(self, path):
        if not self.base:
            raise RuntimeError("ONTOLOGY_API_URL is not configured")
        return self.base + path

    def get(self, path):
        r = self.session.get(self._url(path), timeout=settings.timeout)
        r.raise_for_status()
        return r.json()

    def post(self, path, data):
        r = self.session.post(self._url(path), json=data, timeout=settings.timeout)
        r.raise_for_status()
        return r.json()

    def ontology(self):
        return self.get("/api/ontology")

    def summary(self):
        return self.get("/api/ontology/summary")

    def classes(self):
        return self.get("/api/ontology/classes")

    def terminology(self):
        return self.get("/api/ontology/terminology")

    def relationships(self):
        return self.get("/api/ontology/relationships")

    def search_manifest(self):
        return self.get("/api/ontology/search-manifest")

    def crawl_manifest(self):
        return self.get("/api/ontology/crawl-manifest")

    def resolve_domains(self, concepts, purpose="search"):
        return self.post("/api/ontology/domain-resolution", {
            "concepts": concepts,
            "purpose": purpose,
        })

    def validate_domains(self, domains, purpose="search"):
        return self.post("/api/ontology/validate-domains", {
            "domains": domains,
            "purpose": purpose,
        })
