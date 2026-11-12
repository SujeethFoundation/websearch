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


def _flag(name, default):
    """A true/false app setting. A blank value falls back to the default."""
    value = os.getenv(name, "").strip().lower()
    if not value:
        return bool(default)
    return value in ("1", "true", "yes", "on")


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
    # Query tuning for cost on the Serverless tier, where each query uses Compute Units. Every default keeps
    # the behavior the app had before these settings existed. Change one at a time and compare the Compute
    # Units per search that /health reports (ai_search_compute) and the relevance of the results.
    #   SEARCH_MAX_TEXT_RECALL: hybridSearch.maxTextRecallSize, how many keyword matches feed the rank fusion.
    #     The service default is 1,000 and compute scales roughly with it. 0 sends nothing (service default).
    #   SEARCH_VECTOR_K: nearest neighbours the vector query fetches. 0 keeps max(50, limit * 10).
    #   SEARCH_COUNT: false stops asking the service to count every match (total_matches is then null).
    #   SEARCH_FIELDS: comma-separated fields the keyword half searches. Blank searches every searchable field.
    search_max_text_recall = _int("SEARCH_MAX_TEXT_RECALL", 0, 0, 10000)
    search_vector_k = _int("SEARCH_VECTOR_K", 0, 0, 1000)
    search_count = os.getenv("SEARCH_COUNT", "true").strip().lower() in ("1", "true", "yes", "on")
    search_fields = os.getenv("SEARCH_FIELDS", "").strip()

    # ---- Search inside the app over chunks kept in Azure Table Storage (see services/local_index.py) ----
    # SEARCH_BACKEND: azure = every search goes to Azure AI Search (the default and the behavior before this existed);
    # shadow = Azure AI Search answers, and a sample of searches is also run locally and compared (see
    # LOCAL_SHADOW_SAMPLE), to check the local results before switching; local = the app answers from memory.
    search_backend = (os.getenv("SEARCH_BACKEND", "azure").strip().lower() or "azure")
    if search_backend not in ("azure", "local", "shadow"):
        search_backend = "azure"
    # Whether chunks are still written to (and deleted from) Azure AI Search. Turn off once nothing needs the index.
    ai_search_writes = _flag("AI_SEARCH_WRITES", True)
    # Whether chunks are also written to the Table Storage table the local index reads. Turn on before the backfill.
    local_index_write = _flag("LOCAL_INDEX_WRITE", False)
    local_index_connection = (os.getenv("LOCAL_INDEX_CONNECTION_STRING", "").strip()
                              or blob_connection_string or azure_storage_connection)
    local_index_table = "".join(ch for ch in os.getenv("LOCAL_INDEX_TABLE", "searchchunks") if ch.isalnum()) or "searchchunks"
    # Keep every chunk's text in memory (fast, about 4 KB a chunk) or fetch the few chunks a search returns from the table.
    local_index_text_in_memory = _flag("LOCAL_INDEX_TEXT_IN_MEMORY", True)
    local_index_poll_seconds = _int("LOCAL_INDEX_POLL_SECONDS", 60, 5)
    local_index_min_reload_seconds = _int("LOCAL_INDEX_MIN_RELOAD_SECONDS", 600, 0)
    local_index_title_weight = _int("LOCAL_INDEX_TITLE_WEIGHT", 3, 1, 20)
    local_index_preload = _flag("LOCAL_INDEX_PRELOAD", True)
    # The 1-bit codes find a shortlist of close chunks quickly, but they rank poorly among close neighbours. Each chunk
    # also keeps its vector at 8 bits a dimension (about 1.5 KB), and the best LOCAL_RESCORE_POOL chunks by code are
    # re-ranked by cosine similarity on those. Needs the table rows to carry the vectors (run the local backfill again
    # after upgrading); until every row has one, the search quietly uses the codes alone.
    # What the index does with common words ("the", "is", "to"). The Azure index uses the default Standard Lucene analyzer,
    # which keeps them (its stop word list is empty), so they are stored with every chunk and, by default, searched for like
    # any other word; they score low because nearly every chunk has them. "english" drops a list of 33 of them from questions.
    local_index_stopwords = os.getenv("LOCAL_INDEX_STOPWORDS", "none").strip().lower()
    # How many keyword-ranked chunks go into the rank fusion with the vector results. Azure AI Search feeds up to 1,000
    # (hybridSearch.maxTextRecallSize, default 1000) to its fusion, so a chunk strong on vectors but ranked 80th on keywords
    # still gets its keyword credit. 0 uses SEARCH_MAX_TEXT_RECALL when that is set (so the two engines match), else 1000.
    local_text_recall = _int("LOCAL_TEXT_RECALL", 0, 0, 10000)
    # When a shadow comparison finds different top pages, also run each engine's keyword half and vector half on their own
    # (two extra Azure AI Search queries, at most one such comparison every LOCAL_SHADOW_DIAGNOSE_MIN_SECONDS), so the
    # difference can be put down to the keyword half, the vector half or the fusion. For investigating, not for always.
    local_shadow_diagnose = _flag("LOCAL_SHADOW_DIAGNOSE", False)
    local_shadow_diagnose_min_seconds = _float("LOCAL_SHADOW_DIAGNOSE_MIN_SECONDS", 5)
    # Ranking beyond what Azure AI Search does (local search only; the shadow comparison leaves both out, so it still
    # measures whether the two engines agree):
    # - A chunk tagged with a concept the question itself named ("week 4", "mentor") ranks above one that only passes the
    #   filter through a concept the ontology added by relationship. The bonus is LOCAL_CONCEPT_BOOST times the share of the
    #   question's own concepts the chunk carries, on the scale of a first place in one ranked list. 0 turns it off.
    # - At most LOCAL_MAX_PER_PAGE chunks of one page in an answer (the same page reached as www. and without it counts as
    #   one page, and the same chunk of it is returned once). 0 removes the limit.
    local_concept_boost = _float("LOCAL_CONCEPT_BOOST", 1.0)
    local_max_per_page = _int("LOCAL_MAX_PER_PAGE", 2, 0, 50)
    # - Keyword scoring ignores word order: "students completed Career Circles" scores like the same four words scattered
    #   over a page. A chunk whose text or title holds the question's neighbouring words next to each other gets a bonus:
    #   LOCAL_PHRASE_BOOST times the share of the question's word pairs it holds (each pair weighted by how rare its commoner
    #   word is, so "how many" counts for almost nothing), on the same scale as the concept bonus. Checked on the best
    #   LOCAL_PHRASE_POOL candidates; needs the text in memory (LOCAL_INDEX_TEXT_IN_MEMORY). 0 turns it off.
    local_phrase_boost = _float("LOCAL_PHRASE_BOOST", 1.0)
    local_phrase_pool = _int("LOCAL_PHRASE_POOL", 100, 10, 1000)
    local_rescore = _flag("LOCAL_RESCORE", True)
    local_rescore_pool = _int("LOCAL_RESCORE_POOL", 300, 20, 5000)
    # Search-question embeddings are remembered so a question is embedded once, not once per instance. Only the 1-bit
    # code the local search uses is kept (192 bytes for 1536 dimensions), never the question's text. Memory first, then a
    # table shared by every instance, then the embedding call.
    embed_cache_shared = _flag("EMBED_CACHE_SHARED", True)
    embed_cache_table = ("".join(ch for ch in os.getenv("EMBED_CACHE_TABLE", "").strip() if ch.isalnum())
                         or (local_index_table + "qcache"))
    embed_cache_memory_entries = _int("EMBED_CACHE_MEMORY_ENTRIES", 10000, 100)
    embed_cache_ttl_days = _float("EMBED_CACHE_TTL_DAYS", 30)                     # 0 keeps entries until the model changes
    embed_cache_read_timeout = max(0.2, _float("EMBED_CACHE_TIMEOUT_SECONDS", 2))  # one table read or write
    embed_cache_cooldown = max(1.0, _float("EMBED_CACHE_COOLDOWN_SECONDS", 30))   # after a failure, skip the table this long
    # When the local index is not ready, answer from Azure AI Search instead of failing.
    local_search_fallback = _flag("LOCAL_SEARCH_FALLBACK", True)
    # Share of searches (0 to 1) that shadow mode also runs locally and compares with Azure AI Search.
    local_shadow_sample = min(1.0, max(0.0, _float("LOCAL_SHADOW_SAMPLE", 0.0)))
    # A search that repeats within SEARCH_CACHE_SECONDS (same question once case, spacing and trailing punctuation
    # are ignored, same concepts, domains and limit) is answered from memory instead of querying the index again.
    # 0 turns it off. The index only changes when pages are crawled, so a few minutes of reuse is safe; another
    # instance can serve an older answer for up to this long after a crawl. Each entry is roughly 10 KB.
    search_cache_seconds = _int("SEARCH_CACHE_SECONDS", 0, 0, 86400)
    search_cache_max_entries = _int("SEARCH_CACHE_MAX_ENTRIES", 500, 10, 100000)
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
    # Chunks per backfill task. A "missing" backfill asks the index about each task's chunks with one query, so
    # a larger batch means fewer queries.
    index_backfill_batch_size = max(1, min(int(os.getenv("INDEX_BACKFILL_BATCH_SIZE", "50")), 100))

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
    # The nightly reconcile queues a backfill only when the index holds more than this percent fewer chunks than
    # Blob Storage. A few stored chunks have no text and are never indexed, so some gap is normal.
    index_reconcile_tolerance_pct = _int("INDEX_RECONCILE_TOLERANCE_PCT", 1, 0, 100)
    # Set to true only for an index whose concepts and domain fields are facetable. In sf-knowledge-v2 they are not,
    # so tag counts come from a sample of the stored chunks.
    concepts_facetable = _flag("INDEX_CONCEPTS_FACETABLE", False)
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
    # Search-question embeddings sit on the search path, so they are bounded: one request waits at most
    # EMBED_QUERY_TIMEOUT_SECONDS, there are at most EMBED_QUERY_ATTEMPTS tries, and a throttled answer that asks for a
    # wait longer than EMBED_QUERY_MAX_WAIT_SECONDS is not waited out. The search then continues with keywords only
    # (search_mode "keyword") instead of holding the caller. Indexing keeps its own, longer retries.
    embed_query_timeout = max(1.0, _float("EMBED_QUERY_TIMEOUT_SECONDS", 8))
    embed_query_attempts = _int("EMBED_QUERY_ATTEMPTS", 2, 1, 6)
    embed_query_max_wait = max(0.0, _float("EMBED_QUERY_MAX_WAIT_SECONDS", 2))
    # After a search question fails to embed, skip the endpoint for this many seconds and search by keywords only, so an
    # outage costs one timeout rather than one per search. 0 turns it off.
    embed_query_cooldown = max(0.0, _float("EMBED_QUERY_COOLDOWN_SECONDS", 20))
    index_max_deferrals = _int("INDEX_MAX_DEFERRALS", 12, 0)

    # How long the ontology client keeps a successful manifest or domain-concept answer.
    ontology_cache_seconds = _int("ONTOLOGY_CACHE_SECONDS", 120, 0)
    # Most entries the ontology answer cache keeps. The oldest tenth is dropped when it is exceeded.
    ontology_cache_max_entries = _int("ONTOLOGY_CACHE_MAX_ENTRIES", 2000, 10)
    # Connections kept open per host by each HTTP session. requests keeps only 10 by default; an instance
    # that runs more requests than that at once opens extra connections and throws them away after one use
    # ("Connection pool is full, discarding connection"), which costs a new TLS handshake every time.
    http_pool_size = _int("HTTP_POOL_SIZE", 64, 1, 1000)

    def page_limit_for(self, domain=None):
        """Pages per domain: MAX_PAGES_PER_DOMAIN, the same for every domain.

        This one number caps a crawl job's total pages and also the page URLs collected from sitemaps.
        The domain is accepted so callers need not change; it does not affect the answer.
        """
        return max(1, self.max_pages)

    embedding_configured = bool(embedding_endpoint and embedding_key and embedding_deployment)
    search_configured = bool(search_endpoint and search_api_key and search_index_name)


settings = Settings()
