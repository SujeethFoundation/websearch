import os


VERSION = "0.7.0"


def _int(name, default, low=None, high=None):
    """An integer app setting. A blank or invalid value falls back to the default; low/high clamp it."""
    try:
        value = int(os.getenv(name, "") or default)
    except ValueError:
        value = default
    if low is not None:
        value = max(low, value)
    if high is not None:
        value = min(high, value)
    return value


def _float(name, default):
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return float(default)


def _flag(name, default=True):
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def _csv(name, default):
    return tuple(x.strip().lower() for x in os.getenv(name, default).split(",") if x.strip())


class Settings:
    ontology_api_url = os.getenv("ONTOLOGY_API_URL", "").rstrip("/")
    ontology_function_key = os.getenv("ONTOLOGY_FUNCTION_KEY", "")
    local_data_dir = os.getenv("LOCAL_DATA_DIR", "data")
    blob_connection_string = os.getenv("BLOB_STORAGE_CONNECTION_STRING", "")
    blob_container = os.getenv("BLOB_CONTAINER", "web-content")
    azure_storage_connection = os.getenv("AzureWebJobsStorage", "")
    crawl_queue = os.getenv("CRAWL_QUEUE_NAME", "web-crawl-pages")
    crawl_jobs_table = os.getenv("CRAWL_JOBS_TABLE", "WebCrawlJobs")
    crawl_pages_table = os.getenv("CRAWL_PAGES_TABLE", "WebCrawlPages")
    crawl_manifest_table = os.getenv("CRAWL_MANIFEST_TABLE", "WebCrawlManifest")
    crawler_user_agent = os.getenv("CRAWLER_USER_AGENT", "SujeethFoundationKnowledgeCrawler/0.7")
    max_page_bytes = int(os.getenv("MAX_PAGE_BYTES", "5000000"))
    min_text_chars = int(os.getenv("MIN_TEXT_CHARS", "80"))
    chunk_chars = int(os.getenv("CHUNK_CHARS", "1800"))
    allowed_extensions = tuple(x.strip().lower() for x in os.getenv("CRAWL_EXTENSIONS", ".html,.htm,.pdf,.docx").split(",") if x.strip())

    # Foundry/Azure OpenAI-compatible embeddings endpoint. Use the resource's
    # OpenAI endpoint, not the Foundry project endpoint.
    embedding_endpoint = os.getenv(
        "AZURE_OPENAI_ENDPOINT",
        "https://avisuj-4010-resource.openai.azure.com",
    ).rstrip("/")
    embedding_key = os.getenv("AZURE_OPENAI_API_KEY", "")
    embedding_deployment = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "sf-embeddings")
    embedding_api_version = os.getenv("AZURE_OPENAI_API_VERSION", "v1")
    embedding_dimensions = int(os.getenv("AZURE_OPENAI_EMBEDDING_DIMENSIONS", "1536"))

    search_endpoint = os.getenv(
        "AZURE_SEARCH_ENDPOINT", "https://sujeeth-aisearch.search.windows.net"
    ).rstrip("/")
    search_api_key = os.getenv("AZURE_SEARCH_API_KEY", "")
    search_index_name = os.getenv("AZURE_SEARCH_INDEX_NAME", "sf-knowledge-v2")
    search_api_version = os.getenv("AZURE_SEARCH_API_VERSION", "2025-09-01")
    search_batch_size = max(1, min(int(os.getenv("AZURE_SEARCH_BATCH_SIZE", "100")), 500))
    # The vector field that receives chunk vectors and is searched. To move to a compressed field in the same
    # index, add that field to the index definition, point AZURE_SEARCH_VECTOR_FIELD at it, and list the old field
    # in AZURE_SEARCH_NULL_VECTOR_FIELDS so every rewritten chunk drops its old vector and frees that quota.
    search_vector_field = os.getenv("AZURE_SEARCH_VECTOR_FIELD", "contentVector").strip() or "contentVector"
    # When true, Blob Storage is the only copy of chunk text and vectors. The index keeps no retrievable text and no
    # stored vector copy, so every index write replaces the whole document and result text is read from blobs.
    search_blob_backed = os.getenv("AZURE_SEARCH_BLOB_BACKED", "true").strip().lower() in ("1", "true", "yes", "on")
    search_null_vector_fields = tuple(
        x.strip() for x in os.getenv("AZURE_SEARCH_NULL_VECTOR_FIELDS", "").split(",") if x.strip()
    )
    index_backfill_batch_size = max(1, min(int(os.getenv("INDEX_BACKFILL_BATCH_SIZE", "10")), 25))

    max_pages = int(os.getenv("MAX_PAGES_PER_DOMAIN", "10000"))
    max_crawl_depth = int(os.getenv("MAX_CRAWL_DEPTH", "5"))
    crawl_delay = float(os.getenv("CRAWL_DELAY_SECONDS", "0.5"))
    max_sitemaps = int(os.getenv("MAX_SITEMAPS_PER_DOMAIN", "100"))
    # Memory guards (see services/limits.py). HTML takes about 35x its size in memory to parse, so it has
    # the lowest cap. MAX_PAGE_BYTES stays the ceiling for every kind. MAX_CHUNKS_PER_PAGE of 0 means no limit.
    max_html_bytes = min(int(os.getenv("MAX_HTML_BYTES", "1500000")), max_page_bytes)
    max_doc_bytes = min(int(os.getenv("MAX_DOC_BYTES", str(max_page_bytes))), max_page_bytes)
    max_sitemap_bytes = int(os.getenv("MAX_SITEMAP_BYTES", "10000000"))
    max_chunks_per_page = max(0, int(os.getenv("MAX_CHUNKS_PER_PAGE", "400")))
    chunk_group_size = max(1, int(os.getenv("CHUNK_GROUP_SIZE", "50")))
    timeout = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "20"))
    max_query_concepts = int(os.getenv("MAX_QUERY_CONCEPTS", "5"))

    # Search results (these were read straight from the environment in function_app.py).
    spotlight_slots = _int("SPOTLIGHT_SLOTS", 2, 0, 3)
    best_bets_max = _int("BEST_BETS_MAX", 3, 0, 3)
    full_text_domains = frozenset(_csv("FULL_TEXT_DOMAINS", "sujeeth.org,topics.sujeeth.org"))
    excerpt_max_chars = _int("EXCERPT_MAX_CHARS", 3000)

    # Scheduled work.
    weekly_crawl_domains = frozenset(_csv("WEEKLY_CRAWL_DOMAINS", "sujeeth.org,topics.sujeeth.org"))
    index_reconcile_enabled = _flag("INDEX_RECONCILE_ENABLED", True)
    index_reconcile_max_queue = _int("INDEX_RECONCILE_MAX_QUEUE", 500)
    # Crawl job history. A job with no update for this many days no longer blocks a new crawl of its domain.
    open_job_stale_days = _int("OPEN_JOB_STALE_DAYS", 3, 1)
    # Jobs (and their page rows) not updated for this many days are deleted. The newest job of each domain is kept.
    job_retention_days = _int("JOB_RETENTION_DAYS", 30, 7)
    job_cleanup_enabled = _flag("JOB_CLEANUP_ENABLED", True)

    # Index writes and embeddings (these were read in search_service.py, queue_worker.py and embedding_service.py).
    search_min_interval = _float("SEARCH_MIN_INTERVAL_SECONDS", 0)
    search_write_attempts = _int("SEARCH_WRITE_ATTEMPTS", 8, 1)
    search_breaker_seconds = _float("SEARCH_BREAKER_SECONDS", 120)
    embed_batch_size = _int("EMBED_BATCH_SIZE", 16, 1)
    embed_min_interval = _float("EMBED_MIN_INTERVAL_SECONDS", 0)
    index_max_deferrals = _int("INDEX_MAX_DEFERRALS", 12, 0)

    # How long the ontology client keeps a successful manifest or domain-concept answer.
    ontology_cache_seconds = _int("ONTOLOGY_CACHE_SECONDS", 120, 0)

    def page_limit_for(self, domain=None):
        """Pages per domain: MAX_PAGES_PER_DOMAIN, the same for every domain.

        This one number caps a crawl job's total pages and also the page URLs collected from sitemaps.
        The domain is accepted so callers need not change; it does not affect the answer.
        """
        return max(1, self.max_pages)

    embedding_configured = bool(embedding_endpoint and embedding_key and embedding_deployment)
    search_configured = bool(search_endpoint and search_api_key and search_index_name)


settings = Settings()
