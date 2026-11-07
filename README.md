# Sujeeth Foundation Web Search — v0.7.x

Azure Functions app (Python v2) that crawls ontology-approved websites, stores the text in Blob Storage, indexes it in Azure AI Search, and serves ontology-gated search to the MCP server and other clients.

## How it fits together

| Piece | Role |
|---|---|
| Ontology app | Decides which domains may be crawled or searched and which concepts a query maps to. Crawl size limits are **not** in the ontology. |
| Blob Storage (`documents/`) | Durable copy of every chunk (JSON). The index can be rebuilt from it. |
| Table Storage | Crawl jobs, page state, and the cross-crawl URL manifest. |
| Queue Storage | One message per page to crawl, plus backfill and re-tag tasks. Failed tasks go to the poison queue. |
| Azure AI Search (`sf-knowledge-v2`) | Hybrid keyword + vector retrieval. The **index's vectorizer** turns query text into a vector, using the same Azure OpenAI embedding deployment that indexing uses. |
| Azure OpenAI / Foundry (`sf-embeddings`) | Embeds chunks at indexing time. |

Every search must resolve to ontology concepts and approved domains first. If no concept matches, nothing is searched.

## Endpoints

All routes require a Function key and are under `/api`.

**Search and content**

| Route | Purpose |
|---|---|
| `GET /health` | Version, configuration flags, search mode |
| `GET /search?q=` and `POST /search` | Ontology-gated hybrid search. Body: `query`, optional `concepts`, `domains`, `limit` (max 5) |
| `POST /domain-resolution` | Show how a query resolves to concepts and domains |
| `POST /document` | Read an indexed page. Body: `url` or `chunk_id`, optional `neighbors` (0–3, default 1), `max_chars` (500–20000, default 6000) |
| `GET /sources` | Approved search sources |
| `GET /index-status` | AI Search document count and the storage mode. Add `?count_blobs=true` to also count stored chunks, which lists every blob (slow; do not poll it) |

`POST /document` returns the full page for domains in `FULL_TEXT_DOMAINS`. All other domains return an excerpt only (`EXCERPT_MAX_CHARS`) with `excerpt_only: true`. The domain must be approved by the ontology for search (403 otherwise). A page that is not in the index returns 404.

**Crawling**

| Route | Purpose |
|---|---|
| `POST /crawl` | Start jobs. Optional body `{"domains": ["sujeeth.org"]}` |
| `POST /crawl/all` | Start a job for every crawl-enabled domain without an active job |
| `GET /crawl-status` | Recent jobs |
| `GET /crawl-dashboard` | Every crawl-enabled domain with its latest job, and the search index count |
| `GET /crawl/domain-stats` | Per-domain totals, summed from the job counters of the jobs that are still kept. Does not read page rows |
| `GET /crawl/{job_id}` | One job |
| `GET /crawl/{job_id}/pages` | Page records for a job. Optional `?status=failed`, `?limit=N` (up to 10000), or `?summary=true` for counts by status and reason |
| `POST /crawl/{job_id}/cancel` | Stop a job. Queued messages for it finish immediately |
| `POST /crawl/{job_id}/resume` | Resume failed tasks |
| `POST /crawl/{job_id}/reconcile` | Rebuild job counters from page records |
| `POST /crawl/{job_id}/recover` | Requeue stale pages and expired leases. Optional body `{"stale_minutes": 25}` (5–240) |

Note: the status and dashboard routes use a hyphen (`crawl-status`, `crawl-dashboard`) so they do not collide with `crawl/{job_id}`.

**Index maintenance**

| Route | Purpose |
|---|---|
| `POST /index/backfill` | Queue a resumable pass over every stored chunk. Body: optional `mode` and `domains`. `domains` (for example `["sujeeth.org"]`) limits the pass to chunks from those domains, so your own sites can be indexed without waiting for everything else. `missing` (the default) indexes only chunks that are not in the index yet, which makes it cheap to repeat. `all` re-sends every chunk, for example after changing the index schema or vectorizer. Stored vectors are reused, so nothing is embedded twice |
| `POST /index/retag-preview` | Read-only sample of how many stored documents a re-tag would change. Body: optional `domains`, `scan_limit` (max 1000) |
| `POST /index/retag` | Queue a re-tag of stored documents with the current ontology. Updates Blob Storage and merges tags into the index. Uses no embedding quota. Body: optional `domains` |
| `POST /index/purge-preview` | Read-only sample of how many stored chunks fall under the ontology's `exclude_paths` or outside its `include_paths`. Body: optional `domains`, `scan_limit` (max 1000) |
| `POST /index/purge` | Queue a purge of stored chunks under excluded paths or outside an allow-list: removes them from the index and Blob Storage and clears their manifest rows. Body: optional `domains` |

