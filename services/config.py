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

    # How many nearest neighbours the vector half puts into the rank fusion. 0 keeps max(50, limit * 10).
    search_vector_k = _int("SEARCH_VECTOR_K", 0, 0, 1000)

    # ---- Search inside the app over chunks kept in Azure Table Storage (see services/local_index.py) ----
    # The table is the index: every chunk the crawler writes goes to it, and every instance keeps a copy in memory.
    local_index_connection = (os.getenv("LOCAL_INDEX_CONNECTION_STRING", "").strip()
                              or blob_connection_string or azure_storage_connection)
    local_index_table = "".join(ch for ch in os.getenv("LOCAL_INDEX_TABLE", "searchchunks") if ch.isalnum()) or "searchchunks"
    # Keep every chunk's text in memory (fast, about 4 KB a chunk) or fetch the few chunks a search returns from the table.
    local_index_text_in_memory = _flag("LOCAL_INDEX_TEXT_IN_MEMORY", True)
    local_index_poll_seconds = _int("LOCAL_INDEX_POLL_SECONDS", 60, 5)
    local_index_min_reload_seconds = _int("LOCAL_INDEX_MIN_RELOAD_SECONDS", 600, 0)
    local_index_title_weight = _int("LOCAL_INDEX_TITLE_WEIGHT", 3, 1, 20)
    local_index_preload = _flag("LOCAL_INDEX_PRELOAD", True)
    # While an instance is still loading its first index, a search normally waits for the load, and holds one of the
    # instance's concurrent request slots meanwhile. true: it fails at once (HTTP 503 with Retry-After) so the slot
    # stays free and the caller can retry or be sent elsewhere. Off by default.
    local_index_fail_fast = _flag("LOCAL_INDEX_FAIL_FAST", False)
    local_index_retry_after = _int("LOCAL_INDEX_RETRY_AFTER_SECONDS", 3, 1, 60)
    # The 1-bit codes find a shortlist of close chunks quickly, but they rank poorly among close neighbours. Each chunk
    # also keeps its vector at 8 bits a dimension (about 1.5 KB), and the best LOCAL_RESCORE_POOL chunks by code are
    # re-ranked by cosine similarity on those. Needs the table rows to carry the vectors (run the backfill again after
    # upgrading); until every row has one, the search quietly uses the codes alone.
    # What the index does with common words ("the", "is", "to"). They are stored with every chunk and, by default,
    # searched for like any other word; they score low because nearly every chunk has them. "english" drops a list of 33
    # of them from questions only.
    local_index_stopwords = os.getenv("LOCAL_INDEX_STOPWORDS", "none").strip().lower()
    # How many keyword-ranked chunks go into the rank fusion with the vector results (the vector side takes
    # SEARCH_VECTOR_K). A chunk strong on vectors but ranked 80th on keywords still gets its keyword credit. 0 means 1,000.
    local_text_recall = _int("LOCAL_TEXT_RECALL", 0, 0, 10000)
    # Ranking beyond the plain rank fusion of the keyword and vector lists:
    # - A chunk tagged with a concept the question itself named ("week 4", "mentor") ranks above one that only passes the
    #   filter through a concept the ontology added by relationship. The bonus is LOCAL_CONCEPT_BOOST times the share of the
    #   question's own concepts the chunk carries, on the scale of a first place in one ranked list. 0 turns it off.
    # - At most LOCAL_MAX_PER_PAGE chunks of one page in an answer (the same page reached as www. and without it counts as
    #   one page, and the same chunk of it is returned once). 0 removes the limit.
    local_concept_boost = _float("LOCAL_CONCEPT_BOOST", 1.0)
    local_max_per_page = _int("LOCAL_MAX_PER_PAGE", 2, 0, 50)   # only used when SEARCH_UNIQUE_PAGES is false
    # - Keyword scoring ignores word order: "students completed Career Circles" scores like the same four words scattered
    #   over a page. A chunk whose text or title holds the question's neighbouring words next to each other gets a bonus:
    #   LOCAL_PHRASE_BOOST times the share of the question's word pairs it holds (each pair weighted by how rare its commoner
    #   word is, so "how many" counts for almost nothing), on the same scale as the concept bonus. Checked on the best
    #   LOCAL_PHRASE_POOL candidates; needs the text in memory (LOCAL_INDEX_TEXT_IN_MEMORY). 0 turns it off.
    local_phrase_boost = _float("LOCAL_PHRASE_BOOST", 1.0)
    local_phrase_pool = _int("LOCAL_PHRASE_POOL", 100, 10, 1000)
    local_rescore = _flag("LOCAL_RESCORE", True)
    # Every result is a different page: a page is returned once, as its best chunk, and the www. and bare copies of an
    # address count as one page. The answer holds fewer than `limit` results only when fewer pages match. false allows
    # several chunks of one page again (LOCAL_MAX_PER_PAGE applies).
    search_unique_pages = _flag("SEARCH_UNIQUE_PAGES", True)
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
    # A search that repeats within SEARCH_CACHE_SECONDS (same question once case, spacing and trailing punctuation
    # are ignored, same concepts, domains and limit) is answered from memory instead of querying the index again.
    # 0 turns it off. The index only changes when pages are crawled, so a few minutes of reuse is safe; another
    # instance can serve an older answer for up to this long after a crawl. Each entry is roughly 10 KB.
    search_cache_seconds = _int("SEARCH_CACHE_SECONDS", 0, 0, 86400)
    search_cache_max_entries = _int("SEARCH_CACHE_MAX_ENTRIES", 500, 10, 100000)
    # Chunks per backfill task. A "missing" backfill asks the table about each page of the task's chunks, so a larger
    # batch means fewer round trips.
    index_backfill_batch_size = max(1, min(int(os.getenv("INDEX_BACKFILL_BATCH_SIZE", "50")), 100))

    # A prebuilt copy of the in-memory index in Blob Storage (see services/index_snapshot.py). An instance that starts or
    # reloads reads it instead of scanning and parsing the whole table, when the table's version row still matches the
    # snapshot's. Any problem with it means the table is scanned as before.
    index_snapshot_enabled = _flag("INDEX_SNAPSHOT_ENABLED", True)
    # After an instance has built the index from the table, it saves a snapshot for the others (one instance builds it,
    # whichever claims it first). false: only POST /api/index/snapshot builds one.
    index_snapshot_autobuild = _flag("INDEX_SNAPSHOT_AUTOBUILD", True)
    index_snapshot_prefix = os.getenv("INDEX_SNAPSHOT_PREFIX", "index-snapshot").strip().strip("/") or "index-snapshot"
    # Parallel range requests per blob download or upload.
    index_snapshot_concurrency = _int("INDEX_SNAPSHOT_CONCURRENCY", 4, 1, 16)
    # Files of older builds are deleted once nothing has written to them for this long (a reader may be mid-download).
    index_snapshot_keep_seconds = _int("INDEX_SNAPSHOT_KEEP_SECONDS", 3600, 0)

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
    # Crawl job history. A job with no update for this many days no longer blocks a new crawl of its domain.
    open_job_stale_days = _int("OPEN_JOB_STALE_DAYS", 3, 1)
    # Jobs (and their page rows) not updated for this many days are deleted. The newest job of each domain is kept.
    job_retention_days = _int("JOB_RETENTION_DAYS", 30, 7)
    job_cleanup_enabled = _flag("JOB_CLEANUP_ENABLED", True)

    # Index writes and embeddings (these were read in search_service.py, queue_worker.py and embedding_service.py).
    # After the index table stays busy or unreachable, other writes on this instance skip straight to deferral for this
    # many seconds, so workers keep crawling instead of each waiting out the failure.
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


settings = Settings()
