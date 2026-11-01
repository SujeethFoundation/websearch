# Web Search Function App — Azure AI Search integration v0.7.0

This release keeps Blob Storage as the durable source of truth and adds Azure AI Search (`sf-knowledge`) as the retrieval index. Existing ontology resolution and persistent crawl queues remain in place.

## Required Function App settings

Add/update these under **Function App → Settings → Environment variables**. Do not put secrets in source control.

| Setting | Value |
|---|---|
| `AZURE_SEARCH_ENDPOINT` | `https://sujeeth-aisearch.search.windows.net` |
| `AZURE_SEARCH_API_KEY` | Azure AI Search **primary or secondary admin key** |
| `AZURE_SEARCH_INDEX_NAME` | `sf-knowledge` |
| `AZURE_SEARCH_API_VERSION` | `2025-09-01` |
| `AZURE_OPENAI_ENDPOINT` | `https://avisuj-4010-resource.openai.azure.com` |
| `AZURE_OPENAI_API_KEY` | API key for Foundry resource `avisuj-4010-resource` |
| `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` | `sf-embeddings` |
| `AZURE_OPENAI_API_VERSION` | `v1` |
| `AZURE_OPENAI_EMBEDDING_DIMENSIONS` | `1536` |

Keep the existing settings for `ONTOLOGY_API_URL`, `ONTOLOGY_FUNCTION_KEY`, `BLOB_STORAGE_CONNECTION_STRING`, `BLOB_CONTAINER`, `AzureWebJobsStorage`, and `CRAWL_QUEUE_NAME` unchanged.

The Search key must be an admin key because the Function App uploads and deletes indexed documents. The Foundry key is separate and is used only to generate document embeddings.

## Index contract

The code expects the existing index `sf-knowledge` with fields:

- `id` (String key)
- `title` (String, searchable/retrievable)
- `url` (String, filterable/retrievable)
- `domain` (String, filterable/retrievable)
- `concepts` (Collection(String), filterable/retrievable)
- `content` (String, searchable/retrievable)
- `contentVector` (Collection(Single), searchable, 1536 dimensions)

The vector profile used by `contentVector` must be `sf-vector-profile`, and its vectorizer should be `sf-embedding-vectorizer` configured for the `sf-embeddings` deployment.

## What changes

- New crawl chunks are embedded in batches and uploaded to Azure AI Search.
- Original chunk JSON remains in Blob Storage under `documents/`.
- Search uses hybrid keyword + vector retrieval and applies ontology-resolved domain and concept filters server-side.
- Existing Blob documents can be backfilled through the existing Storage Queue worker.
- The old full-container download-and-scan search path is removed from normal queries.

## Deploy

1. Back up the current Web Search Function App project.
2. Copy this package's project files over the existing Web Search project (preserve `local.settings.json`; it is not included here).
3. Commit and push through the existing GitHub Actions deployment workflow, or deploy using the same process used for v0.6.1.
4. Add the Function App settings above and restart the Function App.
5. Verify `GET /api/health`. Expected values include version `0.7.0`, `ai_search_configured: true`, `embedding_configured: true`, and `search_mode: azure_ai_search_hybrid`.
6. Verify `GET /api/index-status` reports the `sf-knowledge` index.
7. Start the existing-content backfill once using `POST /api/index/backfill` with a Function key. The queue worker will page through `documents/`, batch embeddings, and upload to the index.
8. Monitor queue depth and `GET /api/index-status` until the indexed document count approaches the Blob document count.
9. Run a test search through `POST /api/search`.

## Backfill endpoint

`POST /api/index/backfill`

No request body is required. It enqueues a single backfill coordinator task. The coordinator pages through Blob names and queues small batches for embedding and upload. Avoid starting multiple backfills simultaneously.

## Notes

- `GET /api/index-status` reports Blob document count and Azure AI Search document count.
- `POST /api/reindex` now returns HTTP 409 with instructions to use the queue-driven backfill endpoint.
- The index vectorizer handles query-time text vectorization. The Function App generates vectors for document chunks during ingestion.
- Do not expose either API key in logs, code, GitHub, or chat.