## Scheduled jobs (UTC)

| Function | Schedule | What it does |
|---|---|---|
| `scheduled_crawl_recovery` | Daily 10:00 | Reconciles and recovers unfinished jobs (up to 30 days old). Jobs that finished with errors are retried for 3 days only |
| `scheduled_job_cleanup` | Daily 11:30 | Deletes crawl jobs, and their page rows, that nothing has touched for `JOB_RETENTION_DAYS` (default 30). The newest job of each domain is always kept. At most 20 jobs per run |
| `scheduled_weekly_crawl` | Sundays 08:00 | Starts an incremental crawl for each domain in `WEEKLY_CRAWL_DOMAINS` (default `sujeeth.org,topics.sujeeth.org`; `*` means every crawl-enabled domain). Skips domains that already have an open job. A job nobody has touched for `OPEN_JOB_STALE_DAYS` no longer counts as open |
| `scheduled_index_reconcile` | Daily 11:00 | Queues a `missing`-mode backfill so any chunk that is stored but absent from the index gets indexed. Skipped when the queue already holds more than `INDEX_RECONCILE_MAX_QUEUE` messages or `INDEX_RECONCILE_ENABLED` is false |

Incremental crawls use ETag and Last-Modified conditional requests and content hashes. Unchanged pages are not re-embedded or re-written, but every crawl still creates new job rows and queue messages for every page it visits.

## Settings

Set these under **Function App → Settings → Environment variables**. Keep secrets out of source control.

| Setting | Default | Purpose |
|---|---|---|
| `AzureWebJobsStorage` | none | Queue and Table storage |
| `BLOB_STORAGE_CONNECTION_STRING`, `BLOB_CONTAINER` | none, `web-content` | Chunk storage |
| `CRAWL_QUEUE_NAME`, `CRAWL_POISON_QUEUE_NAME` | `web-crawl-pages`, `web-crawl-pages-poison` | Queues |
| `AzureFunctionsJobHost__extensions__queues__batchSize`, `AzureFunctionsJobHost__extensions__queues__newBatchThreshold` | `1`, `0` (the values in `host.json`) | How many crawl pages one instance works on at once: up to `batchSize` plus `newBatchThreshold` (`batchSize` is at most 32). The total at once is that times the number of running instances, which is set in Azure. An app setting with this name overrides `host.json` without a redeploy; changing it restarts the app |
| `ONTOLOGY_API_URL`, `ONTOLOGY_FUNCTION_KEY` | none | Ontology app |
| `AZURE_SEARCH_ENDPOINT`, `AZURE_SEARCH_API_KEY`, `AZURE_SEARCH_INDEX_NAME`, `AZURE_SEARCH_API_VERSION` | the endpoint, `sf-knowledge-v2` (the live index) and `2025-09-01`; no key | AI Search (admin key) |
| `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_EMBEDDING_DEPLOYMENT`, `AZURE_OPENAI_API_VERSION`, `AZURE_OPENAI_EMBEDDING_DIMENSIONS` | `sf-embeddings`, `v1`, `1536` | Embeddings |
| `MAX_PAGES_PER_DOMAIN` | `10000` | Page cap per domain, the same for every domain. It caps a crawl job's total pages and the page URLs collected from sitemaps. A job keeps the limit it was started with, so after changing this, start a new crawl |
| `MAX_CRAWL_DEPTH` | `5` | Link depth |
| `CHUNK_CHARS`, `MIN_TEXT_CHARS`, `MAX_PAGE_BYTES`, `REQUEST_TIMEOUT_SECONDS`, `CRAWL_DELAY_SECONDS` | `1800`, `80`, `5000000`, `20`, `0.5` | Extraction and politeness |
| `INDEX_BACKFILL_BATCH_SIZE`, `AZURE_SEARCH_BATCH_SIZE` | `10`, `100` | Backfill and upload batch sizes |
| `EMBED_MIN_INTERVAL_SECONDS` | `0` (off) | Minimum seconds between embedding calls on each instance. Protects search from being throttled by indexing |
| `EMBED_BATCH_SIZE` | `16` | Texts per embedding request |
| `SEARCH_MIN_INTERVAL_SECONDS` | `0` (off) | Minimum seconds between index writes on each instance |
| `SEARCH_WRITE_ATTEMPTS` | `8` | Tries per index write when the search service answers 429 or 503. Waits grow from 2 to 60 seconds, about 3 minutes in all |
| `SEARCH_BREAKER_SECONDS` | `120` | After the index stays busy through every retry, other writes on that instance skip the retries for this long and defer at once |
| `INDEX_MAX_DEFERRALS` | `12` | How many times a deferred indexing task is re-queued before it fails normally |
| `AZURE_SEARCH_VECTOR_FIELD` | `contentVector` | The vector field that receives chunk vectors and is searched |
| `AZURE_SEARCH_NULL_VECTOR_FIELDS` | none | Comma-separated old vector fields that are set to null on every chunk write, so a migrated chunk stops using the old field's quota |
| `AZURE_SEARCH_BLOB_BACKED` | `true` | On for the live index, `sf-knowledge-v2` (binary quantization, content not retrievable, vectors not stored). With it on, the index keeps no retrievable text and no stored vector copy: every write replaces the whole document, search snippets and `/document` text are read from Blob Storage, and the retag re-uploads whole documents. Set it to `false` only for an index that keeps retrievable content and a stored vector copy, and never leave it on for one |
| `INDEX_RECONCILE_ENABLED` | `true` | Set to `false` to turn the nightly reconcile off without redeploying |
| `INDEX_RECONCILE_MAX_QUEUE` | `500` | The nightly reconcile is skipped when the queue holds more messages than this |
| `WEEKLY_CRAWL_DOMAINS` | `sujeeth.org,topics.sujeeth.org` | Comma-separated domains for the weekly crawl, or `*` for all. Every other crawl-enabled domain is crawled monthly |
| `JOB_RETENTION_DAYS` | `30` | Crawl jobs and their page rows untouched for this long are deleted by `scheduled_job_cleanup` (minimum 7). The newest job of each domain stays |
| `JOB_CLEANUP_ENABLED` | `true` | Set to `false` to turn the job cleanup off |
| `OPEN_JOB_STALE_DAYS` | `3` | A job with no update for this many days stops blocking a new crawl of its domain |
| `ONTOLOGY_CACHE_SECONDS` | `120` | How long a successful ontology manifest or domain-concept answer is reused. `0` turns the cache off. A failed answer is never cached |
| `FULL_TEXT_DOMAINS` | `sujeeth.org,topics.sujeeth.org` | Domains `/document` may return in full |
| `EXCERPT_MAX_CHARS` | `3000` | Cap for every other domain |

