
import json
import logging
import azure.functions as func

from services.config import settings
from services.ontology_client import OntologyClient
from services.concept_resolver import ConceptResolver
from services.storage_service import StorageService
from services.search_service import SearchService
from services.crawler import Crawler

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)
log = logging.getLogger("sujeeth-web-search")

storage = StorageService()
ontology = OntologyClient()
concept_resolver = ConceptResolver(ontology)
searcher = SearchService(storage)
crawler = Crawler(ontology, storage, searcher)


def response(data, status=200):
    return func.HttpResponse(
        json.dumps(data, ensure_ascii=False, default=str),
        status_code=status,
        mimetype="application/json",
    )


def read_body(req):
    try:
        value = req.get_json()
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def run_search(query, concepts=None, domains=None, purpose="search", limit=5):
    # Every search must resolve concepts and domains through the ontology.
    resolution = concept_resolver.resolve_query(
        query=query,
        explicit_concepts=concepts or [],
        explicit_domains=domains or [],
        purpose=purpose,
    )

    if not resolution["matched_concepts"]:
        return {
            "query": query,
            "status": "no_ontology_concept_match",
            "message": "The query could not be mapped to an ontology concept. No broad-domain search was performed.",
            "ontology_version": resolution.get("ontology_version"),
            "concepts": [],
            "domains": [],
            "count": 0,
            "results": [],
            "resolution": resolution,
        }

    selected_domains = resolution["domains"]
    if not selected_domains:
        return {
            "query": query,
            "status": "no_approved_domains_for_concepts",
            "message": "Ontology matched concepts but returned no approved search domains.",
            "ontology_version": resolution.get("ontology_version"),
            "concepts": resolution["matched_concepts"],
            "domains": [],
            "count": 0,
            "results": [],
            "resolution": resolution,
        }

    result = searcher.search(
        query,
        concepts=resolution["matched_concepts"],
        domains=selected_domains,
        limit=limit,
    )
    result.update({
        "status": "completed",
        "ontology_version": resolution.get("ontology_version"),
        "concepts": resolution["matched_concepts"],
        "domains": selected_domains,
        "resolution": resolution,
    })
    return result


@app.route(route="health", methods=["GET"])
def health(req):
    return response({
        "status": "healthy",
        "service": "Sujeeth Foundation Web Search",
        "version": "0.3.1",
        "storage_mode": storage.mode,
        "embedding_configured": settings.embedding_configured,
        "ontology_configured": bool(settings.ontology_api_url),
        "concept_resolution_required": True,
        "crawler_version": "0.3.1",
        "max_pages_per_domain": settings.max_pages,
        "max_crawl_depth": settings.max_crawl_depth,
        "search_mode": "hybrid" if settings.embedding_configured else "keyword_fallback",
    })


@app.route(route="search", methods=["GET"])
def search_get(req):
    query = (req.params.get("q") or "").strip()
    if not query:
        return response({"error": "Provide ?q=search terms"}, 400)
    try:
        limit = min(max(int(req.params.get("limit", "5")), 1), 5)
        concepts = [x.strip() for x in (req.params.get("concepts") or "").split(",") if x.strip()]
        domains = [x.strip() for x in (req.params.get("domains") or "").split(",") if x.strip()]
        return response(run_search(query, concepts, domains, "search", limit))
    except Exception as exc:
        log.exception("Search failed")
        return response({"error": str(exc), "stage": "concept_resolution_or_search"}, 502)


@app.route(route="search", methods=["POST"])
def search_post(req):
    body = read_body(req)
    query = str(body.get("query", "")).strip()
    if not query:
        return response({"error": "JSON body requires query"}, 400)
    try:
        limit = min(max(int(body.get("limit", 5)), 1), 5)
        return response(run_search(
            query=query,
            concepts=body.get("concepts") or [],
            domains=body.get("domains") or [],
            purpose=body.get("purpose", "search"),
            limit=limit,
        ))
    except Exception as exc:
        log.exception("Search failed")
        return response({"error": str(exc), "stage": "concept_resolution_or_search"}, 502)


@app.route(route="domain-resolution", methods=["POST"])
def domain_resolution(req):
    """Diagnostic endpoint: performs the same mandatory query-to-concept resolution as search."""
    body = read_body(req)
    query = str(body.get("query", "")).strip()
    if not query:
        return response({"error": "JSON body requires query"}, 400)
    try:
        return response(concept_resolver.resolve_query(
            query=query,
            explicit_concepts=body.get("concepts") or [],
            explicit_domains=body.get("domains") or [],
            purpose=body.get("purpose", "search"),
        ))
    except Exception as exc:
        log.exception("Domain resolution failed")
        return response({"error": str(exc)}, 502)


@app.route(route="sources", methods=["GET"])
def sources(req):
    try:
        return response({
            "search_manifest": ontology.search_manifest(),
            "crawl_manifest": ontology.crawl_manifest(),
        })
    except Exception as exc:
        log.exception("Could not retrieve ontology manifests")
        return response({"error": str(exc)}, 502)


@app.route(route="index-status", methods=["GET"])
def index_status(req):
    return response(storage.status())


@app.route(route="refresh", methods=["POST"])
def refresh(req):
    body = read_body(req)
    requested = {str(x).lower() for x in (body.get("domains") or [])}
    try:
        manifest = ontology.crawl_manifest()
        entries = manifest.get("crawl_domains", [])
        allowed = {
            (x.get("domain") if isinstance(x, dict) else str(x)).lower()
            for x in entries
        }
        rejected = sorted(requested - allowed)
        if rejected:
            return response({
                "error": "Domains are not crawl-enabled in ontology",
                "rejected_domains": rejected,
            }, 403)
        targets = sorted(requested) if requested else entries
        if not targets:
            return response({
                "status": "nothing_to_crawl",
                "message": "No crawl-enabled domains in ontology.",
            })
        return response(crawler.refresh(targets), 202)
    except Exception as exc:
        log.exception("Refresh failed")
        return response({"error": str(exc)}, 502)


@app.route(route="reindex", methods=["POST"])
def reindex(req):
    try:
        return response({"status": "completed", "documents": searcher.rebuild_vectors()})
    except Exception as exc:
        log.exception("Reindex failed")
        return response({"error": str(exc)}, 500)
