"""A table shared by every instance that remembers finished search answers.

A search that one instance has answered costs the others the same CPU again when the same question comes to them. This table
lets an instance that meets a question another instance (or an earlier run of itself) already answered read the answer in
milliseconds instead of searching again. Off unless SEARCH_SHARED_CACHE_SECONDS is above 0.

Rows hold the answer (compressed JSON) and a timestamp, never the question's text. The row key is a hash of the index version,
the normalised question, the concepts, domains, limit and ranking, so a crawl (a new index version) misses by itself and a
stale answer is never returned. A table that is slow or failing is skipped for a while, so a search never waits on it long.
Answers over about 60 KB are not stored.
"""
import base64
import hashlib
import json
import logging
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor

from services.config import settings

log = logging.getLogger("sujeeth-web-search.result-store")

MAX_PACKED_CHARS = 60000
MAX_PENDING_WRITES = 100


def row_key_for(version, key):
    digest = hashlib.sha256(json.dumps([version, key], default=str, sort_keys=True).encode("utf-8")).hexdigest()
    return digest[:2], digest[2:]


class ResultStore:
    def __init__(self, connection_string=None, table_name=None, ttl_seconds=None, table=None):
        self._connection = connection_string if connection_string is not None else settings.local_index_connection
        self._name = table_name or settings.search_shared_cache_table
        self._ttl = settings.search_shared_cache_seconds if ttl_seconds is None else ttl_seconds
        self._table = table
        self._lock = threading.Lock()
        self._down_until, self._pending, self._writer = 0.0, 0, None
        self.stats = {"reads": 0, "hits": 0, "misses": 0, "errors": 0, "skipped": 0, "writes": 0, "write_errors": 0,
                      "writes_dropped": 0, "too_big": 0}

    @property
    def enabled(self):
        return self._ttl > 0 and bool(self._table is not None or self._connection)

    def _client(self):
        if self._table is None:
            from azure.core.exceptions import ResourceExistsError
            from azure.data.tables import TableServiceClient
            timeout = settings.embed_cache_read_timeout
            service = TableServiceClient.from_connection_string(
                self._connection, connection_timeout=timeout, read_timeout=timeout, retry_total=1)
            table = service.get_table_client(self._name)
            try:
                table.create_table()
            except ResourceExistsError:
                pass
            self._table = table
        return self._table

    def _fail(self, what, exc):
        with self._lock:
            self.stats["errors"] += 1
            self._down_until = time.monotonic() + settings.embed_cache_cooldown
        log.warning("Result store %s failed (%s: %s); skipping the table for %.0fs", what, exc.__class__.__name__,
                    str(exc)[:160], settings.embed_cache_cooldown)

    def get(self, version, key):
        """The stored answer for this index version and key, or None (absent, too old, unreadable, or table down)."""
        if not self.enabled or not version:
            return None
        if time.monotonic() < self._down_until:
            with self._lock:
                self.stats["skipped"] += 1
            return None
        partition, row = row_key_for(version, key)
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
        try:
            if time.time() - float(entity.get("ts") or 0) > self._ttl:
                raise ValueError("expired")
            answer = json.loads(zlib.decompress(base64.b64decode(entity["answer"])).decode("utf-8"))
        except Exception:
            with self._lock:
                self.stats["misses"] += 1
            return None
        with self._lock:
            self.stats["hits"] += 1
        return answer

    def _write(self, version, key, packed):
        partition, row = row_key_for(version, key)
        try:
            self._client().upsert_entity({"PartitionKey": partition, "RowKey": row, "answer": packed,
                                          "ts": int(time.time())}, mode="replace")
            with self._lock:
                self.stats["writes"] += 1
        except Exception as exc:
            with self._lock:
                self.stats["write_errors"] += 1
            self._fail("write", exc)
        finally:
            with self._lock:
                self._pending -= 1

    def put(self, version, key, answer, wait=False):
        """Remember an answer, in the background unless wait=True. A backlog, a down table or an oversized answer drops it."""
        if not self.enabled or not version or time.monotonic() < self._down_until:
            return
        packed = base64.b64encode(zlib.compress(json.dumps(answer, default=str).encode("utf-8"))).decode("ascii")
        if len(packed) > MAX_PACKED_CHARS:
            with self._lock:
                self.stats["too_big"] += 1
            return
        with self._lock:
            if self._pending >= MAX_PENDING_WRITES:
                self.stats["writes_dropped"] += 1
                return
            self._pending += 1
            if self._writer is None and not wait:
                self._writer = ThreadPoolExecutor(max_workers=2, thread_name_prefix="result-store")
        if wait:
            self._write(version, key, packed)
        else:
            self._writer.submit(self._write, version, key, packed)
