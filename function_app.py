import json
import logging
import os
from datetime import datetime, timedelta, timezone
import azure.functions as func
from azure.storage.queue import QueueClient, TextBase64EncodePolicy

from services.config import settings
from services.ontology_client import OntologyClient
from services.concept_resolver import ConceptResolver
from services.storage_service import StorageService
from services.search_service import SearchService
from services.crawler import Crawler
from services.crawl_state import CrawlState
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
    return result


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
    """Start a queue-driven, resumable backfill of existing Blob documents."""
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
        }))
        return response({
            "status": "queued",
            "task_type": "index_backfill_page",
            "source_prefix": "documents/",
            "batch_size": settings.index_backfill_batch_size,
            "message": "The queue worker will page through Blob Storage, generate embeddings in batches, and upload documents to sf-knowledge."
        }, 202)
    except Exception as exc:
        log.exception("AI Search backfill enqueue failed")
        return response({"error": str(exc)}, 500)



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


@app.timer_trigger(schedule="0 0 8 * * 0", arg_name="timer", run_on_startup=False)
def scheduled_weekly_crawl(timer: func.TimerRequest) -> None:
    """Sundays 08:00 UTC: start an incremental crawl for each allowed domain.

    ETag/Last-Modified checks and content hashes skip unchanged pages. By default every
    crawl-enabled ontology domain is crawled. Set WEEKLY_CRAWL_DOMAINS to a comma-separated
    list to restrict it. Each domain is limited to MAX_PAGES_PER_DOMAIN pages.
    """
    if not crawl_state:
        log.error("Weekly crawl skipped: crawl state is not configured")
        return
    configured = os.getenv("WEEKLY_CRAWL_DOMAINS", "*")
    allowlist = {x.strip().lower() for x in configured.split(",") if x.strip()}
    crawl_everything = bool(allowlist & {"*", "all"})
    open_domains = {
        str(j.get("domain", "")).lower()
        for j in crawl_state.list_jobs(1000)
        if j.get("status") not in _TERMINAL_JOB_STATUSES
    }
    try:
        entries = ontology.crawl_manifest().get("crawl_domains", [])
    except Exception:
        log.exception("Weekly crawl: could not read ontology crawl manifest")
        return
    for item in entries:
        domain = str(item.get("domain") if isinstance(item, dict) else item).lower().strip()
        if not crawl_everything and domain not in allowlist:
            continue
        if domain in open_domains:
            log.warning("Weekly crawl: skipping %s, a job is still open", domain)
            continue
        try:
            cap, depth = _crawl_limits(item)
            job_id = crawl_state.create_job(domain, cap, depth, ontology.domain_concepts(domain))
            if crawl_state.enqueue_pages(job_id, domain, [(f"https://{domain}/", 0)]):
                log.info("Weekly crawl started domain=%s job=%s", domain, job_id)
            else:
                crawl_state._change(job_id, status="failed", error="Could not enqueue homepage.")
                log.error("Weekly crawl could not enqueue homepage for %s", domain)
        except Exception:
            log.exception("Weekly crawl failed to start for %s", domain)