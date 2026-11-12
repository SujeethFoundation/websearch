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
| `SEARCH_BACKEND` | `azure` | `azure`: every search goes to Azure AI Search (as before). `shadow`: Azure AI Search answers and a sample of searches also runs in the app and is compared. `local`: the app answers from memory. See "Search inside the app" |
| `AI_SEARCH_WRITES` | `true` | Whether chunks are still written to and deleted from Azure AI Search. Turn off once nothing needs the index |
| `LOCAL_INDEX_WRITE` | `false` | Also write every chunk to the Table Storage table the local index reads. Turn on before the backfill |
| `LOCAL_INDEX_CONNECTION_STRING`, `LOCAL_INDEX_TABLE` | the blob connection string, `searchchunks` | Where the table lives (letters and digits only). A small `searchchunksmeta` table holds the version row |
| `LOCAL_INDEX_TEXT_IN_MEMORY` | `true` | Keep chunk text in memory (fast), or read the few chunks a search returns from the table (about a third of the memory) |
| `LOCAL_INDEX_POLL_SECONDS`, `LOCAL_INDEX_MIN_RELOAD_SECONDS` | `60`, `600` | How often an instance checks the version row, and the least time between rebuilds |
| `LOCAL_INDEX_TITLE_WEIGHT` | `3` | How much a title word counts against a body word in keyword scoring. Used when a chunk is written, so changing it needs a new backfill |
| `LOCAL_INDEX_PRELOAD` | `true` | Load the index when the worker starts (only when the backend is `local` or `shadow`) |
| `LOCAL_SEARCH_FALLBACK` | `true` | When the local index is not ready, answer from Azure AI Search instead of failing |
| `LOCAL_SHADOW_SAMPLE` | `0` | Share of searches (0 to 1) that shadow mode also runs locally and compares |
| `EMBED_QUERY_COOLDOWN_SECONDS` | `20` | After a search question fails to embed, skip the embedding endpoint for this long and search by keywords only, so an outage costs one timeout, not one per search. `0` turns it off. Answers found by keywords alone are never kept in the result cache |

## Search inside the app (Table Storage)

Azure AI Search on the Serverless tier meters every query. This option answers searches inside the app instead, so a
search costs CPU on the instance that runs it. Nothing changes until you set `SEARCH_BACKEND`.

**How it works.** Each chunk is one row of a Table Storage table (`PartitionKey` is the page id, `RowKey` the chunk
number). The row holds the title, url, domain, concepts, the chunk text, the embedding reduced to one bit per dimension
(192 bytes), and the chunk's words as hashes with counts, worked out once when the row is written. Every instance scans
the table into memory (a few seconds), then answers a search by: keeping the chunks in the listed domains that carry one
of the concepts; BM25 over the question's words (any word may match), best 50; Hamming distance between the question's
bit vector and each chunk's, nearest 50; and reciprocal rank fusion of the two lists. A one-row table holds a random
version token that writers change after they write; instances check it every `LOCAL_INDEX_POLL_SECONDS` and rebuild in
the background, swapping the new index in. A failed rebuild keeps the old index. `document` reads (`fetch_chunks`) are
answered from the same index.

The question's vector now comes from an Azure OpenAI call made by this app (the search service's vectorizer used to make
it). It uses the same deployment, skips the indexing throttle, gives up after 20 seconds and remembers the last 2,000
questions. If it fails, the search falls back to keywords only (`search_mode` is `keyword`).

**Moving over, in this order**
1. Set the instance size (2,048 MB), deploy, and set `LOCAL_INDEX_WRITE=true`. New and changed chunks now go to both places.
2. Copy what is already stored: `POST /api/index/backfill` with `{"mode": "local"}`. It runs on the queue like the other
   backfills, resumes by itself, reuses stored vectors, and does not touch Azure AI Search. Check `/api/health` for `local_index.chunks`.
3. Set `SEARCH_BACKEND=shadow` and `LOCAL_SHADOW_SAMPLE=0.1`. Azure AI Search still answers; `/api/health` reports
   `local_index.shadow` (`average_overlap` of the top results and `top_result_agrees`). Compare on real questions before going on.
4. Set `SEARCH_BACKEND=local`. If the local index is not ready, searches fall back to Azure AI Search (see `fallbacks_to_ai_search`).
5. Later, set `AI_SEARCH_WRITES=false` to stop writing to Azure AI Search. A table write failure is then raised, so the queue retries it.

`POST /api/local-index/reload` rebuilds this instance's index now and returns how long it took.
`GET /api/local-index/status` reports what this instance has loaded; with `?count=true` it also counts the table's rows
(in all and per domain, a keys-and-domains read that takes a few seconds on a large table) and the messages waiting on the
queue, which is how a backfill is followed.

