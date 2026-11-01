# Sujeeth Foundation Web Search Function — v0.5.0

This release upgrades the v0.4.0 crawler with persistent, page-level background crawl jobs while preserving the existing ontology-gated search and concept-tagging implementation.

## Main changes

- Persistent job, page-state, and URL-manifest tables using Azure Table Storage.
- Azure Storage Queue page workers: each URL is a separate retryable task, rather than one long HTTP crawl request.
- Crawl jobs can be inspected and cancelled; queue poison messages are recorded as failed pages.
- Sitemap URLs are discovered and seeded by the background homepage worker, then same-site links are discovered breadth-first.
- Incremental fetches use ETag and Last-Modified conditional requests; content hashes prevent unnecessary re-indexing.
- HTML, PDF, and DOCX text extraction, canonical URL handling, page-size limits, navigation/boilerplate removal, and chunk metadata.
- Domain allowlist and ontology concept restrictions remain mandatory.
- Robots.txt is checked before fetching each URL; 401/403 blocks crawling; inability to verify robots policy causes queue retry rather than assuming permission.
- Public-IP DNS check, HTTPS-only same-site restriction, redirect checks, bounded page size, configurable request timeouts, depth and page caps.
- Conservative stale-page cleanup: old URLs are deleted only after a fully clean crawl that completed below the page cap with no skipped or failed pages. Incomplete/limited/errored crawls never trigger deletion.
- Dashboard endpoint reports all ontology crawl-enabled domains, latest jobs, and index size.

## Important architecture notes

- Blob Storage remains the content/chunk store.
- Azure Table Storage holds job state, per-job URL deduplication, and the cross-crawl URL manifest.
- Azure Queue Storage holds page tasks. Azure Functions Queue Trigger provides retries; exhausted tasks go to the poison queue and are recorded as failures.
- The legacy `POST /api/refresh` endpoint is retained for compatibility but is synchronous. Use `POST /api/crawl` for production crawl runs.
- The new worker uses a single URL per queue invocation, which keeps individual executions bounded. Azure Queue delivery is at-least-once; writes are idempotent by deterministic page/chunk IDs and per-job URL records.
- Stale deletion is deliberately conservative. A crawl with any skipped URL, queue failure, or page-cap hit will not delete old content.
- AI-assisted concept classification is not enabled in this release. The existing deterministic ontology-approved concept resolver remains in use; untagged pages are visible in job metrics.
- This release has not been deployed or integration-tested against your Azure resources. Run the smoke tests below on a small domain before starting all domains.

## Required Azure Function App settings

Keep your existing settings and add/verify:

| Setting | Example | Purpose |
|---|---|---|
| `AzureWebJobsStorage` | existing storage connection string | Queue + Table storage binding and state |
| `CRAWL_QUEUE_NAME` | `web-crawl-pages` | Page task queue |
| `CRAWL_POISON_QUEUE_NAME` | `web-crawl-pages-poison` | Exhausted-task queue |
| `MAX_PAGES_PER_DOMAIN` | `1000` | Per-domain safety cap |
| `MAX_CRAWL_DEPTH` | `5` | Link depth cap |
| `MAX_PAGE_BYTES` | `5000000` | Maximum response size |
| `MAX_SITEMAPS_PER_DOMAIN` | `100` | Maximum sitemap files inspected per domain |
| `MIN_TEXT_CHARS` | `80` | Ignore near-empty extraction |
| `CHUNK_CHARS` | `1800` | Text chunk size |
| `CRAWL_DELAY_SECONDS` | `0.5` | Per-worker politeness delay |
| `REQUEST_TIMEOUT_SECONDS` | `20` | HTTP timeout |
| `CRAWLER_USER_AGENT` | `SujeethFoundationKnowledgeCrawler/0.5 (+https://www.sujeeth.org)` | Robots identity |

The app identity/connection must be able to create queues and tables in the storage account referenced by `AzureWebJobsStorage`. The current implementation uses a connection string, consistent with the existing Function App. Managed identity/RBAC can be a later security improvement.

The content Blob Storage settings remain:

- `BLOB_STORAGE_CONNECTION_STRING`
- `BLOB_CONTAINER=web-content`

The ontology settings remain:

- `ONTOLOGY_API_URL`
- `ONTOLOGY_FUNCTION_KEY`

## New endpoints

All endpoints require the Function key, as before.

### Start background crawl

`POST /api/crawl`

Optional JSON body:

```json
{"domains": ["sujeeth.org"]}
```

Omit `domains` to enqueue every crawl-enabled domain returned by the ontology. The API validates requested domains against the ontology crawl manifest before creating jobs.

### Inspect one job

`GET /api/crawl/{job_id}`

### List recent jobs

`GET /api/crawl/status`

### Cancel a job

`POST /api/crawl/{job_id}/cancel`

