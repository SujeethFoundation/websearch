import json
import logging
import os
from datetime import datetime, timedelta, timezone
import azure.functions as func
from urllib.parse import urlparse
from azure.storage.queue import QueueClient, TextBase64EncodePolicy

from services.config import settings
from services.ontology_client import OntologyClient
from services.concept_resolver import ConceptResolver
from services.storage_service import StorageService
from services.search_service import SearchService, assemble_document, chunk_index
from services.crawler import Crawler
from services.crawl_state import CrawlState
from services.manual_page import build_page_documents
from services.page_fetch import fetch_page, PageFetchError
from services.page_extract import infer_description
from services.best_bets import best_bet_domains, merge_best_bets

# The Azure SDKs log every request and every response at INFO: two multi-line records per storage or table
# call. At hundreds of thousands of calls a day that floods the logs and the monitoring bill. Warnings and
# errors from the SDKs still come through.
logging.getLogger("azure").setLevel(logging.WARNING)
from services.queue_worker import QueueProcessor

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)
log = logging.getLogger("sujeeth-web-search")

storage = StorageService()
ontology = OntologyClient()
concept_resolver = ConceptResolver(ontology)
searcher = SearchService(storage)
crawler = Crawler(ontology, storage, searcher, concept_resolver)
crawl_state = None
queue_processor = None
try:
    if settings.azure_storage_connection:
        crawl_state = CrawlState()
        queue_processor = QueueProcessor(ontology, storage, searcher, concept_resolver)
except Exception:
    log.exception("Persistent crawl infrastructure could not initialize")


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
    add_best_bets(result, query, resolution["matched_concepts"], selected_domains)
    add_spotlights(result, query, resolution, selected_domains)
    return result


SPOTLIGHT_SLOTS = max(0, min(int(os.getenv("SPOTLIGHT_SLOTS", "2")), 3))


def add_spotlights(result, query, resolution, selected_domains):
    """Reserve result slots for Spotlight pages on questions the ontology says Spotlights illustrate.

    Added only when "spotlight" came in through a relationship (for example a career question),
    not when the person asked for spotlights directly. Spotlight pages carry the spotlight tag
    and compete with larger sites in ranking, so they get their own query and are placed first,
    flagged spotlight=True. Never fails the search.
    """
    try:
        via_relationship = any(
            m.get("concept_id") == "spotlight" and m.get("source") == "relationship"
            for m in resolution.get("concept_matches", [])
        )
        if not SPOTLIGHT_SLOTS or not via_relationship:
            return
        domains = [d for d in selected_domains if d == "sujeeth.org"]
        if not domains:
            return
        found = searcher.search(query, concepts=["spotlight"], domains=domains, limit=SPOTLIGHT_SLOTS * 3)
        existing = {str(x.get("url") or "").strip().lower(): x for x in result.get("results", [])}
        picked, seen = [], set()
        for item in found.get("results", []):
            key = str(item.get("url") or "").strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            picked.append({**item, "spotlight": True})
            if len(picked) >= SPOTLIGHT_SLOTS:
                break
        keep = [x for x in result.get("results", []) if str(x.get("url") or "").strip().lower() not in seen]
        result["results"] = picked + keep
        result["count"] = len(result["results"])
        result["spotlights"] = {"status": "applied" if picked else "no_match", "count": len(picked), "max": SPOTLIGHT_SLOTS}
    except Exception:
        log.exception("Spotlight lookup failed; returning normal results only")
        result["spotlights"] = {"status": "unavailable", "count": 0, "max": SPOTLIGHT_SLOTS}


BEST_BETS_MAX = max(0, min(int(os.getenv("BEST_BETS_MAX", "3")), 3))


def add_best_bets(result, query, concepts, selected_domains):
    """Put matching manually added pages first, flagged best_bet. Never fails the search."""
    status, count = "none", 0
    try:
        manual_domains = best_bet_domains(ontology.search_manifest(), selected_domains) if BEST_BETS_MAX else []
        if manual_domains:
            manual = searcher.search(query, concepts=concepts, domains=manual_domains, limit=10)
            merged = merge_best_bets(result.get("results", []), manual.get("results", []), BEST_BETS_MAX)
            count = sum(1 for x in merged if x.get("best_bet"))
            result["results"] = merged
            result["count"] = len(merged)
            status = "applied" if count else "no_match"
    except Exception:
        log.exception("Best-bet lookup failed; returning normal results only")
        status = "unavailable"
    result["best_bets"] = {"status": status, "count": count, "max": BEST_BETS_MAX}