## Operating notes

- **Search 502 with a 429 inside:** the index vectorizer calls the same embedding deployment as indexing. Heavy indexing can throttle query vectorization and break search. Mitigations: set `EMBED_MIN_INTERVAL_SECONDS`, cap the instance count (Flex Consumption: Settings → Scale and concurrency), or give queries their own embedding deployment.
- **Page limits live here, not in the ontology.** Every domain gets `MAX_PAGES_PER_DOMAIN` and `MAX_CRAWL_DEPTH`; there are no per-domain limits. The page limit is also the number of page URLs collected from a domain's sitemaps. The limit applies to every page a job queues (the homepage, sitemap URLs and links found on pages); once a job has found that many pages it queues no new ones. The ontology only decides which domains are crawl-enabled.
- **Permanent failures are skipped, not retried:** off-site redirects, redirect loops, non-public IPs, and HTTP 400/401/403/404/405/406/410/451. Timeouts, 429, and 5xx still retry (up to 5 deliveries).
- **Stale-page cleanup** only runs after a clean crawl: no skipped or failed pages and below the page cap. Because skipped pages block it, domains with dead links will not purge removed pages.
- **Re-tagging:** tags are assigned at crawl time, and unchanged pages are not re-tagged on later crawls. After an ontology alias or domain-concept change, run `index/retag-preview`, then `index/retag`. The resolver caches the ontology in memory, so restart the app after an ontology deploy.
- **Path rules fail closed.** The crawler needs the ontology's `crawl_policy` rules (`include_paths`, `exclude_paths`, `include_sitemaps`) before it fetches a page or queues a link. If it has never been able to read them, or the ontology answers with a manifest that lists no crawl domains, page tasks fail and retry instead of crawling unfiltered. Once rules have been read, a later failed or empty answer keeps the previous rules.
- **Excluded paths.** A domain's `crawl_policy.exclude_paths` in the ontology (for example TealHQ's `/job` and `/jobs`) stops the crawler from queuing or fetching those URLs. This covers sitemap URLs, discovered links, and pages already waiting in the queue. Excluded pages are not counted as skipped, so they do not block stale-page cleanup. The rules are cached for 10 minutes, so after changing them, restart the app, then run `index/purge-preview` and `index/purge` to remove what is already stored. Run the purge before a backfill, or the backfill will re-index the excluded pages from Blob Storage.
- **Allowed paths.** A domain's `crawl_policy.include_paths` (for example `["/career-advice"]`) limits the crawl to those sections. It uses the same rule format as `exclude_paths`, and the two combine: with an allow-list, only matching paths are crawled, and `exclude_paths` still wins (allow `/career-advice`, block `/career-advice/news`). The bare homepage (no query string) is always allowed, because the crawl starts there to find sitemaps and links; its query variants such as `/?from=nav` are not. A bare `/` is ignored in either list. Without `include_paths`, nothing is limited. Deploy this code before adding `include_paths` to the ontology: older code ignores the key and would crawl the whole site. `index/purge-preview` and `index/purge` also remove stored chunks outside an allow-list, and `blob_paths_report.py` flags them.
- **Vectors are stored and a busy index never fails a crawl.** Each chunk is embedded once, and its vector is saved as `vectors/<id>.json` next to the chunk. If the search service stays busy (429 or 503) through every retry, the page is still marked processed (`index_deferred`), and a delayed `index_blob_batch` task indexes the stored chunks later: after 2, 5, 10, 20, and 30 minutes, then hourly, up to `INDEX_MAX_DEFERRALS` times. Nothing is fetched or embedded again. A page whose indexing is deferred is missing from search until that task succeeds, and `POST /api/index/backfill` indexes anything still stored but missing.
- **Backfill** compares Blob Storage with the index and, in the default `missing` mode, indexes only the chunks that are absent. It is safe to run again whenever you suspect a gap, such as after a long index outage. Use `mode: all` only to rebuild the whole index. Chunks that already have a stored vector are not embedded again, so only old chunks without one pay for embeddings the first time. Watch `index-status`, the `Index batch mode=… already_indexed=… indexed=…` log lines, and the poison queue.
- **Index capacity.** The search index has hard quotas (on the Serverless tier, 300 MB of vector index and 1 GB of storage per index). At 1,536 dimensions with no compression, each chunk used about 18 KB of vector quota and 40 KB of storage, so about 17,000 chunks filled it, and once a quota is full, writes fail. Quantization can only be set on a new vector field, so the compressed index is a new one: `sf-knowledge-v2`, now the live index (`AZURE_SEARCH_INDEX_NAME`). It was created in the portal (Indexes, Add index, JSON) from a definition file that was not saved in this repo. To keep a copy, read the definition back with `GET {endpoint}/indexes/sf-knowledge-v2?api-version=2025-09-01` (admin key in the `api-key` header), and drop its `@odata.context` and `@odata.etag` keys before using it to create an index. The definition is blob-backed: binary quantization, no stored vector copy, and `content` searchable but not retrievable, because Blob Storage holds every chunk's text and vector. Run the app with `AZURE_SEARCH_BLOB_BACKED=true` against such an index and never against one with stored copies turned on. To measure capacity, divide an index's vector index size and total storage size (portal: Indexes) by its document count, then divide each quota by that per-chunk cost. Judge it at 1,000 chunks or more, because the first few hundred overstate the cost.
- **Logging.** The Azure SDKs are set to log warnings and errors only. At INFO they write two multi-line records for every storage or table call (each blob read shows up as an HTTP 206 range request), which floods the logs and inflates the Log Analytics bill. The app's own messages (`Purge batch …`, `Index batch …`, `Search index busy …`) are still logged.
- **Queue messages are at-least-once.** Writes use deterministic IDs.

