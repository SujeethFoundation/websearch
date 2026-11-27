# Sujeeth Foundation Web Search — v0.7.x

Azure Functions app (Python v2) that crawls ontology-approved websites, stores the text in Blob Storage, indexes it in a Table Storage table, and serves ontology-gated hybrid search (keywords and vectors) from memory to the MCP server and other clients.

## Platform architecture

The Sujeeth Foundation helps college students network into their first job through Career Circles, a six-week mentorship program. Students, mentors and partners use two front ends, the static site at sujeeth.org and a Power Pages portal, both on one Dataverse environment. Azure Function apps add chat, knowledge search, payments, short links and texting. The Dataverse side (Base Camp, circles, book requests, surveys, credentials, the Career Ascent course) ships as Dataverse solutions and has no repo.

| Repo (README) | Role |
|---|---|
| [`sujeethfoundation.github.io`](https://github.com/SujeethFoundation/sujeethfoundation.github.io/blob/main/README.md) | Website at sujeeth.org, wiki, Discussions, course player and Power Pages CSS |
| [`chat`](https://github.com/SujeethFoundation/chat/blob/main/README.md) | Chat API behind the site's assistant widget |
| [`mcp`](https://github.com/SujeethFoundation/mcp/blob/main/README.md) | MCP server (the "Sujeeth Foundation Graph") that AI assistants use to search Foundation knowledge |
| [`ontology`](https://github.com/SujeethFoundation/ontology/blob/main/README.md) | Concept graph and governance: concepts, approved domains, Feelings API |
| [`websearch`](https://github.com/SujeethFoundation/websearch/blob/main/README.md) | Crawler and hybrid search over ontology-approved websites |
| [`wikisearch`](https://github.com/SujeethFoundation/wikisearch/blob/main/README.md) | Keyword search over the GitHub wiki |
| [`payments`](https://github.com/SujeethFoundation/payments/blob/main/README.md) | PayPal checkout and webhook for online courses |
| [`shortner`](https://github.com/SujeethFoundation/shortner/blob/main/README.md) | Link shortener for go.sujeeth.org |
| [`acs-sms-bot`](https://github.com/SujeethFoundation/acs-sms-bot/blob/main/readme.md) | Texting through Azure Communication Services |

Architecture docs, in the website repo's `architecture/` folder: [platform architecture](https://github.com/SujeethFoundation/sujeethfoundation.github.io/blob/main/architecture/platform-architecture.md) (repos, Azure apps, how they connect), [data model and portal](https://github.com/SujeethFoundation/sujeethfoundation.github.io/blob/main/architecture/platform-data-model-and-portal.md) (Dataverse tables, solutions, Power Pages) and [business architecture](https://github.com/SujeethFoundation/sujeethfoundation.github.io/blob/main/architecture/business-architecture.md) (vision, assumptions, tests). This block is the same in every repo's README; the canonical copy is in the platform architecture doc.

**This repo:** called by the `mcp` server and its admin scripts. It reads the ontology's crawl and search manifests to decide what to crawl and search.

## How it fits together

| Piece | Role |
|---|---|
| Ontology app | Decides which domains may be crawled or searched and which concepts a query maps to. Crawl size limits are **not** in the ontology. |
| Blob Storage (`documents/`, `vectors/`) | Durable copy of every chunk (JSON) and its embedding. The index table can be rebuilt from it. |
| Table Storage | Crawl jobs, page state, and the cross-crawl URL manifest, and the index table (`searchchunks`): one row per chunk, which every instance reads into memory and searches itself. |
| Queue Storage | One message per page to crawl, plus backfill and re-tag tasks. Failed tasks go to the poison queue. |
| Azure OpenAI / Foundry (`sf-embeddings`) | Embeds chunks at indexing time and each new search question (answers are remembered, see Search). |

The MCP server resolves the question through the ontology and sends the matched concepts and approved domains with each search request. Websearch uses those supplied values to filter indexed results and does not classify questions or authorize domains.

## Endpoints

All routes require a Function key and are under `/api`.

**Search and content**

| Route | Purpose |
|---|---|
| `GET /health` | Version, configuration flags, the index, caches and the process |
| `GET /search?q=` and `POST /search` | MCP-filtered hybrid search. POST body: `query`, MCP-supplied `concepts` and `domains`, optional `queries` (up to five variants searched together), and `limit` (default 5, at most `SEARCH_MAX_LIMIT`, default 20). Batch results include `for_query` on each result. Websearch does not resolve concepts or approve domains during a search request |
| `POST /domain-resolution` | Show how a query resolves to concepts and domains |
| `POST /search/explain` | Why the search ranks a question as it does and where the page it should find stands. Body: `query`, `expect` (pieces of the right page's address), optional `concepts`, `domains`, `ranking` |
| `GET /local-index/status`, `POST /local-index/reload` | What this instance has loaded (`?count=true` also counts the table's rows and the queue); rebuild this instance's index now |
| `GET /index/snapshot`, `POST /index/snapshot` | What this instance did with the prebuilt index snapshot and what the container holds (`current.matches_table` says whether it is current). `POST` reads the table on this instance and saves a new snapshot in the background |
| `POST /embed-cache/seed` | Embed common questions ahead of time (see Search) |
| `POST /document` | Read an indexed page. Body: `url` or `chunk_id`, optional `neighbors` (0–3, default 1), `max_chars` (500–20000, default 6000) |
| `GET /sources` | Approved search sources |
| `GET /index-status` | The number of chunks in the index table and what this instance has loaded, plus the storage mode. Add `?count_blobs=true` to also count stored chunks, which lists every blob (slow; do not poll it) |

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
| `POST /index/backfill` | Queue a resumable pass over every stored chunk. Body: optional `mode` and `domains`. `domains` (for example `["sujeeth.org"]`) limits the pass to chunks from those domains. `missing` (the default) writes only the chunks the table does not hold yet, which makes it cheap to repeat. `all` rewrites every chunk, for example to add the 8-bit vectors or after a change to how chunks are analysed (`local` is accepted as a name for `all`). Stored vectors are reused, so only chunks without one are embedded |
| `POST /index/retag-preview` | Read-only sample of how many stored documents a re-tag would change. Body: optional `domains`, `scan_limit` (max 1000) |
| `POST /index/retag` | Queue a re-tag of stored documents with the current ontology. Updates Blob Storage and rewrites the chunks' rows with the new tags. Uses no embedding quota. Body: optional `domains` |
| `POST /index/purge-preview` | Read-only sample of how many stored chunks fall under the ontology's `exclude_paths` or outside its `include_paths`. Body: optional `domains`, `scan_limit` (max 1000) |
| `POST /index/purge` | Queue a purge of stored chunks under excluded paths or outside an allow-list: removes them from the index table and Blob Storage and clears their manifest rows. Body: optional `domains` |

## Scheduled jobs (UTC)

| Function | Schedule | What it does |
|---|---|---|
| `scheduled_crawl_recovery` | Every 30 minutes | Reconciles and recovers unfinished jobs (up to 30 days old). Jobs that finished with errors are retried for 3 days only |
| `scheduled_job_cleanup` | Daily 11:30 | Deletes crawl jobs, and their page rows, that nothing has touched for `JOB_RETENTION_DAYS` (default 30). The newest job of each domain is always kept. At most 20 jobs per run |
| `scheduled_weekly_crawl` | Sundays 08:00 | Starts an incremental crawl for each domain in `WEEKLY_CRAWL_DOMAINS` (default `sujeeth.org,topics.sujeeth.org`; `*` means every crawl-enabled domain). Skips domains that already have an open job. A job nobody has touched for `OPEN_JOB_STALE_DAYS` no longer counts as open |
| `scheduled_monthly_crawl` | 1st of the month, 09:00 | Starts an incremental crawl for every crawl-enabled domain that is not in `WEEKLY_CRAWL_DOMAINS`. Does nothing when the weekly list is `*` |
| `scheduled_index_reconcile` | Daily 11:00 | Queues a `missing`-mode backfill so any chunk that is stored but absent from the index table gets written, when the table holds clearly fewer chunks than Blob Storage. Skipped when the queue already holds more than `INDEX_RECONCILE_MAX_QUEUE` messages or `INDEX_RECONCILE_ENABLED` is false |

Incremental crawls use ETag and Last-Modified conditional requests and content hashes. Unchanged pages are not re-embedded or re-written, but every crawl still creates new job rows and queue messages for every page it visits.

## Settings

Set these under **Function App → Settings → Environment variables**. Keep secrets out of source control.

| Setting | Default | Purpose |
|---|---|---|
| `AzureWebJobsStorage` | none | Queue and Table storage |
| `RELATIONSHIP_EFFECTS` | `true` | Let ontology relationships expand, boost or suppress (see Search). `false` keeps only the original `illustrates` expansion |
| `BLOB_STORAGE_CONNECTION_STRING`, `BLOB_CONTAINER` | none, `web-content` | Chunk storage |
| `CRAWL_QUEUE_NAME`, `CRAWL_POISON_QUEUE_NAME` | `web-crawl-pages`, `web-crawl-pages-poison` | Queues |
| `AzureFunctionsJobHost__extensions__queues__batchSize`, `AzureFunctionsJobHost__extensions__queues__newBatchThreshold` | `1`, `0` (the values in `host.json`) | How many crawl pages one instance works on at once: up to `batchSize` plus `newBatchThreshold` (`batchSize` is at most 32). The total at once is that times the number of running instances, which is set in Azure. An app setting with this name overrides `host.json` without a redeploy; changing it restarts the app |
| `ONTOLOGY_API_URL`, `ONTOLOGY_FUNCTION_KEY` | none | Ontology app. The concept resolver reads its slim catalog (`/api/ontology/catalog`); the crawl and search manifests and domain approval come from their own routes |
| `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_EMBEDDING_DEPLOYMENT`, `AZURE_OPENAI_API_VERSION`, `AZURE_OPENAI_EMBEDDING_DIMENSIONS` | `sf-embeddings`, `v1`, `1536` | Embeddings |
| `MAX_PAGES_PER_DOMAIN` | `10000` | Page cap per domain, the same for every domain. It caps a crawl job's total pages and the page URLs collected from sitemaps. A job keeps the limit it was started with, so after changing this, start a new crawl |
| `MAX_CRAWL_DEPTH` | `5` | Link depth |
| `CHUNK_CHARS`, `MIN_TEXT_CHARS`, `MAX_PAGE_BYTES`, `REQUEST_TIMEOUT_SECONDS`, `CRAWL_DELAY_SECONDS` | `1800`, `80`, `5000000`, `20`, `0.5` | Extraction and politeness |
| `INDEX_BACKFILL_BATCH_SIZE` | `50` | Chunks per backfill task (at most 100) |
| `EMBED_MIN_INTERVAL_SECONDS` | `0` (off) | Minimum seconds between embedding calls on each instance. Protects search questions from being throttled by indexing |
| `EMBED_BATCH_SIZE` | `16` | Texts per embedding request |
| `SEARCH_BREAKER_SECONDS` | `120` | After the index table stays busy or unreachable, other writes on that instance skip straight to deferral for this long |
| `INDEX_MAX_DEFERRALS` | `12` | How many times a deferred indexing task is re-queued before it fails normally |
| `INDEX_RECONCILE_ENABLED` | `true` | Set to `false` to turn the nightly reconcile off without redeploying |
| `INDEX_RECONCILE_MAX_QUEUE` | `500` | The nightly reconcile is skipped when the queue holds more messages than this |
| `WEEKLY_CRAWL_DOMAINS` | `sujeeth.org,topics.sujeeth.org` | Comma-separated domains for the weekly crawl, or `*` for all. Every other crawl-enabled domain is crawled monthly |
| `JOB_RETENTION_DAYS` | `30` | Crawl jobs and their page rows untouched for this long are deleted by `scheduled_job_cleanup` (minimum 7). The newest job of each domain stays |
| `JOB_CLEANUP_ENABLED` | `true` | Set to `false` to turn the job cleanup off |
| `OPEN_JOB_STALE_DAYS` | `3` | A job with no update for this many days stops blocking a new crawl of its domain |
| `ONTOLOGY_CACHE_SECONDS` | `120` | How long a successful ontology manifest or domain-concept answer is reused. `0` turns the cache off. A failed answer is never cached |
| `FULL_TEXT_DOMAINS` | `sujeeth.org,topics.sujeeth.org` | Domains `/document` may return in full |
| `EXCERPT_MAX_CHARS` | `3000` | Cap for every other domain |
| `LOCAL_INDEX_CONNECTION_STRING`, `LOCAL_INDEX_TABLE` | the blob connection string, `searchchunks` | Where the index table lives (letters and digits only). A small `searchchunksmeta` table holds the version row |
| `LOCAL_INDEX_TEXT_COMPRESS` | `true` | Keep the in-memory chunk text compressed (zlib, one chunk at a time, about half the size). Only the chunks a search reads are unpacked (about 10 microseconds each). `false` keeps plain UTF-8. Changing it rebuilds the snapshot. The plain constructor (`LOCAL_INDEX_LEAN_BUILD=false`) holds strings and packs on publish |
| `LOCAL_INDEX_TEXT_IN_MEMORY` | `true` | Keep chunk text in memory (fast), or read the few chunks a search returns from the table (about a third of the memory). The phrase bonus needs it |
| `LOCAL_INDEX_POLL_SECONDS`, `LOCAL_INDEX_MIN_RELOAD_SECONDS` | `60`, `600` | How often an instance checks the version row, and the least time between rebuilds |
| `LOCAL_INDEX_TITLE_WEIGHT` | `3` | How much a title word counts against a body word in keyword scoring. Used when a chunk is written, so changing it needs a backfill with `mode: all` |
| `LOCAL_INDEX_PRELOAD` | `true` | Load the index when the worker starts, not at the first search |
| `LOCAL_POSTING_WEIGHT_DTYPE` | `float16` | How the keyword weights are stored in memory (`float16` or `float32`). `float16` halves that array and changes a BM25 score by about 0.1%. Applies to the next index build; a snapshot built earlier keeps its own type |
| `LOCAL_POSTING_MAX_DF` | `0` (off) | Words found in more than this share of the chunks (`0.3` = 30%) get no keyword postings. They score near zero anyway and are the largest lists. Check `tests/relevance_eval.py` before turning it on. Applies to the next index build |
| `LOCAL_INDEX_PRELOAD_ON_HTTP` | `true` | `true`: do not preload the index when the worker starts; the first HTTP request starts the load. Queue and timer instances then never carry the index |
| `LOCAL_INDEX_DROP_BEFORE_RELOAD` | `true` | `true`: a reload (from the snapshot or a lean build) frees the old index first, so the instance never holds two. Searches in the few seconds the load takes wait, or fail fast with a 503 and `Retry-After` when `LOCAL_INDEX_FAIL_FAST` is on |
| `LOCAL_INDEX_LEAN_BUILD` | `true` | `true`: a build from the table reads it twice and streams into arrays of the final size (`services/lean_index.py`), instead of holding every row first. The index and its answers are identical; the build needs about 35 MB beyond the finished index instead of about 200 MB (measured on 20,000 chunks). It reads the table twice (a read costs a fraction of a cent per 10,000 rows). A row added between the two reads waits for the next reload; a row deleted between them restarts the build (up to 3 tries) |
| `LOCAL_INDEX_SNAPSHOT_ONLY` | `false` | `true`: loads and reloads that the app starts by itself use only a current snapshot and never scan the table. Without a current snapshot a reload is skipped and the old index stays; a first load fails with a message. Only for an app whose snapshots are built elsewhere (`tools/build_snapshot.py`, `POST /api/index/snapshot`); with `LOCAL_INDEX_LEAN_BUILD` the app can build its own, so you do not need it |
| `LOCAL_INDEX_RELOAD_JITTER_SECONDS` | `300` | After an instance sees a changed table it waits a random 0 to this many seconds before reloading, so two instances are not both reloading at once. Set `0` to turn the stagger off |
| `LOCAL_INDEX_FAIL_FAST`, `LOCAL_INDEX_RETRY_AFTER_SECONDS` | `true`, `3` | An instance answers searches only when it holds the index. While it loads (at start, or during a reload with `LOCAL_INDEX_DROP_BEFORE_RELOAD`), `/search` and `/document` answer 503 with `Retry-After` at once, and no request loads the index itself: the first request, however early, starts the load on a background thread and is refused too. The MCP app retries that. `/health` shows `local_index.loaded` |
| `SEARCH_MAX_CONCURRENT` | `4` | Searches and document reads one instance runs at once (`/search`, `/search/explain`, `/document`). `0` turns the limit off. Search is CPU work, so more at once only makes each one slower |
| `SEARCH_QUEUE_TIMEOUT_SECONDS`, `SEARCH_BUSY_RETRY_AFTER_SECONDS` | `10`, `1` | The longest a request waits for one of those slots. After that it is refused with HTTP 503, `stage: search_queue_timeout` and `Retry-After`, and the MCP app retries it. `0` refuses at once when no slot is free. `/health` shows `admission` (running, waiting, refused, wait times) |
| `HEARTBEAT_SECONDS`, `SLOW_SEARCH_SECONDS` | `15`, `5` | The log lines that explain a restart: `process_start` (pid, instance, memory limit) and `process_exit` once per worker; `heartbeat` at most every 15 s, sent by whichever request arrives (the host only forwards log lines from request threads), while the instance is busy, loading or recovering (every 5 min when idle); `slow_search` also carries pid, instance and memory with memory, CPU, CPU throttling, searches running and waiting, 503s since the last beat (`loading_503`, `queue_503`) and the index state; `slow_search` for any search or document read over 5 s. A worker killed for memory writes no exit line: look for a heartbeat that stops and a new `process_start` with another pid. `0` turns off the heartbeat / slow-search line. Per-request logging is off |
| `INDEX_SNAPSHOT_ENABLED`, `INDEX_SNAPSHOT_AUTOBUILD` | `true`, `true` | Read a prebuilt copy of the index from Blob Storage instead of scanning the table, and save one after a table build (see Search). `false` for the first turns snapshots off entirely; `false` for the second leaves building to `POST /api/index/snapshot` |
| `INDEX_SNAPSHOT_PREFIX`, `INDEX_SNAPSHOT_CONCURRENCY`, `INDEX_SNAPSHOT_KEEP_SECONDS` | `index-snapshot`, `4`, `3600` | Where in the blob container the snapshot lives (nothing else lists that prefix), parallel range requests per blob, and how long older builds are kept before they are deleted |
| `LOCAL_INDEX_STOPWORDS` | `none` | `english` drops 33 common words from questions (not from chunks) |
| `SEARCH_VECTOR_K`, `LOCAL_TEXT_RECALL` | `0` (50), `0` (1,000) | How many vector and keyword results go into the rank fusion |
| `LOCAL_RESCORE`, `LOCAL_RESCORE_POOL` | `true`, `300` | Re-rank the vector shortlist by cosine similarity on the rescoring vectors |
| `LOCAL_RESCORE_BITS`, `LOCAL_RESCORE_DIMS`, `LOCAL_RESCORE_CLIP` | `4`, `1024`, `3.0` | How the rescoring vectors are held in memory: 4 (two dimensions a byte) or 8 bits, over the first N dimensions (`0` = all). The table still stores the full 8-bit vector; the index derives the smaller copy when it loads, so nothing is re-embedded. At 95,000 chunks of 1,536 dimensions this is 49 MB instead of 146 MB. `LOCAL_RESCORE_BITS=8` with `LOCAL_RESCORE_DIMS=0` is the old behaviour. `LOCAL_RESCORE_CLIP` is the 4-bit scaling: that many root-mean-squares of a vector's values reach the largest step and anything beyond is clipped (lower packs typical values finer but clips more outliers; `python tools/compare_rescoring.py --sweep` tries values on your index). Changing any of the three builds a new snapshot. Measure the effect on your own index with `python tools/compare_rescoring.py` first |
| `LOCAL_CONCEPT_BOOST`, `LOCAL_PHRASE_BOOST`, `LOCAL_PHRASE_POOL` | `1.0`, `1.0`, `100` | The concept and phrase bonuses (`0` turns one off) |
| `SEARCH_UNIQUE_PAGES`, `LOCAL_MAX_PER_PAGE` | `true`, `2` | Every result a different page; the per-page preference used only when that is off |
| `SEARCH_CACHE_SECONDS`, `SEARCH_CACHE_MAX_ENTRIES` | `0` (off), `500` | Keep search answers briefly, keyed on the question, concepts, domains and limit |
| `EMBED_CACHE_SHARED`, `EMBED_CACHE_TABLE`, `EMBED_CACHE_MEMORY_ENTRIES`, `EMBED_CACHE_TTL_DAYS`, `EMBED_CACHE_TIMEOUT_SECONDS`, `EMBED_CACHE_COOLDOWN_SECONDS` | `true`, the index table's name plus `qcache`, `10000`, `30`, `2`, `30` | The question cache (see Search) |
| `EMBED_QUERY_COOLDOWN_SECONDS` | `20` | After a search question fails to embed, skip the embedding endpoint for this long and search by keywords only, so an outage costs one timeout, not one per search. `0` turns it off. Answers found by keywords alone are never kept in the result cache |

## Search (Table Storage)

Searches run inside the app, so a search costs CPU on the instance that runs it and nothing per query. There is no separate
search service.

**How it works.** Each chunk is one row of a Table Storage table (`PartitionKey` is the page id, `RowKey` the chunk number).
The row holds the title, url, domain, concepts, the chunk text, the embedding reduced to one bit per dimension (192 bytes) and
at eight bits per dimension (1,536 bytes), and the chunk's words as hashes with counts, worked out once when the row is
written. Every instance scans the table into memory (a few seconds), then answers a search by:

1. keeping the chunks in the listed domains that carry at least one of the concepts;
2. keyword half: BM25 over the question's words (any word may match), the best 1,000 (`LOCAL_TEXT_RECALL`);
3. vector half: the nearest 50 (`SEARCH_VECTOR_K`) by the 1-bit codes, re-ranked by cosine similarity on the rescoring vectors (`LOCAL_RESCORE_BITS`, `LOCAL_RESCORE_DIMS`);
4. reciprocal rank fusion of the two lists (1 / (60 + rank)), the concept and phrase bonuses below, and the best few, one per page.

A one-row table holds a random version token that writers change after they write; instances check it every
`LOCAL_INDEX_POLL_SECONDS` and rebuild in the background, swapping the new index in. A failed rebuild keeps the old index.
`/document` reads are answered from the same index. If the index is not ready (a first load that failed, or an empty
table), searches fail with 502 and say why: there is nothing to fall back to, so check `/api/health` (`local_index`) and
`/api/local-index/status`.

**The question's vector** comes from an Azure OpenAI call made by this app, with the same deployment that embeds chunks. It
skips the indexing throttle and gives up after `EMBED_QUERY_TIMEOUT_SECONDS`. If it fails, the search continues with keywords
only (`search_mode` is `keyword`, and such answers are never kept in the result cache).

**Writing.** The crawler writes each chunk to Blob Storage (the document and its vector) and then to the table. If the table is
busy (throttled, timed out, server error), the page is still marked processed and a delayed `index_blob_batch` task writes the
stored chunks later; any other failure is raised so the queue retries it. A table connection is required for crawling:
without `LOCAL_INDEX_CONNECTION_STRING` (or the blob connection string) a write fails with a message that says so.

**Setting up or repairing the table.** `POST /api/index/backfill` (default mode `missing`) writes the stored chunks the
table lacks; `{"mode": "all"}` rewrites every chunk. It runs on the queue, resumes by itself and reuses stored vectors. In
PowerShell: `Start-LocalBackfill -Follow -CountBlobs`, or `Start-LocalBackfill -Mode all` to rewrite everything. Re-run
with `mode: all` after changing how chunks are analysed (the title weight, or the words kept) or to add the 8-bit vectors.
`POST /api/local-index/reload` rebuilds this instance's index now and returns how long it took. `GET /api/local-index/status`
reports what this instance has loaded; with `?count=true` it also counts the table's rows (in all and per domain, a read of
keys and domains that takes a few seconds on a large table) and the messages waiting on the queue, which is how a backfill is
followed.

**Prebuilt snapshot.** Every instance used to scan the whole table, parse every row in Python and build the same arrays
(postings, code matrix, page and concept lookups). Now one instance does that and saves the finished arrays under
`index-snapshot/` in the blob container; an instance that starts or reloads reads them back. The snapshot is used only when
the table's version row equals the snapshot's version, so it is exactly as fresh as a scan. In every other case (no snapshot,
the table changed, a different format, a different set of text-cold domains, a missing or damaged blob, storage down) the
table is scanned as before and a search is never affected. `/api/health` (`local_index.load_source`, `local_index.snapshot`)
and `GET /api/index/snapshot` say which happened and why.

- **Who builds it.** After a table build, an instance claims the build (`<version>-<fingerprint>/claim.json`, created only if
  absent) and uploads it in the background; instances that start together build it once. A claim older than 20 minutes belongs
  to a builder that died and is taken over. The manifest, `latest.json`, is written last, so a reader never sees a half-written
  build. `POST /api/index/snapshot` builds one on demand.
- **What it holds.** The arrays as raw `.bin` blobs (about 280 MB at 86,500 chunks, 133 MB of it the rescoring vectors at 8 bits over every dimension; about 45 MB at the 4-bit, 1,024-dimension default) and one
  gzip JSON file for ids, titles, urls, concepts and the text of the domains that keep their text in memory. With rescoring off
  the rescoring vectors are not downloaded. A snapshot built with other `LOCAL_RESCORE_BITS` or `LOCAL_RESCORE_DIMS` than the reader's is not used (the reader scans the table and publishes its own). A reader that keeps more domains' text in the table than the snapshot did drops that
  text on load; one that wants text the snapshot lacks scans instead.
- **The ontology.** If the ontology cannot be read on a first load, the snapshot's own set of text-cold domains is used
  rather than loading every domain's text.
- **Cost.** An instance that builds a snapshot holds a few copies of one array at a time while it uploads (the largest is the
  133 MB of vectors). Set `INDEX_SNAPSHOT_AUTOBUILD=false` and build with `POST /api/index/snapshot` when no instance can spare it.
  Old builds are deleted an hour after they were last written.

**Rescoring.** The 1-bit codes find a shortlist of close chunks quickly but rank close neighbours poorly (many equal
distances). Each row also holds the chunk's vector at 8 bits a dimension, and the search re-ranks the best
`LOCAL_RESCORE_POOL` (300) chunks by cosine similarity on those. On 86,500 chunks of 1,536 dimensions this adds about 0.3 ms to a
search and 133 MB of memory per instance. Turn it off with `LOCAL_RESCORE=false`. It needs the vectors in every row; until
they are all there the search quietly uses the codes alone, and `/api/health` and `Show-LocalIndex` say which
(`rescoring.active`, `rows_without_vectors`). `Watch-Websearch` shows a `Resc` column and checks it.

**Common words.** Words like "the", "is" and "to" are stored with every chunk and a question searches for them like any
other word (they score low, because nearly every chunk has them), `LOCAL_INDEX_STOPWORDS=english` drops 33 of them from
questions only, with no rewrite needed. On 40,000 synthetic chunks (42% common words) searching for them costs about 0.9 ms
more a search.

**Keyword depth in the rank fusion.** Up to 1,000 keyword-ranked results (`LOCAL_TEXT_RECALL`) and the nearest 50 vector
results go into the fusion, so a chunk strong on vectors but ranked 80th by keywords still gets its keyword credit. About
0.6 ms more a search than fusing 50 at 40,000 chunks.

**Ranking by what the question named.** The ontology turns a question into concepts of two kinds: ones the question's own words
matched ("week 4", "mentor") and ones it adds by relationship (networking, grit). All of them widen the filter equally, so a
broad related concept can flood the candidates: "What does a mentor do in week 4?" returned five third-party articles tagged
only `networking` while the Foundation's own "Week 4: Networking Strategies" section, tagged `week_4`, was left out. Each
candidate chunk gets a bonus: `LOCAL_CONCEPT_BOOST` (1.0) times the share of the question's own concepts the chunk is tagged
with, weighted by the ontology's confidence, on the scale of a first place in one ranked list. It reorders chunks that already
matched by keyword or vector; it never adds one that matched nothing. `0` turns it off. The answer reports
`ranking.question_concepts`.

**Every result is a different page.** With `SEARCH_UNIQUE_PAGES` (on by default) a page appears once, as its best chunk, and
the `www.` and bare copies of an address (`www.sujeeth.org/circle` and `sujeeth.org/circle/`) are one page. An answer holds fewer
than `limit` results only when fewer pages match. Best bets and spotlights are placed first, and a page they share with the
ordinary results appears once, as the curated copy.

**How a question matches a concept.** A concept matches at full strength when one of its names, aliases or query phrases appears in the question as whole words (a plural `s` on the last word is accepted): "job" is not found in "jobbing". Otherwise it matches partly when the question shares at least two topic words with a phrase. One shared word of a longer phrase is not a match, so "first moon landing" does not reach First Job and "monthly car payment" does not reach "monthly gift". A concept that should answer to a single word ("resume", "salary") needs that word as an alias or query phrase in the ontology. Run the ontology at 1.13.3 or later with this rule; older versions relied on one-word matches and lose in-scope questions.

**Relationships can widen a search or change its ranking.** Each predicate in the ontology can carry a `search_behavior` with a `matched_endpoint` (`subject`, `object` or `either`: the end the question has to match), an `effect` and a `weight` (0 to 1). The effect falls on the concept at the other end:

| Effect | What it does |
|---|---|
| `expand` | The related concept joins the search filter, so pages tagged only with it can be returned, and it counts for `weight` in the ranking (0: filter only). |
| `boost` | The filter is unchanged. A page already in the results that also carries the related concept ranks higher by `weight`. |
| `suppress` | The filter is unchanged. A page that carries the related concept ranks lower by `weight`. It is not removed. |

A weight is a share of what carrying everything the question named is worth. A single relationship can carry its own `search_behavior` to override its predicate, or `{"enabled": false}` to opt out. The rules: one hop only; a concept the question itself matched is never boosted or suppressed; when several relationships reach one concept it is expanded if any expands it, and its weight is the strongest boost less the strongest suppress; and when the question already matched one of the concepts an expansion would add ("week 3" matches Week 3 and Week), that expansion is dropped. The answer's `resolution.relationship_effects` lists what was applied, and `ranking.question_concepts` shows related concepts with a `~` in front. An ontology with no effects behaves as before: only `illustrates` acts, adding its subject to the filter. `RELATIONSHIP_EFFECTS=false` forces that.

**Spotlights are boosted three ways.** The ontology says a Spotlight illustrates career development and the career skills (networking, interviewing, job applications, salary negotiation), so a question on any of them reserves up to `SPOTLIGHT_SLOTS` (3) places for Spotlight stories, placed first and flagged `spotlight`. A Spotlight added that way also counts toward the main ranking at `SPOTLIGHT_CONCEPT_WEIGHT` (1.0; 0 leaves it out), so Spotlight pages that are not in a reserved place rank higher too. The MCP server then tells the agent to offer a Spotlight as a real example, with its link.

**A best bet must also rank in the main search.** Best bets are searched among the few hand-added pages alone, where the top
page scores high whatever it says. So a hand-added page is a best bet only when it is also among the first
`BEST_BET_MAX_RANK` (5) pages of the main search, which ranks every selected domain together. `0` turns the check off. When it
leaves pages out, `best_bets.not_relevant` says how many. Pages that were not ranked in the main search are left to the
ordinary results, so a question about 2025 expenses is no longer led by a profile page that merely matched "Foundation". `"ranking": {"unique_pages": false}` on one request (or
`SEARCH_UNIQUE_PAGES=false`) allows repeats again, and then `LOCAL_MAX_PER_PAGE` (2) prefers at most that many chunks of a
page, refilling with repeats if too few pages match.

**One pass for the main search, best bets and spotlights.** A search request asks the index up to three questions: the main
search, a search of the manually added pages (best bets) and a search of Spotlight pages. They are the same question with
different filters, so `run_search` runs them together (`SearchService.search_many`): the question is embedded once, and the keyword
scores of every chunk, the question's word pairs and the code distances are worked out once and shared. Each answer is exactly
what a separate search would have given (`tests/test_search_many.py` checks this against the separate searches). One request
counts as one search in `local_index.searches` and its timings, however many questions it asked. A best-bet or spotlight search that
fails marks `best_bets` or `spotlights` as `unavailable`, as before, and leaves the main answer alone.

**Words side by side.** Keyword scoring ignores word order: "114 students completed Career Circles" scores like the same words
scattered over a page. A chunk whose text or title holds the question's neighbouring words next to each other gets a bonus:
`LOCAL_PHRASE_BOOST` (1.0) times the share of the question's word pairs it holds, on the same scale as the concept bonus. Only
words that say what the question is about start or end a pair, with the question's own wording between them ("reach out to a
professional" gives "reach out" and "out to a professional"), so "how do" and "do I" never earn the bonus on a page that merely
happens to be written as questions and answers. Each pair weighs what the commoner of its two words weighs. It is checked on
the best `LOCAL_PHRASE_POOL` (100) candidates, costs about 0.7 ms a search at 40,000 chunks, and needs the chunk text in memory
(`LOCAL_INDEX_TEXT_IN_MEMORY=true`, the default). `0` turns it off.

**Measuring relevance.** Whether answers are good is measured with questions whose right page is known:

    python tests/relevance_eval.py

It reads `WEBSEARCH_URL` and `WEBSEARCH_KEY` (or asks for the key), asks every question in `tests/relevance_gold.json` of the
deployed app, and reports how often the right page came first, in the top three and in the top five. `--try phrase_boost=0
concept_boost=2` adds a second column: the same app with those ranking settings for that run only (`POST /api/search` and
`/api/search/explain` take `"ranking": {"concept_boost": .., "phrase_boost": .., "max_per_page": .., "unique_pages": ..}`), so a
value can be tried against your questions before it becomes an app setting. Add `--explain` and, for every question not
answered first, it asks `POST /api/search/explain` (`{"query": ..., "expect": [...]}`) and prints why: the ranking settings in
force, the word pairs it looked for, the concepts the question named and the ones added by relationship, how many chunks pass
the filters, whether the right page is in the index at all, whether the filters leave it out (and for which reason), and where
its best chunk stands in the keyword list, in the vector list (counted over every chunk that passes, not only those that
entered the ranking) and among the answer's pages. The file ships with seven questions as a start; replace them with thirty to
fifty that students and mentors really ask.

**Question cache.** Turning a question into a vector is a network call (often 150 ms or more) and the only slow step of a
search. Each question is embedded once, not once per instance: an instance looks in its own memory (10,000 questions,
`EMBED_CACHE_MEMORY_ENTRIES`), then in a table every instance shares (`EMBED_CACHE_TABLE`, default the index table's name plus
`qcache`), and only then calls the embedding service (with its single flight and cooldown, `EMBED_QUERY_*`). Only the
question's 1-bit code (192 bytes for 1536 dimensions) and its 8-bit vector (1,536 bytes) are kept, never the question's text;
the row key is a hash that includes the embedding model and its size, so changing the model misses by itself. Questions are
matched ignoring case, punctuation and extra spaces ("How do I volunteer?" is "how do i volunteer"). Entries older than
`EMBED_CACHE_TTL_DAYS` (30; 0 keeps them) are ignored and replaced. If the table is slow or failing, it is skipped for
`EMBED_CACHE_COOLDOWN_SECONDS` (30) so a search never waits on it for long, and each table call is limited to
`EMBED_CACHE_TIMEOUT_SECONDS` (2). Turn the shared table off with `EMBED_CACHE_SHARED=false` (memory only). Questions already
remembered are still answered while the embedding endpoint is paused after a failure. `/api/health` and
`/api/local-index/status` report `embed_cache`: lookups, the share answered without an embedding call, where each was
answered, embedding calls and their average time, and the table's errors. `POST /api/embed-cache/seed` with
`{"questions": [...]}` (up to 500) embeds questions ahead of time, so common ones are never waited for. The table grows by about
1.8 KB per distinct question and is not purged; delete it any time.

**From PowerShell** (`. .\crawl-helper.ps1`): `Show-LocalIndex -Count` shows what one instance has loaded and the table's rows
per domain (`-CountBlobs` adds the stored chunks, slowly, to show how much is written). `Start-LocalBackfill` (or
`Start-LocalBackfill muse` for one domain, `-Mode all` to rewrite everything) asks first and queues the work; `-Follow` hands
over to `Watch-LocalBackfill`, which shows rows, rows added, rows per minute and the queue backlog, and stops by itself once the
table stops growing and the queue is empty. `Seed-EmbedCache` seeds the question cache from a list or a file (one question per
line). `Watch-Websearch` is a health watcher: every poll shows the instance's uptime and process id (a restart is flagged),
whether the index is loaded and its size, whether rescoring is active, process memory, searches with the average embedding and
in-memory search time, keyword-only searches, and the result cache and question cache hit rates; on Ctrl+C (or after `-Polls N`)
it prints a summary with checks. `-InstanceMb` is the instance size the memory check is measured against.

**Memory (measured on synthetic chunks, per 40,000 chunks).** Steady: about 230 MB with text in memory, about 90 MB without,
plus the rescoring vectors (about 61 MB per 40,000 chunks at 8 bits over 1,536 dimensions, about 20 MB at the 4-bit, 1,024-dimension default). While a rebuild runs there is a peak about 400 MB higher, with the old
index still in use. `/api/health` shows `local_index.process_memory_mb`. On the live index (about 86,500 chunks) 2,048 MB
instances run at 750 to 830 MB steady with a rebuild peak near 1,000 MB. If memory gets tight, set
`LOCAL_INDEX_TEXT_IN_MEMORY=false` (each search then reads about five table rows, and a read costs a fraction of a cent per
10,000; the phrase bonus is off then), or use 4,096 MB.

**Running in 512 MB.** Steady memory is the index, so it is set by what is kept in memory. Settings, from the largest saving down:
`LOCAL_INDEX_TEXT_IN_MEMORY` with text-cold domains in the ontology (about 1.8 KB a chunk of text kept out), `LOCAL_POSTING_MAX_DF=0.3`
(on a synthetic Zipf corpus: postings 43% smaller together with float16, less on real text), `LOCAL_POSTING_WEIGHT_DTYPE=float16`
(the default, a quarter off the postings), and the shared title, url and concept strings (always on). Rescoring stays on.
A reload is the other peak: the old index and the new one are in memory together, and a normal build from the table holds every row on
top of that. The settings that fix this are **on by default**; set one to `false` (or the jitter to `0`) to get the old behaviour back.
`LOCAL_POSTING_MAX_DF` is the one that stays off until you have checked relevance. For 512 MB instances:

1. `LOCAL_INDEX_LEAN_BUILD` and `LOCAL_INDEX_DROP_BEFORE_RELOAD`: a reload frees the old index and builds the new one with
   nothing else in memory, from the snapshot when it is current, otherwise from the table in two passes.
2. `LOCAL_INDEX_FAIL_FAST` (the MCP app retries the 503), and `LOCAL_INDEX_RELOAD_JITTER_SECONDS` (300) with two always-ready instances,
   so one answers while the other reloads. Raise `LOCAL_INDEX_MIN_RELOAD_SECONDS` (for example `3600`) so a crawl does not reload every 10 minutes.
3. `LOCAL_INDEX_PRELOAD_ON_HTTP`, so queue and timer instances do not load the index.
4. Nothing to run after a crawl. The first instance to see the change builds the index from the table and publishes the snapshot
   (`INDEX_SNAPSHOT_AUTOBUILD`); the other instance, a few minutes later, loads that snapshot. While an instance builds, its searches
   answer 503 and the MCP app retries them on the other instance. If the table keeps changing for the whole build (a crawl writing
   constantly), the build gives up after 3 tries and the next poll tries again.
5. After changing `LOCAL_POSTING_*`, force one rebuild: `POST /api/index/snapshot` on an instance, or `python tools/build_snapshot.py`
   from any computer (optional; the instances would otherwise pick the change up at the next crawl).

Watch `process_memory_mb` in `/api/health` and `Watch-Websearch -InstanceMb 512`.

## Operating notes

- **Searches answer by keywords only, or slowly:** a search question is embedded by Azure OpenAI, and the same deployment embeds chunks while crawling. Heavy indexing can throttle question embedding; searches then continue with keywords only (`search_mode: keyword`, counted in `local_index.keyword_only_searches`) and the endpoint is skipped for `EMBED_QUERY_COOLDOWN_SECONDS`. Mitigations: set `EMBED_MIN_INTERVAL_SECONDS`, cap the instance count (Flex Consumption: Settings → Scale and concurrency), give queries their own embedding deployment, and seed common questions with `Seed-EmbedCache`.
- **Page limits live here, not in the ontology.** Every domain gets `MAX_PAGES_PER_DOMAIN` and `MAX_CRAWL_DEPTH`; there are no per-domain limits. The page limit is also the number of page URLs collected from a domain's sitemaps. The limit applies to every page a job queues (the homepage, sitemap URLs and links found on pages); once a job has found that many pages it queues no new ones. The ontology only decides which domains are crawl-enabled.
- **Permanent failures are skipped, not retried:** off-site redirects, redirect loops, non-public IPs, and HTTP 400/401/403/404/405/406/410/451. Timeouts, 429, and 5xx still retry (up to 5 deliveries).
- **Stale-page cleanup** only runs after a clean crawl: no skipped or failed pages and below the page cap. Because skipped pages block it, domains with dead links will not purge removed pages.
- **Re-tagging:** tags are assigned at crawl time, and unchanged pages are not re-tagged on later crawls. After an ontology alias or domain-concept change, run `index/retag-preview`, then `index/retag`. The resolver caches the ontology in memory, so restart the app after an ontology deploy.
- **Path rules fail closed.** The crawler needs the ontology's `crawl_policy` rules (`include_paths`, `exclude_paths`, `include_sitemaps`) before it fetches a page or queues a link. If it has never been able to read them, or the ontology answers with a manifest that lists no crawl domains, page tasks fail and retry instead of crawling unfiltered. Once rules have been read, a later failed or empty answer keeps the previous rules.
- **Excluded paths.** A domain's `crawl_policy.exclude_paths` in the ontology (for example TealHQ's `/job` and `/jobs`) stops the crawler from queuing or fetching those URLs. This covers sitemap URLs, discovered links, and pages already waiting in the queue. Excluded pages are not counted as skipped, so they do not block stale-page cleanup. The rules are cached for 10 minutes, so after changing them, restart the app, then run `index/purge-preview` and `index/purge` to remove what is already stored. Run the purge before a backfill, or the backfill will re-index the excluded pages from Blob Storage.
- **Allowed paths.** A domain's `crawl_policy.include_paths` (for example `["/career-advice"]`) limits the crawl to those sections. It uses the same rule format as `exclude_paths`, and the two combine: with an allow-list, only matching paths are crawled, and `exclude_paths` still wins (allow `/career-advice`, block `/career-advice/news`). The bare homepage (no query string) is always allowed, because the crawl starts there to find sitemaps and links; its query variants such as `/?from=nav` are not. A bare `/` is ignored in either list. Without `include_paths`, nothing is limited. Deploy this code before adding `include_paths` to the ontology: older code ignores the key and would crawl the whole site. `index/purge-preview` and `index/purge` also remove stored chunks outside an allow-list, and `blob_paths_report.py` flags them.
- **Canonical tags.** The crawler stores a page under the URL in its `<link rel="canonical">` tag. If that URL is blocked by the domain's `include_paths` or `exclude_paths` (a wrong tag on the site, such as `/author/<article>` on an `/advice` page), the tag is ignored and the page is stored under the URL that was fetched. Pages already stored under a blocked canonical are found by `index/purge-preview`; after `index/purge`, the next crawl stores them under their fetched URLs.
- **Vectors are stored and a busy index table never fails a crawl.** Each chunk is embedded once, and its vector is saved as `vectors/<id>.json` next to the chunk. If the index table stays busy or unreachable (throttling, timeouts, server errors) through the SDK's own retries, the page is still marked processed (`index_deferred`), and a delayed `index_blob_batch` task writes the stored chunks later: after 2, 5, 10, 20, and 30 minutes, then hourly, up to `INDEX_MAX_DEFERRALS` times. Nothing is fetched or embedded again. A page whose indexing is deferred is missing from search until that task succeeds, and `POST /api/index/backfill` writes anything still stored but missing.
- **Backfill** compares Blob Storage with the index table and, in the default `missing` mode, writes only the chunks that are absent. It is safe to run again whenever you suspect a gap, such as after a long outage. Use `mode: all` to rewrite every row (to add the 8-bit vectors, or after changing the title weight or the words kept). Chunks that already have a stored vector are not embedded again, so only chunks without one pay for embeddings. Watch `Watch-LocalBackfill`, the `Index batch mode=… already_indexed=… indexed=…` log lines, and the poison queue.
- **Index capacity.** Every instance holds the whole index in memory, so the limit is memory, not a service quota: see the memory figures under Search. A Table Storage entity is at most 1 MiB and a string property 32,768 characters, so chunk text and titles are cut at 30,000 characters (chunks are about 1,800). The table itself has no practical size limit here. To measure memory, run `Show-LocalIndex` and `Watch-Websearch` on each instance size you consider.
- **Logging.** The Azure SDKs are set to log warnings and errors only. At INFO they write two multi-line records for every storage or table call (each blob read shows up as an HTTP 206 range request), which floods the logs and inflates the Log Analytics bill. The app's own messages (`Purge batch …`, `Index batch …`, `Index table busy …`) are still logged.
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
| `tools/build_snapshot.py` | Builds the index snapshot from the table and saves it to Blob Storage, from any computer. Optional: instances build and publish their own, and `LOCAL_INDEX_SNAPSHOT_ONLY` apps rely on this | |
| `crawl-helper.ps1` | Table views and shortcuts for the crawl API: `Show-Crawl` (one row per domain, with a TOTAL row), `Show-CrawlPages`, `Show-CrawlPaths` (with a TOTAL row), `Show-CrawlTimeline`, `Start-Crawl`, `Wait-Crawl`, `Resume-Crawl`, `Stop-Crawl`, `Get-CrawlJobId`, `Show-LocalIndex`, `Start-LocalBackfill`, `Watch-LocalBackfill`, `Watch-Websearch`, `Seed-EmbedCache` (the index table and the health of each instance, see Search), and `Call` for any raw request. Load it with `. .\crawl-helper.ps1` | `GET /health`, `GET /crawl-dashboard` (the `latest_job` row of each domain), `POST /crawl` (its `jobs` and `skipped` lists), `GET /crawl/{job_id}/pages` (`?summary=true`, `?status=`, `?limit=`; fields `url`, `status`, `reason`, `attempts`, `error`, `created_utc`, `updated_utc`), `POST /crawl/{job_id}/cancel` and `/resume`, `GET /local-index/status` (`enabled`, `loaded`, `chunks`, `rescoring`, `embed_cache`, `table_rows`, `table_by_domain`, `queue_messages`), `POST /index/backfill` (`mode`, `domains`), `POST /embed-cache/seed`, and the `process`, `local_index`, `query_cache` and `embed_cache` sections of `GET /health` |
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
2. Wait for the run to finish, then check `GET /api/health`: `index_configured` and `embedding_configured` should be `true`, and `local_index.loaded` becomes `true` after the first search or the preload.
3. If the ontology changed, deploy it first, then restart this app.
4. Confirm the five timers appear under **Functions**: `scheduled_crawl_recovery`, `scheduled_job_cleanup`, `scheduled_weekly_crawl`, `scheduled_monthly_crawl` and `scheduled_index_reconcile`.

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

Release history and the notes for upgrading from Azure AI Search are in [CHANGELOG.md](CHANGELOG.md).
