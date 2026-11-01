import os


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
    index_backfill_batch_size = max(1, min(int(os.getenv("INDEX_BACKFILL_BATCH_SIZE", "10")), 25))

    max_pages = int(os.getenv("MAX_PAGES_PER_DOMAIN", "1000"))
    max_crawl_depth = int(os.getenv("MAX_CRAWL_DEPTH", "5"))
    crawl_delay = float(os.getenv("CRAWL_DELAY_SECONDS", "0.5"))
    max_sitemap_urls = int(os.getenv("MAX_SITEMAP_URLS", "5000"))
    max_sitemaps = int(os.getenv("MAX_SITEMAPS_PER_DOMAIN", "100"))
    timeout = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "20"))
    max_query_concepts = int(os.getenv("MAX_QUERY_CONCEPTS", "5"))

    embedding_configured = bool(embedding_endpoint and embedding_key and embedding_deployment)
    search_configured = bool(search_endpoint and search_api_key and search_index_name)


settings = Settings()