## Setup for scripts

```powershell
$azureUrl = "https://<your-websearch-app>.azurewebsites.net"
$key      = Read-Host "Function key" -AsSecureString
$headers  = @{ "x-functions-key" = [System.Net.NetworkCredential]::new("", $key).Password }

function Call($method, $path, $body = $null) {
    $p = @{ Method = $method; Uri = "$azureUrl$path"; Headers = $headers; ContentType = "application/json" }
    if ($body) { $p.Body = ($body | ConvertTo-Json -Depth 5) }
    try { Invoke-RestMethod @p }
    catch { Write-Warning "$method $path -> $($_.Exception.Message)"; $_.ErrorDetails.Message }
}
```

For day-to-day crawl work, load `crawl-helper.ps1` instead (see Helper scripts). It does this setup for you.

Common calls:

```powershell
Call GET  "/api/health"
Call GET  "/api/crawl-dashboard"
Call POST "/api/crawl" @{ domains = @("sujeeth.org") }
Call POST "/api/search" @{ query = "how can I donate"; concepts = @("donation"); domains = @("sujeeth.org"); limit = 3 }
Call POST "/api/document" @{ url = "https://www.sujeeth.org/"; max_chars = 3000 }
Call POST "/api/index/retag-preview" @{ domains = @("sujeeth.org") }
```

