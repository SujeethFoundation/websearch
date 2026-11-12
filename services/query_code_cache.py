"""A table shared by every instance that remembers how each search question was embedded.

Embedding a question costs a network call (often 150 ms or more). The local search needs the question's 1-bit code (192
bytes) and its vector at 8 bits a dimension (1,536 bytes), so one question is under 2 KB in a Table Storage row, and an
instance that meets a question another instance (or an earlier run of itself) has already seen reads it in milliseconds
instead of embedding it again.

Rows hold the code and a timestamp, never the question's text. The row key is a hash of the embedding model, its
dimensions and the normalised question, so changing the model misses by itself and nothing stale is ever returned.
A table that is slow or failing is skipped for a while, so a search never waits on it for long.
"""
import hashlib
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from services.config import settings

log = logging.getLogger("sujeeth-web-search.query-cache")

_NOT_WORD = re.compile(r"[^\w']+", re.UNICODE)
MAX_PENDING_WRITES = 200


def normalize_question(text):
    """The question as a cache key: lower case, punctuation and runs of spaces collapsed to one space.
    "How do I volunteer?" and " how  do i volunteer " are the same question."""
    cleaned = str(text or "").casefold().replace("\u2019", "'")
    return " ".join(_NOT_WORD.sub(" ", cleaned).split())


def row_key_for(normalized, model, dimensions):
    digest = hashlib.sha256(f"{model}|{dimensions}|{normalized}".encode("utf-8")).hexdigest()
    return digest[:2], digest[2:]


class QueryCodeCache:
    """Point reads and best-effort writes of question codes in one table."""

    def __init__(self, connection_string=None, table_name=None, table=None):
        self._connection = connection_string if connection_string is not None else settings.local_index_connection
        self._name = table_name or settings.embed_cache_table
        self._table = table
        self._lock = threading.Lock()
        self._down_until = 0.0
        self._pending = 0
        self._writer = None
        self.stats = {"reads": 0, "hits": 0, "misses": 0, "errors": 0, "skipped": 0, "expired": 0,
                      "writes": 0, "write_errors": 0, "writes_dropped": 0}

    @property
    def enabled(self):
        return bool(self._table is not None or self._connection)

    def _client(self):
        if self._table is None:
            from azure.core.exceptions import ResourceExistsError
            from azure.data.tables import TableServiceClient
            service = TableServiceClient.from_connection_string(
                self._connection, connection_timeout=settings.embed_cache_read_timeout,
                read_timeout=settings.embed_cache_read_timeout, retry_total=1)
            table = service.get_table_client(self._name)
            try:
                table.create_table()
            except ResourceExistsError:
                pass
            self._table = table
        return self._table

    def _is_down(self):
        return time.monotonic() < self._down_until

    def _fail(self, what, exc):
        with self._lock:
            self.stats["errors"] += 1
            self._down_until = time.monotonic() + settings.embed_cache_cooldown
        log.warning("Question cache %s failed (%s: %s); skipping the table for %.0fs",
                    what, exc.__class__.__name__, str(exc)[:160], settings.embed_cache_cooldown)

    def get(self, normalized, model=None, dimensions=None):
        """(code, vector8) for this question, or None (not there, too old, wrong size, or the table is unavailable)."""
        if not self.enabled:
            return None
        if self._is_down():
            with self._lock:
                self.stats["skipped"] += 1
            return None
        model = model or settings.embedding_deployment
        dimensions = dimensions or settings.embedding_dimensions
        partition, row = row_key_for(normalized, model, dimensions)
        with self._lock:
            self.stats["reads"] += 1
        try:
            entity = self._client().get_entity(partition, row)
        except Exception as exc:
            if exc.__class__.__name__ == "ResourceNotFoundError":
                with self._lock:
                    self.stats["misses"] += 1
                return None
            self._fail("read", exc)
            return None
        code = entity.get("code")
        code = bytes(getattr(code, "value", code) or b"")
        vec8 = entity.get("q8")
        vec8 = bytes(getattr(vec8, "value", vec8) or b"")
        ttl = settings.embed_cache_ttl_days
        if ttl > 0 and time.time() - float(entity.get("ts") or 0) > ttl * 86400:
            with self._lock:
                self.stats["expired"] += 1
                self.stats["misses"] += 1
            return None
        if len(code) != (int(dimensions) + 7) // 8 or len(vec8) != int(dimensions):
            with self._lock:                  # an older row without the 8-bit vector counts as a miss and is replaced
                self.stats["misses"] += 1
            return None
        with self._lock:
            self.stats["hits"] += 1
        return code, vec8

    def _write(self, normalized, code, vec8, model, dimensions):
        partition, row = row_key_for(normalized, model, dimensions)
        try:
            self._client().upsert_entity({"PartitionKey": partition, "RowKey": row, "code": bytes(code),
                                          "q8": bytes(vec8), "ts": int(time.time())}, mode="replace")
            with self._lock:
                self.stats["writes"] += 1
        except Exception as exc:
            with self._lock:
                self.stats["write_errors"] += 1
            self._fail("write", exc)
        finally:
            with self._lock:
                self._pending -= 1

    def put(self, normalized, code, vec8, model=None, dimensions=None, wait=False):
        """Remember a code. In the background by default, so the search that found it does not wait; a backlog or a
        down table drops the write (the code is still in memory). wait=True writes now (used when seeding)."""
        if not self.enabled or self._is_down():
            return
        model = model or settings.embedding_deployment
        dimensions = dimensions or settings.embedding_dimensions
        with self._lock:
            if self._pending >= MAX_PENDING_WRITES and not wait:
                self.stats["writes_dropped"] += 1
                return
            self._pending += 1
            if self._writer is None and not wait:
                self._writer = ThreadPoolExecutor(max_workers=2, thread_name_prefix="qcache-write")
        if wait:
            self._write(normalized, code, vec8, model, dimensions)
        else:
            self._writer.submit(self._write, normalized, code, vec8, model, dimensions)

    def describe(self):
        with self._lock:
            out = dict(self.stats)
            out.update({"enabled": self.enabled, "table": self._name, "down": self._is_down(), "pending_writes": self._pending})
        return out
