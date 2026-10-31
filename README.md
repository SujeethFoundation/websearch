
# Sujeeth Foundation Web Search Function App v0.2.0

This version makes ontology-based concept selection mandatory for every search.

## Search flow

1. Load ontology classes, preferred terminology and relationships.
2. Match the natural-language query to ontology class IDs.
3. Send the selected concept IDs to the Ontology API's `domain-resolution` endpoint.
4. Search only the approved domains returned for those concepts.
5. Apply concept metadata filtering to indexed documents.
6. Return the matched concepts, selected domains, ontology version and resolution trace.

If no ontology concepts match, the app returns `no_ontology_concept_match`. It does **not** fall back to searching every domain.

Explicit `concepts` can be supplied to narrow or guide resolution. Explicit `domains` can only narrow the ontology-selected domains and must pass ontology validation. They cannot broaden the search.

## Endpoints

- `GET /api/health`
- `GET /api/search?q=...`
- `POST /api/search`
- `POST /api/domain-resolution` — diagnostic query-to-concept and domain-resolution route
- `GET /api/sources`
- `GET /api/index-status`
- `POST /api/refresh`
- `POST /api/reindex`

## Prerequisites

- Python 3.12 or 3.13
- Azure Functions Core Tools v4
- Ontology Function App v1.1.0
- Optional: Azure Blob Storage and Azure OpenAI embeddings

## PowerShell setup

From the extracted project directory:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item local.settings.example.json local.settings.json
```

Edit `local.settings.json`:
- `ONTOLOGY_API_URL`: base URL of the deployed Ontology Function App, without `/api`
- `ONTOLOGY_FUNCTION_KEY`: its shared host key
- `MAX_PAGES_PER_DOMAIN`: defaults to 1000

Never commit `local.settings.json` or expose the function key.

## Run locally

```powershell
func start
```

In a second PowerShell terminal:

```powershell
$baseUrl = "http://localhost:7071"
Invoke-RestMethod "$baseUrl/api/health"
Invoke-RestMethod "$baseUrl/api/sources"
```

## Test concept-specific domain resolution through Search App

```powershell
$body = @{
    query = "How do I write a resume?"
} | ConvertTo-Json

Invoke-RestMethod `
    -Method Post `
    -Uri "$baseUrl/api/domain-resolution" `
    -ContentType "application/json" `
    -Body $body | ConvertTo-Json -Depth 15
```

## Test a complete search

```powershell
$body = @{
    query = "How do I write a resume?"
    limit = 5
} | ConvertTo-Json

Invoke-RestMethod `
    -Method Post `
    -Uri "$baseUrl/api/search" `
    -ContentType "application/json" `
    -Body $body | ConvertTo-Json -Depth 15