**Question cache.** Turning a question into a vector is a network call (often 150 ms or more) and the only slow step of a local
search. Each question is now embedded once, not once per instance: an instance looks in its own memory (20,000 questions,
`EMBED_CACHE_MEMORY_ENTRIES`), then in a table every instance shares (`EMBED_CACHE_TABLE`, default the index table's name
plus `qcache`), and only then calls the embedding service (with its single flight and cooldown, `EMBED_QUERY_*`). Only the
question's 1-bit code is kept, 192 bytes for 1536 dimensions, never the question's text; the row key is a hash that
includes the embedding model and its size, so changing the model misses by itself. Questions are matched ignoring case,
punctuation and extra spaces ("How do I volunteer?" is "how do i volunteer"). Entries older than `EMBED_CACHE_TTL_DAYS` (30;
0 keeps them) are ignored and replaced. If the table is slow or failing, it is skipped for `EMBED_CACHE_COOLDOWN_SECONDS` (30)
so a search never waits on it for long, and each table call is limited to `EMBED_CACHE_TIMEOUT_SECONDS` (2). Turn the shared
table off with `EMBED_CACHE_SHARED=false` (memory only). Questions already remembered are still answered while the embedding
endpoint is paused after a failure. `/api/health` and `/api/local-index/status` report `embed_cache`: lookups, the share answered
without an embedding call, where each was answered, embedding calls and their average time, and the table's errors.
`POST /api/embed-cache/seed` with `{"questions": [...]}` (up to 500) embeds questions ahead of time, so common ones are never
waited for. The table grows by about 200 bytes per distinct question and is not purged; delete it any time.

**From PowerShell** (`. .\crawl-helper.ps1`): `Show-LocalIndex -Count` shows the settings in force, what one instance has
loaded, and the table's rows per domain (`-CountBlobs` adds the stored chunks, slowly, to show how much is copied).
`Start-LocalBackfill` (or `Start-LocalBackfill muse` for one domain) asks first, queues the copy and refuses to run if
`LOCAL_INDEX_WRITE` is off; `-Follow` hands over to `Watch-LocalBackfill`, which shows rows, rows added, rows per minute and
the queue backlog, and stops by itself once the table stops growing and the queue is empty. `Seed-EmbedCache` seeds the question cache from a list or a file (one question per line). `Watch-Websearch` is a health
watcher: every poll shows the instance's uptime and process id (a restart is flagged), whether the index is loaded and its
size, process memory, searches with the average embedding and in-memory search time, fallbacks to Azure AI Search, the
shadow comparison, the result cache hit rate, keyword-only searches and the question cache hit rate; on Ctrl+C (or after `-Polls N`) it prints a summary with checks. The bars for the
checks (`-InstanceMb`, `-MinCompared`, `-MinOverlap`, `-MinTop`) are yours to choose.

**Memory (measured on synthetic chunks, per 40,000 chunks).** Steady: about 230 MB with text in memory, about 90 MB without.
While a rebuild runs there is a peak about 400 MB higher, with the old index still in use. `/api/health` shows
`local_index.process_memory_mb`. As a rule, 2,048 MB instances are comfortable up to about 60,000 chunks with text in memory;
beyond that set `LOCAL_INDEX_TEXT_IN_MEMORY=false` (each search then reads about five table rows, and a read costs a fraction of a cent per 10,000), or use 4,096 MB.

**The table as the index of record.** When `SEARCH_BACKEND=local`, or `AI_SEARCH_WRITES=false`, the admin reads use the
table too: `index/tags` (exact counts, no sampling, in every form: totals, `?domain=`, `?tag=`, `?url=`), `index-status` and the
nightly reconcile (the document count is read from the table, keys and domains only, so it takes a few seconds), and
`index/backfill` in `missing` mode (it asks the table which chunks it already holds). Re-tagging and manual page adds
write to the table. With these in place nothing in the app needs Azure AI Search once `AI_SEARCH_WRITES=false`. The
Compute Unit and query-tuning settings apply to Azure AI Search only.

**Not the same as Azure AI Search.** Keyword scoring is BM25 over lower-cased words with Lucene's English stop words
removed and no stemming; the title counts `LOCAL_INDEX_TITLE_WEIGHT` times. Vectors are compared as one bit per dimension
without rescoring. Rankings will differ somewhat, which is what shadow mode measures.

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

`tests/test_local_index_table_sdk.py` runs the local index against a real Table Storage endpoint and is skipped unless
`LOCAL_INDEX_TEST_CONNECTION` is set. The Azurite emulator works (`npm install -g azurite`, `azurite-table --silent --location azurite-data`,
then set the variable to its connection string); the file's header has the exact command.

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