@app.route(route="health", methods=["GET"])
def health(req):
    return response({
        "status": "healthy",
        "service": "Sujeeth Foundation Web Search",
        "version": "0.7.0",
        "storage_mode": storage.mode,
        "embedding_configured": settings.embedding_configured,
        "ai_search_configured": settings.search_configured,
        "ai_search_index": settings.search_index_name,
        "ontology_configured": bool(settings.ontology_api_url),
        "concept_resolution_required": True,
        "crawler_version": "0.7.0",
        "persistent_crawl_jobs": bool(crawl_state),
        "crawl_queue": settings.crawl_queue,
        "max_pages_per_domain": settings.max_pages,
        "max_crawl_depth": settings.max_crawl_depth,
        "search_mode": "azure_ai_search_hybrid" if settings.search_configured and settings.embedding_configured else "not_configured",
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
    try:
        return response({"blob_storage": storage.status(), "ai_search": searcher.status()})
    except Exception as exc:
        log.exception("Index status failed")
        return response({"error": str(exc)}, 502)


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
    return response({
        "error": "Use POST /api/index/backfill to queue a resumable Azure AI Search backfill.",
        "backfill_endpoint": "/api/index/backfill"
    }, 409)


@app.route(route="index/backfill", methods=["POST"])
def index_backfill(req):
    """Start a queue-driven, resumable backfill of existing Blob documents.

    Body (optional): {"mode": "missing"} indexes only chunks that are not in the index yet (the default);
    {"mode": "all"} re-sends every chunk. Stored vectors are reused either way.
    {"domains": ["sujeeth.org"]} limits the pass to chunks from those domains.
    """
    body = read_body(req)
    domains = [str(x).lower().strip() for x in (body.get("domains") or []) if str(x).strip()]
    mode = str(body.get("mode") or "missing").strip().lower()
    if mode not in ("missing", "all"):
        return response({"error": "mode must be 'missing' or 'all'"}, 400)
    if not settings.azure_storage_connection or not storage.container or not queue_processor:
        return response({"error": "Azure Blob Storage and AzureWebJobsStorage must be configured."}, 503)
    if not settings.search_configured or not settings.embedding_configured:
        return response({"error": "Configure Azure AI Search and Foundry embeddings before backfill."}, 503)
    try:
        queue = QueueClient.from_connection_string(
            settings.azure_storage_connection,
            settings.crawl_queue,
            message_encode_policy=TextBase64EncodePolicy(),
        )
        queue.send_message(json.dumps({
            "task_type": "index_backfill_page",
            "continuation_token": None,
            "mode": mode,
            "domains": domains,
        }))
        return response({
            "status": "queued",
            "task_type": "index_backfill_page",
            "mode": mode,
            "domains": domains or "all",
            "source_prefix": "documents/",
            "batch_size": settings.index_backfill_batch_size,
            "message": ("The queue worker will page through Blob Storage and index only the chunks that are not in sf-knowledge yet."
                        if mode == "missing" else
                        "The queue worker will page through Blob Storage and re-send every chunk to sf-knowledge."),
        }, 202)
    except Exception as exc:
        log.exception("AI Search backfill enqueue failed")
        return response({"error": str(exc)}, 500)



@app.route(route="index/page", methods=["POST"])
def index_page(req):
    """Add or replace one page. The server crawls it and infers the description from what it finds.

    Body: {"url": "https://...", "content": "optional page text", "title": "optional override",
           "concepts": ["optional", "class_ids"], "allow_crawled_domain": false, "preview": false}
    Without content the page is fetched like a crawl (crawler user agent, robots.txt respected). The
    title comes from the page, and the description from its summary tags or opening text. Send
    content only for a site that blocks the crawler; the description is then inferred from that
    text and a title is required. A description in the request is ignored. preview: true crawls and
    returns what would be stored, without writing.
    The page's domain must be search-enabled in the ontology and marked "manual_only": true,
    so a later crawl cannot replace the page. Resubmitting the same URL replaces its chunks.
    """
    body = read_body(req)
    if not settings.search_configured or not settings.embedding_configured:
        return response({"error": "Configure Azure AI Search and Foundry embeddings before adding pages."}, 503)
    try:
        url = Crawler._normalize_url(str(body.get("url") or "").strip())
        host = (urlparse(url).hostname or "").lower()
        validation = ontology.validate_domains([host], "search")
        approved = validation.get("approved") or []
        if not approved or validation.get("rejected"):
            return response({"error": f"{host or 'This domain'} is not search-enabled in the ontology. Add it there first."}, 403)
        if not approved[0].get("manual_only") and not body.get("allow_crawled_domain"):
            return response({"error": f"{host} is crawled, so a crawl would replace this page. Mark the domain "
                                      "manual_only in the ontology, or send allow_crawled_domain: true."}, 409)
        explicit = body.get("concepts")
        if explicit is not None and not (isinstance(explicit, list) and all(isinstance(x, str) for x in explicit)):
            return response({"error": "concepts must be an array of strings"}, 400)
        supplied = str(body.get("content") or "").strip()
        title = str(body.get("title") or "").strip()
        if supplied:
            text, origin = supplied, "supplied_text"
            description, description_source = infer_description(supplied), "supplied_text"
            if not title:
                return response({"error": "title is required when you send content; a crawl finds it from the page."}, 400)
        else:
            try:
                page = fetch_page(url)
            except PageFetchError as exc:
                return response({"error": str(exc)}, 422)
            text, origin = page["text"], "crawl"
            title = title or page["title"]
            description, description_source = page["description"], page["description_source"]
            if not title:
                return response({"error": "The page has no title tag. Send a title."}, 400)
        documents, domain, _ = build_page_documents(
            url=url, title=title, text=text, description=description,
            allowed_concepts=ontology.domain_concepts(host), tag_fn=concept_resolver.tag_document,
            chunk_chars=settings.chunk_chars, min_chars=settings.min_text_chars, explicit_concepts=explicit)
        tagged = sum(1 for d in documents if d["concepts"])
        summary = {"url": url, "domain": domain, "title": title, "description": description,
                   "description_source": description_source, "text_from": origin, "characters": len(text),
                   "chunks": len(documents), "chunks_tagged": tagged, "chunks_untagged": len(documents) - tagged}
        if body.get("preview"):
            return response({"status": "preview", **summary})
        replaced = searcher.delete_url(url)
        indexed, deferred = searcher.upsert_or_defer(documents, persist_blobs=True)
        return response({
            "status": "indexed" if not deferred else "stored_pending_index", **summary,
            "chunks_replaced": replaced, "chunks_indexed": indexed,
            "message": None if not deferred else "Chunks are stored; the search index was busy. Run POST /api/index/backfill with mode 'missing' to index them.",
        }, 200 if not deferred else 202)
    except ValueError as exc:
        return response({"error": str(exc)}, 400)
    except Exception as exc:
        log.exception("Manual page add failed")
        return response({"error": str(exc)}, 502)


@app.route(route="index/page/delete", methods=["POST"])
def index_page_delete(req):
    """Remove one page (all its chunks) from the search index and Blob Storage. Body: {"url": "https://..."}"""
    body = read_body(req)
    url = str(body.get("url") or "").strip()
    if not url:
        return response({"error": "JSON body requires url"}, 400)
    try:
        removed = searcher.delete_url(Crawler._normalize_url(url))
        return response({"status": "deleted" if removed else "not_found", "chunks_removed": removed})
    except Exception as exc:
        log.exception("Manual page delete failed")
        return response({"error": str(exc)}, 502)


@app.route(route="index/retag", methods=["POST"])
def index_retag(req):
    """Queue a resumable re-tag of stored documents using the current ontology.

    Updates tags in Blob Storage and merges them into the search index. No embeddings are
    generated, so this does not use Azure OpenAI quota. Optional body: {"domains": ["sujeeth.org"]}.
    """
    if not settings.azure_storage_connection or not storage.container or not queue_processor:
        return response({"error": "Azure Blob Storage and AzureWebJobsStorage must be configured."}, 503)
    if not settings.search_configured:
        return response({"error": "Configure Azure AI Search before re-tagging."}, 503)
    body = read_body(req)
    domains = [str(x).lower().strip() for x in (body.get("domains") or []) if str(x).strip()]
    try:
        queue = QueueClient.from_connection_string(
            settings.azure_storage_connection,
            settings.crawl_queue,
            message_encode_policy=TextBase64EncodePolicy(),
        )
        queue.send_message(json.dumps({
            "task_type": "index_retag_page",
            "continuation_token": None,
            "domains": domains,
        }))
        return response({
            "status": "queued",
            "task_type": "index_retag_page",
            "domains": domains or "all",
            "message": "The queue worker will page through Blob Storage, re-tag documents with the current ontology, and merge changed tags into the index.",
        }, 202)
    except Exception as exc:
        log.exception("Retag enqueue failed")
        return response({"error": str(exc)}, 500)


@app.route(route="index/retag-preview", methods=["POST"])
def index_retag_preview(req):
    """Read-only sample of how many stored documents a re-tag would change."""
    if not storage.container or not queue_processor:
        return response({"error": "Azure Blob Storage must be configured."}, 503)
    body = read_body(req)
    domains = [str(x).lower().strip() for x in (body.get("domains") or []) if str(x).strip()]
    try:
        return response(queue_processor.retag_preview(domains, scan_limit=int(body.get("scan_limit", 1000))))
    except Exception as exc:
        log.exception("Retag preview failed")
        return response({"error": str(exc)}, 502)


@app.route(route="index/purge-preview", methods=["POST"])
def index_purge_preview(req):
    """Read-only sample of how many stored chunks fall under the ontology's excluded paths."""
    if not storage.container or not queue_processor:
        return response({"error": "Azure Blob Storage must be configured."}, 503)
    body = read_body(req)
    domains = [str(x).lower().strip() for x in (body.get("domains") or []) if str(x).strip()]
    try:
        return response(queue_processor.purge_preview(domains, scan_limit=int(body.get("scan_limit", 1000))))
    except Exception as exc:
        log.exception("Purge preview failed")
        return response({"error": str(exc)}, 502)


@app.route(route="index/purge", methods=["POST"])
def index_purge(req):
    """Queue a resumable purge of stored chunks under the ontology's excluded paths.

    Removes the chunks from the search index and Blob Storage and clears their crawl manifest
    rows. Optional body: {"domains": ["tealhq.com"]} to limit it to some domains.
    """
    if not settings.azure_storage_connection or not storage.container or not queue_processor:
        return response({"error": "Azure Blob Storage and AzureWebJobsStorage must be configured."}, 503)
    body = read_body(req)
    domains = [str(x).lower().strip() for x in (body.get("domains") or []) if str(x).strip()]
    try:
        queue = QueueClient.from_connection_string(
            settings.azure_storage_connection,
            settings.crawl_queue,
            message_encode_policy=TextBase64EncodePolicy(),
        )
        queue.send_message(json.dumps({
            "task_type": "index_purge_page",
            "continuation_token": None,
            "domains": domains,
        }))
        return response({
            "status": "queued",
            "task_type": "index_purge_page",
            "domains": domains or "all",
            "message": "The queue worker will page through Blob Storage and remove chunks whose pages fall under an excluded path.",
        }, 202)
    except Exception as exc:
        log.exception("Purge enqueue failed")
        return response({"error": str(exc)}, 500)


@app.route(route="document", methods=["POST"])
def document(req):
    """Return one indexed page, or a passage with its neighbors, within size limits.

    Domains listed in FULL_TEXT_DOMAINS return up to max_chars. Every other domain returns
    an excerpt only (EXCERPT_MAX_CHARS) with a note to link to the source.
    """
    body = read_body(req)
    url = str(body.get("url") or "").strip()
    chunk_id = str(body.get("chunk_id") or "").strip()
    if not url and not chunk_id:
        return response({"error": "JSON body requires url or chunk_id"}, 400)
    if not settings.search_configured:
        return response({"error": "Azure AI Search is not configured."}, 503)
    try:
        neighbors = int(body.get("neighbors", 1))
        max_chars = max(500, min(int(body.get("max_chars", 6000)), 20000))
        rows, focus, total = searcher.fetch_chunks(
            None if chunk_id else url, chunk_id or None, neighbors)
        if not rows:
            return response({"error": "Document not found in the index."}, 404)
        domains = sorted({str(r.get("domain") or "").lower() for r in rows})
        validation = ontology.validate_domains(domains, "search")
        if not validation.get("approved") or validation.get("rejected"):
            return response({"error": "This domain is not approved for search."}, 403)
        full_text_domains = {
            x.strip().lower()
            for x in os.getenv("FULL_TEXT_DOMAINS", "sujeeth.org,topics.sujeeth.org").split(",")
            if x.strip()
        }
        excerpt_only = any(d not in full_text_domains for d in domains)
        limit = min(max_chars, int(os.getenv("EXCERPT_MAX_CHARS", "3000"))) if excerpt_only else max_chars
        chosen, truncated = assemble_document(rows, focus, limit)
        return response({
            "status": "ok",
            "title": chosen[0].get("title"),
            "url": chosen[0].get("url"),
            "domain": domains[0],
            "source": "website",
            "excerpt_only": excerpt_only,
            "total_chunks": total,
            "chunks_returned": len(chosen),
            "complete_page": bool(total is not None and len(chosen) == total and not truncated),
            "truncated": truncated,
            "chunks": [
                {"chunk_id": r.get("id"), "index": chunk_index(r.get("id")), "text": r.get("content")}
                for r in chosen
            ],
            "text": "\n\n".join(str(r.get("content") or "") for r in chosen),
            "note": ("Excerpt of third-party content. Link to the source URL instead of reproducing it."
                     if excerpt_only else None),
        })
    except ValueError as exc:
        return response({"error": str(exc)}, 400)
    except Exception as exc:
        log.exception("Document lookup failed")
        return response({"error": str(exc)}, 502)


def _crawl_limits(item=None):
    """Page cap and depth for a crawl job.

    Crawl size is an operational setting of this app, not ontology content. The limits come
    from MAX_PAGES_PER_DOMAIN and MAX_CRAWL_DEPTH in the app settings, and the ontology
    only decides which domains may be crawled.
    """
    return max(1, settings.max_pages), max(0, settings.max_crawl_depth)


@app.route(route="crawl", methods=["POST"])
def crawl_start(req):
    """Create persistent page-level crawl jobs. Work is executed by the Storage Queue trigger."""
    if not crawl_state:
        return response({"error":"Persistent crawl infrastructure is not configured. Set AzureWebJobsStorage and deploy the required storage dependencies."},503)
    body=read_body(req)
    requested={str(x).lower().strip() for x in (body.get("domains") or []) if str(x).strip()}
    try:
        manifest=ontology.crawl_manifest(); entries=manifest.get("crawl_domains",[])
        allowed={str(x.get("domain") if isinstance(x,dict) else x).lower() for x in entries}
        rejected=sorted(requested-allowed)
        if rejected: return response({"error":"Domains are not crawl-enabled in ontology","rejected_domains":rejected},403)
        targets=[x for x in entries if (not requested or str(x.get("domain") if isinstance(x,dict) else x).lower() in requested)]
        created=[]
        for item in targets:
            domain=str(item.get("domain") if isinstance(item,dict) else item).lower()
            cap,depth=_crawl_limits(item)
            concepts=ontology.domain_concepts(domain)
            job_id=crawl_state.create_job(domain,cap,depth,concepts)
            # Enqueue only the homepage here. The queue worker discovers and registers
            # sitemap URLs before it marks the homepage task complete.
            seeds=[(f"https://{domain}/",0)]
            queued=crawl_state.enqueue_pages(job_id,domain,seeds)
            if not queued:
                crawl_state._change(job_id,status="failed",error="Could not enqueue any seed URLs")
            created.append({"job_id":job_id,"domain":domain,"status":"queued","page_limit":cap,"max_depth":depth,"allowed_concepts":concepts})
        return response({"status":"queued","job_count":len(created),"jobs":created,"message":"Page-level crawl tasks are running in Azure Storage Queue workers."},202)
    except Exception as exc:
        log.exception("Could not create crawl jobs"); return response({"error":str(exc)},502)

@app.route(route="crawl/all", methods=["POST"])
def crawl_all(req):
    """Start persistent crawl jobs for all ontology-approved domains."""
    if not crawl_state:
        return response({
            "error": "Persistent crawl infrastructure is not configured."
        }, 503)

    try:
        manifest = ontology.crawl_manifest()
        entries = manifest.get("crawl_domains", [])

        if not entries:
            return response({
                "status": "nothing_to_crawl",
                "job_count": 0,
                "message": "No crawl-enabled domains found."
            })

        existing_jobs = crawl_state.list_jobs(1000)

        # Do not start a second job for a domain with an active crawl.
        active_statuses = {"queued", "running", "processing"}
        active_domains = {
            str(job.get("domain", "")).lower()
            for job in existing_jobs
            if job.get("status") in active_statuses
        }

        created = []
        skipped = []

        for item in entries:
            domain = str(
                item.get("domain") if isinstance(item, dict) else item
            ).lower().strip()

            if not domain:
                continue

            if domain in active_domains:
                skipped.append({
                    "domain": domain,
                    "reason": "An active crawl already exists."
                })
                continue

            cap, depth = _crawl_limits(item)

            concepts = ontology.domain_concepts(domain)

            job_id = crawl_state.create_job(
                domain, cap, depth, concepts
            )

            queued = crawl_state.enqueue_pages(
                job_id,
                domain,
                [(f"https://{domain}/", 0)]
            )

            if not queued:
                crawl_state._change(
                    job_id,
                    status="failed",
                    error="Could not enqueue homepage."
                )
                skipped.append({
                    "domain": domain,
                    "job_id": job_id,
                    "reason": "Homepage could not be queued."
                })
                continue

            created.append({
                "job_id": job_id,
                "domain": domain,
                "status": "queued",
                "page_limit": cap,
                "max_depth": depth,
                "allowed_concepts": concepts
            })

        return response({
            "status": "queued",
            "job_count": len(created),
            "skipped_count": len(skipped),
            "jobs": created,
            "skipped": skipped,
            "message": "Bulk crawl jobs submitted to Azure Storage Queue."
        }, 202)

    except Exception as exc:
        log.exception("Bulk crawl failed")
        return response({"error": str(exc)}, 502)

@app.route(route="crawl-status", methods=["GET"])
def crawl_status(req):
    if not crawl_state: return response({"error":"Crawl state is not configured"},503)
    jobs=crawl_state.list_jobs(100)
    return response({"count":len(jobs),"jobs":jobs})


@app.route(route="crawl-dashboard", methods=["GET"])
def crawl_dashboard(req):
    if not crawl_state: return response({"error":"Crawl state is not configured"},503)
    jobs=crawl_state.list_jobs(100)
    by_domain={}
    for j in jobs:
        d=j.get("domain")
        if d not in by_domain: by_domain[d]=j
    manifest=ontology.crawl_manifest(); entries=manifest.get("crawl_domains",[])
    domain_rows=[]
    for item in entries:
        domain=str(item.get("domain") if isinstance(item,dict) else item)
        domain_rows.append({"domain":domain,"crawl_enabled":True,"latest_job":by_domain.get(domain)})
    return response({"domains":domain_rows,"recent_jobs":jobs,"index":storage.status(),"generated_utc":__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()})

@app.route(route="crawl/domain-stats", methods=["GET"])
def crawl_domain_stats(req):
    if not crawl_state:
        return response({"error": "Crawl state is not configured"}, 503)

    try:
        # Retrieve crawl history across domains.
        jobs = crawl_state.list_jobs(10000)

        stats = {}

        for job in jobs:
            domain = str(job.get("domain", "unknown")).lower()

            if domain not in stats:
                stats[domain] = {
                    "domain": domain,
                    "jobs": 0,
                    "job_statuses": {},
                    "pages_discovered": 0,
                    "processed": 0,
                    "unchanged": 0,
                    "skipped": 0,
                    "failed": 0,
                    "queued": 0,
                    "processing": 0,
                    "publish_failed": 0,
                    "chunks_indexed": 0,
                    "chunks_tagged": 0,
                    "chunks_untagged": 0,
                    "pending": 0,
                    "latest_job": None,
                    "latest_created_utc": None,
                    "latest_updated_utc": None
                }

            row = stats[domain]
            row["jobs"] += 1

            status = job.get("status", "unknown")
            row["job_statuses"][status] = (
                row["job_statuses"].get(status, 0) + 1
            )

            # Aggregate counters stored on each crawl job.
            for field in (
                "pages_discovered",
                "chunks_indexed",
                "chunks_tagged",
                "chunks_untagged",
                "pending"
            ):
                row[field] += int(job.get(field, 0) or 0)

            # The job list is newest first.
            if row["latest_job"] is None:
                row["latest_job"] = job.get("RowKey")
                row["latest_status"] = status
                row["latest_created_utc"] = job.get("created_utc")
                row["latest_updated_utc"] = job.get("updated_utc")

            # Count actual page states from the page table.
            for page in crawl_state.page_records(job["RowKey"]):
                page_status = page.get("status", "queued")

                if page_status in (
                    "processed",
                    "unchanged",
                    "skipped",
                    "failed",
                    "queued",
                    "processing",
                    "publish_failed"
                ):
                    row[page_status] += 1

        return response({
            "domain_count": len(stats),
            "job_count": len(jobs),
            "aggregation": "all retained crawl jobs; page counts are per-job records",
            "domains": sorted(
                stats.values(),
                key=lambda x: x["domain"]
            )
        })

    except Exception as exc:
        log.exception("Domain crawl statistics failed")
        return response({"error": str(exc)}, 500)

@app.route(route="crawl/{job_id}", methods=["GET"])
def crawl_get(req):
    if not crawl_state: return response({"error":"Crawl state is not configured"},503)
    job=crawl_state.get_job(req.route_params.get("job_id"))
    return response(job or {"error":"Crawl job not found"},200 if job else 404)

@app.route(route="crawl/{job_id}/cancel", methods=["POST"])
def crawl_cancel(req):
    if not crawl_state: return response({"error":"Crawl state is not configured"},503)
    job=crawl_state.cancel_job(req.route_params.get("job_id"))
    return response(job or {"error":"Crawl job not found"},200 if job else 404)

@app.route(route="crawl/{job_id}/resume", methods=["POST"])
def crawl_resume(req):
    if not crawl_state: return response({"error":"Crawl state is not configured"},503)
    try:
        result=crawl_state.resume_job(req.route_params.get("job_id"))
        return response(result or {"error":"Crawl job not found"},200 if result else 404)
    except Exception as exc:
        log.exception("Could not resume crawl job"); return response({"error":str(exc)},500)

@app.route(route="crawl/{job_id}/pages", methods=["GET"])
def crawl_pages(req):
    if not crawl_state:
        return response({"error": "Crawl state is not configured"}, 503)
    job_id = req.route_params.get("job_id")
    job = crawl_state.get_job(job_id)
    if not job:
        return response({"error": "Crawl job not found"}, 404)
    pages = crawl_state.page_records(job_id)
    pages.sort(key=lambda item: item.get("created_utc", ""))
    return response({"job_id": job_id, "count": len(pages), "pages": pages})


@app.route(route="crawl/{job_id}/reconcile", methods=["POST"])
def crawl_reconcile(req):
    if not crawl_state:
        return response({"error": "Crawl state is not configured"}, 503)
    try:
        result = crawl_state.reconcile_job(req.route_params.get("job_id"))
        return response(result or {"error": "Crawl job not found"}, 200 if result else 404)
    except Exception as exc:
        log.exception("Crawl reconciliation failed")
        return response({"error": str(exc)}, 500)


@app.route(route="crawl/{job_id}/recover", methods=["POST"])
def crawl_recover(req):
    if not crawl_state:
        return response({"error": "Crawl state is not configured"}, 503)
    try:
        body = read_body(req)
        stale_minutes = min(max(int(body.get("stale_minutes", 25)), 5), 240)
        result = crawl_state.recover_job(req.route_params.get("job_id"), stale_minutes=stale_minutes)
        return response(result or {"error": "Crawl job not found"}, 200 if result else 404)
    except Exception as exc:
        log.exception("Crawl recovery failed")
        return response({"error": str(exc)}, 500)


@app.queue_trigger(arg_name="msg", queue_name="%CRAWL_QUEUE_NAME%", connection="AzureWebJobsStorage")
def crawl_page_worker(msg: func.QueueMessage):
    if not queue_processor:
        raise RuntimeError("Queue processor is unavailable; verify AzureWebJobsStorage and Table/Queue dependencies.")
    body=msg.get_body().decode("utf-8")
    try:
        result=queue_processor.process(body)
        log.info("Crawl queue disposition: %s",result)
    except Exception:
        log.exception("Crawl page task failed; Azure Queue retry will handle it")
        raise


@app.route(route="enrich", methods=["POST"])
def enrich(req):
    """Backfill ontology concept tags on content already stored by the crawler."""
    try:
        result = concept_resolver.enrich_existing_documents(storage, searcher)
        result["ontology_version"] = ontology.summary().get("ontology_version")
        return response(result)
    except Exception as exc:
        log.exception("Document concept enrichment failed")
        return response({"error": str(exc), "stage": "document_enrichment"}, 502)


@app.queue_trigger(arg_name="msg", queue_name="%CRAWL_POISON_QUEUE_NAME%", connection="AzureWebJobsStorage")
def crawl_page_poison(msg: func.QueueMessage):
    """Record tasks that exhausted Azure Queue retries instead of silently losing them."""
    if not crawl_state: raise RuntimeError("Crawl state is unavailable")
    try:
        task=json.loads(msg.get_body().decode("utf-8"))
        if task.get("task_type", "crawl_page") == "crawl_page":
            crawl_state.fail_page(task.get("job_id"), task.get("url"),
                "Queue retry limit exceeded; inspect poison queue message and function logs.")
        else:
            log.error("AI Search queue task moved to poison queue: task_type=%s payload=%s",
                      task.get("task_type"), task)
    except Exception:
        log.exception("Could not record poison crawl message")
        raise


# ---------------------------------------------------------------------------
# Scheduled maintenance (all schedules are UTC). Manual HTTP endpoints remain.
# ---------------------------------------------------------------------------
_TERMINAL_JOB_STATUSES = ("completed", "completed_with_errors", "failed", "cancelled")


def _job_age_days(job):
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(job["created_utc"])).days
    except Exception:
        return 0  # unknown age: treat as recent


@app.timer_trigger(schedule="0 0 10 * * *", arg_name="timer", run_on_startup=False)
def scheduled_crawl_recovery(timer: func.TimerRequest) -> None:
    """Daily 10:00 UTC: reconcile and recover unfinished crawl jobs. No-op when healthy."""
    if not crawl_state:
        log.error("Scheduled recovery skipped: crawl state is not configured")
        return
    examined = 0
    for job in crawl_state.list_jobs(100):
        status = job.get("status")
        age = _job_age_days(job)
        # Unfinished jobs are recovered for up to 30 days. Jobs that finished with
        # errors get failed pages retried for 3 days only, so permanently failing
        # URLs (404s, robots blocks) are not refetched forever.
        if status in ("cancelled", "failed", "completed"):
            continue
        if status == "completed_with_errors" and age > 3:
            continue
        if age > 30:
            continue
        try:
            crawl_state.reconcile_job(job["job_id"])
            result = crawl_state.recover_job(job["job_id"], stale_minutes=25)
            examined += 1
            log.info("Daily recovery job=%s domain=%s requeued=%s",
                     job["job_id"], job.get("domain"), (result or {}).get("requeued"))
        except Exception:
            log.exception("Daily recovery failed for job=%s", job.get("job_id"))
    log.info("Daily crawl recovery finished; jobs examined: %d", examined)


# Domains that get a crawl every week. Every other crawl-enabled ontology domain is crawled monthly.
# Override with the WEEKLY_CRAWL_DOMAINS app setting (comma-separated). "*" or "all" puts every domain
# on the weekly schedule and leaves the monthly crawl with nothing to do.
WEEKLY_CRAWL_DEFAULT = "sujeeth.org,topics.sujeeth.org"


def _weekly_allowlist():
    configured = os.getenv("WEEKLY_CRAWL_DOMAINS", WEEKLY_CRAWL_DEFAULT)
    return {x.strip().lower() for x in configured.split(",") if x.strip()}


def _is_weekly_domain(domain, allowlist):
    return bool(allowlist & {"*", "all"}) or domain in allowlist


def _start_scheduled_crawls(label, wanted):
    """Start an incremental crawl for each crawl-enabled ontology domain that wanted(domain) accepts.

    ETag/Last-Modified checks and content hashes skip unchanged pages, so every run walks the whole
    domain but only re-embeds and re-indexes what changed. Each domain is limited to MAX_PAGES_PER_DOMAIN
    pages. A domain with a job still open is skipped.
    """
    if not crawl_state:
        log.error("%s crawl skipped: crawl state is not configured", label)
        return
    open_domains = {
        str(j.get("domain", "")).lower()
        for j in crawl_state.list_jobs(1000)
        if j.get("status") not in _TERMINAL_JOB_STATUSES
    }
    try:
        entries = ontology.crawl_manifest().get("crawl_domains", [])
    except Exception:
        log.exception("%s crawl: could not read ontology crawl manifest", label)
        return
    started = 0
    for item in entries:
        domain = str(item.get("domain") if isinstance(item, dict) else item).lower().strip()
        if not domain or not wanted(domain):
            continue
        if domain in open_domains:
            log.warning("%s crawl: skipping %s, a job is still open", label, domain)
            continue
        try:
            cap, depth = _crawl_limits(item)
            job_id = crawl_state.create_job(domain, cap, depth, ontology.domain_concepts(domain))
            if crawl_state.enqueue_pages(job_id, domain, [(f"https://{domain}/", 0)]):
                started += 1
                log.info("%s crawl started domain=%s job=%s", label, domain, job_id)
            else:
                crawl_state._change(job_id, status="failed", error="Could not enqueue homepage.")
                log.error("%s crawl could not enqueue homepage for %s", label, domain)
        except Exception:
            log.exception("%s crawl failed to start for %s", label, domain)
    log.info("%s crawl finished starting jobs; domains started: %d", label, started)


@app.timer_trigger(schedule="0 0 8 * * 0", arg_name="timer", run_on_startup=False)
def scheduled_weekly_crawl(timer: func.TimerRequest) -> None:
    """Sundays 08:00 UTC: incremental crawl of the domains in WEEKLY_CRAWL_DOMAINS."""
    allowlist = _weekly_allowlist()
    _start_scheduled_crawls("Weekly", lambda domain: _is_weekly_domain(domain, allowlist))


@app.timer_trigger(schedule="0 0 9 1 * *", arg_name="timer", run_on_startup=False)
def scheduled_monthly_crawl(timer: func.TimerRequest) -> None:
    """1st of each month, 09:00 UTC: incremental crawl of every crawl-enabled domain that is not weekly."""
    allowlist = _weekly_allowlist()
    _start_scheduled_crawls("Monthly", lambda domain: not _is_weekly_domain(domain, allowlist))


@app.timer_trigger(schedule="0 0 11 * * *", arg_name="timer", run_on_startup=False)
def scheduled_index_reconcile(timer: func.TimerRequest) -> None:
    """Daily 11:00 UTC: index any stored chunk that is missing from the search index.

    Queues the same resumable backfill as POST /api/index/backfill, in "missing" mode, so a clean
    index costs one cheap existence check per 10 chunks and no writes. The run is skipped when:
      - INDEX_RECONCILE_ENABLED is set to false,
      - storage, search, or embeddings are not configured, or
      - the queue already holds more than INDEX_RECONCILE_MAX_QUEUE messages (default 500). That keeps
        one nightly run from starting on top of an unfinished one, and from piling onto a busy index.
    """
    if os.getenv("INDEX_RECONCILE_ENABLED", "true").strip().lower() in ("0", "false", "no", "off"):
        log.info("Nightly index reconcile is turned off (INDEX_RECONCILE_ENABLED)")
        return
    if (not settings.azure_storage_connection or not storage.container or not queue_processor
            or not settings.search_configured or not settings.embedding_configured):
        log.warning("Nightly index reconcile skipped: storage, search, or embeddings are not configured")
        return
    try:
        queue = QueueClient.from_connection_string(
            settings.azure_storage_connection,
            settings.crawl_queue,
            message_encode_policy=TextBase64EncodePolicy(),
        )
        limit = int(os.getenv("INDEX_RECONCILE_MAX_QUEUE", "500") or 500)
        waiting = int(queue.get_queue_properties().approximate_message_count or 0)
        if waiting > limit:
            log.warning("Nightly index reconcile skipped: the queue holds %d messages (limit %d)", waiting, limit)
            return
        queue.send_message(json.dumps({
            "task_type": "index_backfill_page",
            "continuation_token": None,
            "mode": "missing",
        }))
        log.info("Nightly index reconcile queued (the queue held %d messages)", waiting)
    except Exception:
        log.exception("Nightly index reconcile could not be queued")