Cancellation prevents queued tasks from doing more work; already-running HTTP requests may finish.

### Resume failed tasks

`POST /api/crawl/{job_id}/resume`

### Dashboard

`GET /api/crawl/dashboard`

Returns all ontology crawl-enabled domains, their latest job, recent jobs, and index status.

### Existing endpoints preserved

- `GET /api/health`
- `GET /api/search?q=...`
- `POST /api/search`
- `POST /api/domain-resolution`
- `GET /api/sources`
- `GET /api/index-status`
- `POST /api/refresh` (legacy synchronous)
- `POST /api/reindex`
- `POST /api/enrich`

## Deployment

1. Add the required Function App settings in Azure Configuration. `AzureWebJobsStorage` should already exist; add both queue-name settings exactly as shown.
2. Ensure the Function App storage account has Queue and Table service enabled and the connection string has permission to create queues/tables and read/write entities/messages.
3. Deploy this project using the existing GitHub Actions workflow. Ensure the workflow installs `requirements.txt` into `.python_packages/lib/site-packages` before zipping, as in the corrected v0.4 deployment workflow.
4. Restart the Function App.
5. Check `GET /api/health`. Confirm `persistent_crawl_jobs` is `true`.
6. Start with one small domain and inspect the returned `job_id`.
7. Poll `GET /api/crawl/{job_id}` until `completed` or `completed_with_errors`.
8. Inspect `GET /api/crawl/dashboard` and run the ontology search regression queries.

## PowerShell smoke test

```powershell
$body = @{ domains = @("sujeeth.org") } | ConvertTo-Json
$job = Invoke-RestMethod -Method Post -Uri "$azureUrl/api/crawl" -Headers $headers -ContentType "application/json" -Body $body
$job | ConvertTo-Json -Depth 10

$jobId = $job.jobs[0].job_id
Invoke-RestMethod "$azureUrl/api/crawl/$jobId" -Headers $headers | ConvertTo-Json -Depth 10
Invoke-RestMethod "$azureUrl/api/crawl/dashboard" -Headers $headers | ConvertTo-Json -Depth 20
```

Do not launch all domains until the one-domain test confirms that Queue Trigger invocations are running, jobs progress, and the index is being updated.

## Known limitations / next hardening

- No JavaScript browser rendering; client-rendered content may not be extracted.
- PDF and DOCX extraction is text-only; scanned PDFs need OCR, which is not included.
- No AI classifier; deterministic ontology tagging favors precision over recall.
- The public-IP check is a defense-in-depth check, not a substitute for network egress restrictions. For higher assurance, use an outbound firewall/private network policy.
- Queue tasks are at-least-once. Deterministic IDs and page-state deduplication reduce duplicate writes, but a worker crash between a state update and queue acknowledgement can still cause a repeated fetch.
- Job counter updates are best-effort under concurrency; for very high parallelism, use transactional entity-group operations or a dedicated durable orchestrator.
- Stale-page cleanup requires a clean, complete crawl and intentionally does nothing when pages are skipped or failed. Review stale records and metrics before enabling aggressive deletion policies.
- No scheduled refresh is created automatically; use a separate scheduler after crawl stability is verified.


## v0.6.0 persistent crawl reliability

This release adds ETag-guarded page claims and terminal transitions, idempotent page completion, per-page attempt counts and processing leases, aggregate job reconciliation, and stale-page recovery. New authenticated endpoints:

- `GET /api/crawl/{job_id}/pages` — inspect persisted page records.
- `POST /api/crawl/{job_id}/reconcile` — rebuild job counters from page records.
- `POST /api/crawl/{job_id}/recover` — republish stale queued/publish-failed pages and expired processing leases. Optional JSON body: `{"stale_minutes":25}` (minimum 5, maximum 240).

Recovery is safe to repeat: completed pages are not requeued, and workers must acquire a page claim before processing. Azure Table Storage and Queue Storage do not provide a cross-service transaction, so reconciliation remains necessary after partial failures. Test with one domain before starting a multi-domain crawl.

## Version 0.6.1 — persistent queue recovery corrections

- Page claims now carry a unique lease token. Only the worker holding that token can complete or release the page claim.
- If page processing raises an exception, that worker releases its own claim back to `queued` before re-raising so Azure Queue retry can retry without waiting for the full lease duration.
- Expired-page recovery now re-reads each page and uses an ETag-guarded transition before publishing, reducing races with workers that complete concurrently.
- Queue processor logs the actual page disposition instead of always claiming that a page was processed.
- HTML response bytes are decoded once as UTF-8 with replacement before BeautifulSoup parsing.
- Reconciliation can finalize a job whose authoritative page records show no remaining pending pages.

Deploy this version before calling the existing `POST /api/crawl/{job_id}/recover` endpoint. Recovery is explicit; deployment does not automatically restart an existing job.