```

Or GET:

```powershell
Invoke-RestMethod "$baseUrl/api/search?q=How%20do%20I%20write%20a%20resume"
```

## Important: concept tagging of indexed documents

The search service requires documents to have ontology concept IDs in their `concepts` metadata. The current starter crawler sets `concepts` to an empty list; therefore newly crawled documents will not be returned by concept-filtered search until a content-enrichment/classification step assigns ontology concepts to each chunk.

This package implements mandatory query-side concept selection and domain scoping. It does not yet implement document-side ontology enrichment. That is the next required step before real website search can return crawled pages reliably.

## Storage and embeddings

Without Blob settings, documents are stored as local JSON files under `data/`. This is for local testing only and is not shared between scaled-out Function instances.

Optional Blob settings:
- `BLOB_STORAGE_CONNECTION_STRING`
- `BLOB_CONTAINER`

Optional Azure OpenAI embedding settings:
- `AZURE_OPENAI_ENDPOINT`
- `AZURE_OPENAI_API_KEY`
- `AZURE_OPENAI_EMBEDDING_DEPLOYMENT`
- `AZURE_OPENAI_API_VERSION`

Without embeddings, the response explicitly reports `keyword_fallback`. With embeddings, this starter uses cosine similarity plus keyword scoring; it is not a managed vector database or production hybrid-search engine.

## Current limitations before production

- Concept matching is deterministic lexical matching against ontology names, aliases, and definitions. It is not an LLM-based semantic classifier.
- Ontology response schemas are handled flexibly but should be integration-tested against the deployed ontology app.
- The crawler is a starter, not production-grade: robust robots parsing, sitemap ingestion, URL canonicalization, SSRF protection, persistent crawl state, content enrichment, stale-page deletion, retries/backoff, and scheduling still need implementation.
- Crawler documents currently have no assigned ontology concepts; add document-side concept enrichment before relying on concept-filtered retrieval.
- Local file storage is not suitable for a scaled-out production deployment.
- No automatic zone or regional failover is provided.


## Version 0.3.0 — deeper website crawling

This release upgrades the starter crawler with:

- Breadth-first recursive same-host link discovery.
- Sitemap discovery from `robots.txt`, `/sitemap.xml`, and `/sitemap_index.xml`,
  including sitemap-index traversal.
- Configurable maximum crawl depth (`MAX_CRAWL_DEPTH`, default 5).
- Configurable per-domain page cap (`MAX_PAGES_PER_DOMAIN`, default 1000).
- Configurable request spacing (`CRAWL_DELAY_SECONDS`, default 0.5 seconds).
- URL normalization, fragment removal, and common tracking-parameter removal.
- Robots exclusion checks using Python's `RobotFileParser`.
- Crawl metrics: discovered, crawled, skipped, errors, sitemap count, depth reached,
  page limit, and remaining queue.

### Azure application settings

Set these Function App application settings:

- `MAX_PAGES_PER_DOMAIN=1000`
- `MAX_CRAWL_DEPTH=5`
- `CRAWL_DELAY_SECONDS=0.5`
- `MAX_SITEMAP_URLS=5000`

Keep `BLOB_STORAGE_CONNECTION_STRING` and `BLOB_CONTAINER=web-content` as already configured.

### Important operating limits

- Crawling remains synchronous inside the HTTP-triggered refresh function. Do not
  start a 1,000-page crawl in one request; gateway/runtime timeouts remain possible.
- Run one domain at a time for initial validation and keep the page cap low (e.g. 25–50).
- The crawler only follows HTTPS URLs on the exact configured hostname. It does not
  crawl subdomains automatically.
- Sitemap discovery is best-effort; malformed, oversized, inaccessible, or off-host
  sitemap entries are ignored.
- Robots rules are checked for each requested URL. A robots.txt fetch failure blocks
  the domain rather than assuming permission.
- This release does not yet implement document-side ontology concept enrichment,
  persistent crawl checkpoints, stale-page deletion, a background queue, or semantic
  page prioritization. Crawled chunks currently carry an empty `concepts` list.
- Respect site terms, robots directives, rate limits, and any privacy/access restrictions.


## Version 0.3.1 — apex/www sitemap compatibility

- Treats `sujeeth.org` and `www.sujeeth.org` as the same approved website.
- Checks robots.txt independently for both permitted host variants and refuses to crawl a host whose robots policy could not be retrieved.
- Discovers sitemap locations from both robots.txt files and conventional sitemap paths.
- Parses sitemap URL sets and nested sitemap indexes.
- Processes sitemap-discovered URLs before relying on homepage link discovery.
- Follows redirects between apex and www, but rejects redirects to unrelated hosts.
- Adds skip-reason counts, sitemap failures, robots errors, and sitemap-page crawl counts.
- Retains same-site HTTPS restriction, page/depth limits, and request delay.

The sitemap `<lastmod>` metadata is currently included only where parsed by the crawler; the supplied sitemap's `<lastmod>` fields can be added to per-URL metadata in a later refinement. The crawler remains synchronous and does not yet checkpoint unfinished queues or assign ontology concepts to document chunks.
