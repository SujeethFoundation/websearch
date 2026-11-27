# Web search changelog

- Snapshot restore downloads each array straight into its final buffer and fetches `INDEX_SNAPSHOT_DOWNLOAD_WORKERS` (default 2) at a time, instead of holding every blob twice at once. This lowers the memory peak while an instance loads, which was about 529 MB against a 512 MB limit.

Moved out of the README.

## Running in 512 MB

On by default: `LOCAL_INDEX_LEAN_BUILD`, `LOCAL_INDEX_DROP_BEFORE_RELOAD`, `LOCAL_INDEX_PRELOAD_ON_HTTP`, `LOCAL_INDEX_FAIL_FAST`, `LOCAL_INDEX_RELOAD_JITTER_SECONDS=300` and float16 weights. Set each to `false` (the jitter to `0`, the weight type to `float32`) for the old behaviour. `LOCAL_POSTING_MAX_DF` and `LOCAL_INDEX_SNAPSHOT_ONLY` stay off.

- Keyword weights are stored as float16 (`LOCAL_POSTING_WEIGHT_DTYPE`), and `LOCAL_POSTING_MAX_DF` can leave words found in most chunks out of the postings (off by default).
- Chunks of one page share their title, url and concept list in memory, in built and restored indexes.
- `LOCAL_INDEX_LEAN_BUILD`: a build from the table reads it twice and streams into arrays of the final size (`services/lean_index.py`), so it needs about 35 MB beyond the finished index instead of about 200 MB. Snapshot uploads no longer copy the largest array, and a table-built index keeps its text packed.
- `LOCAL_INDEX_SNAPSHOT_ONLY` (optional): the app never scans the table by itself; `LOCAL_INDEX_DROP_BEFORE_RELOAD`: a snapshot reload frees the old index first; `LOCAL_INDEX_RELOAD_JITTER_SECONDS`: instances reload at different times; `LOCAL_INDEX_PRELOAD_ON_HTTP`: queue and timer instances do not preload. `/document` now answers 503 with `Retry-After` while the index loads and `LOCAL_INDEX_FAIL_FAST` is on, like `/search`.
- `tools/build_snapshot.py` builds and publishes a snapshot from any computer.

## Slim catalog

- The concept resolver builds its catalog from one call to the ontology app's `/api/ontology/catalog` (classes, predicates, relationships and preferred terminology, about 255 KB) instead of four (`/api/ontology`, `/classes`, `/terminology`, `/relationships`, which downloaded the classes and relationships twice). An ontology app without the route is read the old way.

## Version history (short)

- 0.5.0: persistent page-level crawl jobs on queues and tables.
- 0.6.x: page claims, leases, reconcile, and recover.
- 0.7.0: Azure AI Search hybrid retrieval, backfill (since replaced).
- 0.7.x reliability patch: job rows carry `job_id` (the daily recovery timer reads it), one job-start path with an open-job check for the manual, bulk and scheduled crawls, a deleted page (404/410) leaves the index, robots.txt is cached per host, and the nightly reconcile checks the index before it reads blobs.
- 0.7.x: the page limit comes only from `MAX_PAGES_PER_DOMAIN`; `MAX_PAGES_BY_DOMAIN` is removed and ignored if still set.
- 0.7.x memory patch: the old crawler and the `/refresh`, `/enrich` and `/reindex` routes are removed; page, manifest and job rows are read a page at a time; `crawl/domain-stats` sums job counters; `crawl/{job_id}/pages` takes `status`, `limit` and `summary`; `scheduled_job_cleanup` deletes old jobs; ontology answers are cached briefly; every tunable setting is read in `services/config.py`.
- After 0.7.0: embedding retry and throttle; permanent failures skipped; explicit-domain validation fix; scheduled recovery and weekly crawl; re-tag and document routes; app-wide page limits.
- After 0.7.x: search runs inside the app over a Table Storage index; Azure AI Search, shadow mode and the Compute Unit tracking are removed. Added: 8-bit rescoring, the concept and phrase bonuses, one result per page, the shared question cache, `search/explain`, and the relevance scorer.

## Upgrading from the version that used Azure AI Search

Azure AI Search is no longer used. These settings are ignored if
still set, and can be deleted: `AZURE_SEARCH_ENDPOINT`, `AZURE_SEARCH_API_KEY`, `AZURE_SEARCH_INDEX_NAME`,
`AZURE_SEARCH_API_VERSION`, `AZURE_SEARCH_BATCH_SIZE`, `AZURE_SEARCH_VECTOR_FIELD`, `AZURE_SEARCH_NULL_VECTOR_FIELDS`,
`AZURE_SEARCH_BLOB_BACKED`, `SEARCH_BACKEND`, `AI_SEARCH_WRITES`, `LOCAL_INDEX_WRITE`, `LOCAL_SEARCH_FALLBACK`,
`LOCAL_SHADOW_SAMPLE`, `LOCAL_SHADOW_DIAGNOSE`, `LOCAL_SHADOW_DIAGNOSE_MIN_SECONDS`, `SEARCH_MAX_TEXT_RECALL`, `SEARCH_COUNT`,
`SEARCH_FIELDS`, `SEARCH_UNIQUE_OVERFETCH`, `SEARCH_MIN_INTERVAL_SECONDS`, `SEARCH_WRITE_ATTEMPTS`, `BLOB_CACHE_SECONDS`,
`BLOB_CACHE_MAX_ENTRIES`, `INDEX_CONCEPTS_FACETABLE`. A caller that still sends `backend` to `/api/search` is ignored. The
`/api/local-index/shadow-diffs` route and the shadow columns of `Watch-Websearch` are gone, and `/api/health` no longer reports
`ai_search_*`, `search_backend`, `blob_cache` or `search_tuning`. Before the first deploy of this version, check that the index
table is full (`Show-LocalIndex -Count`); the Azure AI Search service and its index can be deleted once you are satisfied.
