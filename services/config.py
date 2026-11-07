import os


def parse_limits(raw):
    """Parse "host=500,other.org=200" into {"host": 500, "other.org": 200}. Bad or non-positive entries are ignored."""
    limits = {}
    for part in str(raw or "").split(","):
        host, _, value = part.partition("=")
        host = host.strip().lower()
        try:
            number = int(value.strip())
        except ValueError:
            continue
        if host and number > 0:
            limits[host] = number
    return limits


class Settings:
    ontology_api_url = os.getenv("ONTOLOGY_API_URL", "").rstrip("/")
    ontology_function_key = os.getenv("ONTOLOGY_FUNCTION_KEY", "")
    local_data_dir = os.getenv("LOCAL_DATA_DIR", "data")
    blob_connection_string = os.getenv("BLOB_STORAGE_CONNECTION_STRING", "")
    blob_container = os.getenv("BLOB_CONTAINER", "web-content")
    azure_storage_connection = os.getenv("AzureWebJobsStorage", "")
    crawl_queue = os.getenv("CRAWL_QUEUE_NAME", "web-crawl-pages")
    crawl_poison_queue = os.getenv("CRAWL_POISON_QUEUE_NAME", "web-crawl-pages-poison")
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
    search_index_name = os.getenv("AZURE_SEARCH_INDEX_NAME", "sf-knowledge")
    search_api_version = os.getenv("AZURE_SEARCH_API_VERSION", "2025-09-01")
    search_batch_size = max(1, min(int(os.getenv("AZURE_SEARCH_BATCH_SIZE", "100")), 500))
    # The vector field that receives chunk vectors and is searched. To move to a compressed field in the same
    # index, add that field to the index definition, point AZURE_SEARCH_VECTOR_FIELD at it, and list the old field
    # in AZURE_SEARCH_NULL_VECTOR_FIELDS so every rewritten chunk drops its old vector and frees that quota.
    search_vector_field = os.getenv("AZURE_SEARCH_VECTOR_FIELD", "contentVector").strip() or "contentVector"
    # When true, Blob Storage is the only copy of chunk text and vectors. The index keeps no retrievable text and no
    # stored vector copy, so every index write replaces the whole document and result text is read from blobs.
    search_blob_backed = os.getenv("AZURE_SEARCH_BLOB_BACKED", "false").strip().lower() in ("1", "true", "yes", "on")
    search_null_vector_fields = tuple(
        x.strip() for x in os.getenv("AZURE_SEARCH_NULL_VECTOR_FIELDS", "").split(",") if x.strip()
    )
    index_backfill_batch_size = max(1, min(int(os.getenv("INDEX_BACKFILL_BATCH_SIZE", "10")), 25))

    max_pages = int(os.getenv("MAX_PAGES_PER_DOMAIN", "10000"))
    max_crawl_depth = int(os.getenv("MAX_CRAWL_DEPTH", "5"))
    crawl_delay = float(os.getenv("CRAWL_DELAY_SECONDS", "0.5"))
    # Per-domain overrides of MAX_PAGES_PER_DOMAIN, e.g. MAX_PAGES_BY_DOMAIN="www.themuse.com=500" (host=limit, comma separated).
    page_limits = parse_limits(os.getenv("MAX_PAGES_BY_DOMAIN", ""))
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

    def page_limit_for(self, domain):
        """Pages per domain: the domain's MAX_PAGES_BY_DOMAIN entry (apex or www), else MAX_PAGES_PER_DOMAIN.

        This one number caps the crawl job's total pages and also the page URLs collected from sitemaps.
        """
        host = str(domain or "").lower().strip().strip(".")
        host = host.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
        apex = host[4:] if host.startswith("www.") else host
        for name in (host, apex, "www." + apex):
            if name in self.page_limits:
                return self.page_limits[name]
        return max(1, self.max_pages)

    embedding_configured = bool(embedding_endpoint and embedding_key and embedding_deployment)
    search_configured = bool(search_endpoint and search_api_key and search_index_name)


settings = Settings()