## Helper scripts

These scripts sit next to the app and call it, or read the same storage and rules. They are part of the project:
change them in the same commit as the thing they depend on.

| Helper | What it does | Depends on |
|---|---|---|
| `crawl-helper.ps1` | Table views and shortcuts for the crawl API: `Show-Crawl` (one row per domain, with a TOTAL row), `Show-CrawlPages`, `Show-CrawlPaths` (with a TOTAL row), `Show-CrawlTimeline`, `Start-Crawl`, `Wait-Crawl`, `Resume-Crawl`, `Stop-Crawl`, `Get-CrawlJobId`, and `Call` for any raw request. Load it with `. .\crawl-helper.ps1` | `GET /health`, `GET /crawl-dashboard` (the `latest_job` row of each domain), `POST /crawl` (its `jobs` and `skipped` lists), `GET /crawl/{job_id}/pages` (`?summary=true`, `?status=`, `?limit=`; fields `url`, `status`, `reason`, `attempts`, `error`, `created_utc`, `updated_utc`), `POST /crawl/{job_id}/cancel` and `/resume` |
| `Add-ManualPage.ps1` | Adds, replaces or removes one page by URL (`-Preview`, `-FromClipboard`, `-Delete`) | `POST /index/page`, `POST /index/page/delete`; the domain must be `search_enabled` and `manual_only` in the ontology |
| `check_sitemap.py` | Shows what the crawler would collect from a domain's sitemaps and where the page limit lands. Read-only | Copies `services/sitemap.py` `collect_sitemap_urls` (collection order, `MAX_SITEMAPS_PER_DOMAIN`, `MAX_SITEMAP_BYTES`, each URL counted once, the crawler user agent) and reads `MAX_PAGES_PER_DOMAIN` like `services/config.py` |
| `blob_paths_report.py` | Counts stored chunks by domain and path section and flags any the crawl rules would not allow. Exits 1 if any are out of scope | Blob Storage (`BCONN`); `services/path_rules.py` |
| `request.json` | A sample body for `POST /search` | The `/search` request fields |

**Keeping helpers in step.** Before you change a route, a response field, a setting, a queue name, a stored
record, or a module that a helper imports or copies, read this table. For every helper it names: update the
helper, its comments and its usage text, run it or its tests, and commit it with the change. A route's response
fields are a contract with `crawl-helper.ps1`. When you add a helper, add a row here; when you delete one, remove
its row and any mention of it in this file.

## Deploy

1. Push to `main`. The GitHub Actions workflow builds and deploys.
2. Wait for the run to finish, then check `GET /api/health`: `ai_search_configured` and `embedding_configured` should be `true`.
3. If the ontology changed, deploy it first, then restart this app.
4. Confirm both timers appear under **Functions**.

## Tests

```powershell
pip install -r requirements.txt
python -m pytest -q tests
```

## Known limitations

- No JavaScript rendering. PDF and DOCX extraction is text-only (no OCR).
- Tagging is deterministic (names and aliases), so untagged chunks are not searchable by concept.
- Job counters are best-effort under heavy concurrency; use `reconcile` after partial failures.
- The public-IP check is defense in depth, not a replacement for network egress controls.
- Re-tag runs read every blob, and so does `GET /api/index-status?count_blobs=true`; avoid calling either in a loop.

## Version history (short)

- 0.5.0: persistent page-level crawl jobs on queues and tables.
- 0.6.x: page claims, leases, reconcile, and recover.
- 0.7.0: Azure AI Search hybrid retrieval, backfill.
- 0.7.x reliability patch: job rows carry `job_id` (the daily recovery timer reads it), one job-start path with an open-job check for the manual, bulk and scheduled crawls, a deleted page (404/410) leaves the index, robots.txt is cached per host, and the nightly reconcile checks the index before it reads blobs.
- 0.7.x: the page limit comes only from `MAX_PAGES_PER_DOMAIN`; `MAX_PAGES_BY_DOMAIN` is removed and ignored if still set.
- 0.7.x memory patch: the old crawler and the `/refresh`, `/enrich` and `/reindex` routes are removed; page, manifest and job rows are read a page at a time; `crawl/domain-stats` sums job counters; `crawl/{job_id}/pages` takes `status`, `limit` and `summary`; `scheduled_job_cleanup` deletes old jobs; ontology answers are cached briefly; every tunable setting is read in `services/config.py`.
- After 0.7.0: embedding retry and throttle; permanent failures skipped; explicit-domain validation fix; scheduled recovery and weekly crawl; re-tag and document routes; app-wide page limits.