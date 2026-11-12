"""In-process hybrid search over chunks kept in Azure Table Storage.

Every instance loads the chunks from one Table Storage table into memory and answers searches itself, so a search
costs CPU on the instance it runs on instead of a metered query to Azure AI Search.

What is stored, one table row per chunk (PartitionKey = the page id, RowKey = the chunk number, zero padded):
  title, url, domain, concepts   what the filters and the results need
  text                           the chunk text (optional in memory, see LOCAL_INDEX_TEXT_IN_MEMORY)
  vec                            the embedding reduced to one bit per dimension (192 bytes for 1,536 dimensions)
  tid, tcnt                      the chunk's words, as 64-bit hashes and counts, worked out once when the chunk is
                                 written so that loading an instance does not have to read and tokenize every text

How a search works (the same shape as the Azure AI Search hybrid query it replaces):
  1. keep the chunks whose domain is in the list and that carry at least one of the concepts
  2. keyword half: BM25 over the question's words (any word may match), best 50
  3. vector half: Hamming distance between the question's bit vector and each chunk's, nearest 50
  4. merge the two lists by reciprocal rank fusion (1 / (60 + rank)) and return the best few

A separate small table holds one row with a random version token. Writers change it after they change chunks, and
every instance checks it now and then; a changed token makes the instance rebuild its index in the background and
swap it in. A failed rebuild keeps the index it already has.
"""
import gc
import hashlib
import logging
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from functools import lru_cache

import numpy as np

from services.config import settings

log = logging.getLogger("sujeeth-web-search.local-index")

FORMAT_VERSION = 1
BM25_K1 = 1.2
BM25_B = 0.75
RRF_K = 60
META_PARTITION = "index"
META_ROW = "version"
MAX_TEXT_CHARS = 30000  # a Table Storage string property holds at most 32,768 characters

# Lucene's default English stop words, which the standard analyzer of the search service drops as well.
STOPWORDS = frozenset(
    "a an and are as at be but by for if in into is it no not of on or such that the their then there these they "
    "this to was will with".split()
)
_TOKEN = re.compile(r"[^\W_]+")  # runs of letters and digits in any script


class LocalIndexUnavailable(RuntimeError):
    """The in-memory index is not loaded, is empty, or could not be loaded."""


# ---------------------------------------------------------------------------------------------------------------
# Text and vectors
# ---------------------------------------------------------------------------------------------------------------

def tokens(text):
    """Lower-cased words, without stop words."""
    return [t for t in _TOKEN.findall(str(text or "").lower()) if t not in STOPWORDS]


@lru_cache(maxsize=500000)
def term_hash(token):
    """A stable 64-bit number for a word (Python's own hash() changes from run to run)."""
    return int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "little")


def query_terms(text):
    """The distinct words of a question, as an array of hashes."""
    seen = sorted({term_hash(t) for t in tokens(text)})
    return np.array(seen, dtype=np.uint64)


def analyze(title, content, title_weight=3):
    """(hashes, counts) for one chunk as raw bytes: the word hashes in ascending order and how often each occurs.

    Words in the title count title_weight times, like the title boost in the wiki search. Counts stop at 255."""
    counts = {}
    for token in tokens(content):
        h = term_hash(token)
        counts[h] = counts.get(h, 0) + 1
    for token in tokens(title):
        h = term_hash(token)
        counts[h] = counts.get(h, 0) + max(1, int(title_weight))
    ordered = sorted(counts)
    hashes = np.array(ordered, dtype="<u8")
    values = np.array([min(255, counts[h]) for h in ordered], dtype=np.uint8)
    return hashes.tobytes(), values.tobytes()


def binary_code(vector):
    """One bit per dimension (set when the value is above zero), packed into bytes."""
    return np.packbits(np.asarray(vector, dtype=np.float32) > 0).tobytes()


def _words(codes, nbytes):
    """Rows of packed bits as an (n, words) array of 64-bit integers, padding each row with zero bits."""
    padded = (nbytes + 7) // 8 * 8
    block = np.zeros((len(codes), padded), dtype=np.uint8)
    for i, code in enumerate(codes):
        block[i, :len(code)] = np.frombuffer(code, dtype=np.uint8)
    return np.ascontiguousarray(block).view(np.uint64)


_POP8 = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


def _popcount_rows(words):
    """Number of set bits in each row of a uint64 array."""
    if hasattr(np, "bitwise_count"):
        return np.bitwise_count(words).sum(axis=1, dtype=np.int32)
    return _POP8[words.view(np.uint8)].sum(axis=1, dtype=np.int32)


def split_chunk_id(chunk_id):
    """('<page id>', number) from '<page id>-<number>'."""
    page_id, _, number = str(chunk_id).rpartition("-")
    if not page_id or not number.isdigit():
        raise ValueError(f"chunk id must look like <page>-<number>: {chunk_id!r}")
    return page_id, int(number)


def build_entity(doc_id, title, url, domain, concepts, content, vector, title_weight=None):
    """The table row for one chunk."""
    page_id, number = split_chunk_id(doc_id)
    weight = settings.local_index_title_weight if title_weight is None else title_weight
    tid, tcnt = analyze(title, content, weight)
    return {
        "PartitionKey": page_id,
        "RowKey": str(number).zfill(6),
        "id": str(doc_id),
        "title": str(title or "")[:MAX_TEXT_CHARS],
        "url": str(url or ""),
        "domain": str(domain or "").lower(),
        "concepts": ",".join(str(c) for c in (concepts or [])),
        "text": str(content or "")[:MAX_TEXT_CHARS],
        "vec": binary_code(vector),
        "tid": tid,
        "tcnt": tcnt,
        "fmt": FORMAT_VERSION,
    }


def entity_from_search_doc(item):
    """The table row for a document built by SearchService._search_doc (it carries the vector)."""
    return build_entity(
        item["id"], item.get("title"), item.get("url"), item.get("domain"), item.get("concepts"),
        item.get("content"), item[settings.search_vector_field],
    )


def row_from_entity(entity):
    """The dict the index is built from. Binary values may come back as bytes or as a wrapper with a .value."""
    def raw(value):
        value = getattr(value, "value", value)
        return bytes(value) if value is not None else b""

    concepts = [c for c in str(entity.get("concepts") or "").split(",") if c]
    return {
        "id": str(entity.get("id") or f"{entity['PartitionKey']}-{int(entity['RowKey'])}"),
        "title": entity.get("title") or "",
        "url": entity.get("url") or "",
        "domain": str(entity.get("domain") or "").lower(),
        "concepts": concepts,
        "text": entity.get("text"),
        "vec": raw(entity.get("vec")),
        "tid": raw(entity.get("tid")),
        "tcnt": raw(entity.get("tcnt")),
    }


# ---------------------------------------------------------------------------------------------------------------
# The in-memory index
# ---------------------------------------------------------------------------------------------------------------

class LocalIndex:
    """An immutable snapshot. Built once from rows, then only read, so any number of searches can use it at once."""

    def __init__(self, rows, keep_text=True):
        usable = []
        skipped = 0
        width = 0
        for r in rows:
            vec, tid, tcnt = r.get("vec") or b"", r.get("tid") or b"", r.get("tcnt") or b""
            if not vec or len(tid) % 8 or len(tid) // 8 != len(tcnt):
                skipped += 1
                continue
            width = width or len(vec)
            if len(vec) != width:
                skipped += 1
                continue
            usable.append(r)
        if skipped:
            log.warning("Local index: %d row(s) were unusable (missing or inconsistent vector or word data) and were left out", skipped)
        self.skipped = skipped
        n = len(usable)
        self.n = n
        self.ids = [r["id"] for r in usable]
        self.titles = [r["title"] for r in usable]
        self.urls = [r["url"] for r in usable]
        self.concepts = [list(r["concepts"]) for r in usable]
        self.texts = [r.get("text") or "" for r in usable] if keep_text else None

        # domains and concepts become small integers and bits
        self.domain_names = sorted({r["domain"] for r in usable})
        domain_code = {d: i for i, d in enumerate(self.domain_names)}
        self.domain_codes = np.array([domain_code[r["domain"]] for r in usable], dtype=np.int32)
        self.concept_names = sorted({c.lower() for r in usable for c in r["concepts"]})
        concept_bit = {c: i for i, c in enumerate(self.concept_names)}
        self.concept_words = max(1, (len(self.concept_names) + 63) // 64)
        bits = np.zeros((n, self.concept_words), dtype=np.uint64)
        for row, r in enumerate(usable):
            for c in r["concepts"]:
                b = concept_bit[c.lower()]
                bits[row, b // 64] |= np.uint64(1) << np.uint64(b % 64)
        self.concept_bits = bits

        # vectors
        self.code_bytes = width
        self.codes = _words([r["vec"] for r in usable], width) if n else np.zeros((0, 1), dtype=np.uint64)

        # keyword postings, as a column-compressed matrix: for each word, the chunks that contain it and their weights
        arrays = [np.frombuffer(r["tid"], dtype="<u8") for r in usable]
        counts = [np.frombuffer(r["tcnt"], dtype=np.uint8) for r in usable]
        lengths = np.array([len(a) for a in arrays], dtype=np.int64)
        if n and lengths.sum():
            # Built with in-place arithmetic and each large array dropped as soon as it is not needed, because this is
            # where an instance's memory peaks while the index is built.
            all_hashes = np.concatenate(arrays)
            all_counts = np.concatenate(counts)
            del arrays, counts
            all_docs = np.repeat(np.arange(n, dtype=np.int32), lengths)
            self.vocab, inverse = np.unique(all_hashes, return_inverse=True)
            del all_hashes
            cols = inverse.astype(np.int32)
            del inverse
            df = np.bincount(cols, minlength=len(self.vocab))
            doc_len = np.bincount(all_docs, weights=all_counts, minlength=n)
            avg_len = float(doc_len.mean()) or 1.0
            idf = np.log(1.0 + (n - df + 0.5) / (df + 0.5)).astype(np.float32)
            doc_norm = (BM25_K1 * (1.0 - BM25_B + BM25_B * doc_len / avg_len)).astype(np.float32)
            tf = all_counts.astype(np.float32)
            del all_counts
            weights = tf * np.float32(BM25_K1 + 1.0)
            tf += doc_norm[all_docs]
            weights /= tf
            del tf
            weights *= idf[cols]
            order = np.argsort(cols, kind="stable")
            del cols
            self.post_docs = all_docs[order]
            del all_docs
            self.post_weights = weights[order]
            del weights, order
            self.post_ptr = np.concatenate([[0], np.cumsum(df)]).astype(np.int64)
        else:
            self.vocab = np.zeros(0, dtype=np.uint64)
            self.post_docs = np.zeros(0, dtype=np.int32)
            self.post_weights = np.zeros(0, dtype=np.float32)
            self.post_ptr = np.zeros(1, dtype=np.int64)

        # lookups for reading pages and single chunks
        self.by_id = {cid: i for i, cid in enumerate(self.ids)}
        pages = {}
        for i, cid in enumerate(self.ids):
            page_id, number = split_chunk_id(cid)
            pages.setdefault(page_id, []).append((number, i))
        self.pages = {p: [i for _, i in sorted(items)] for p, items in pages.items()}
        by_url = {}
        for page_rows in self.pages.values():
            for i in page_rows:
                by_url.setdefault(self.urls[i], []).append(i)
        self.by_url = by_url

    # -- sizes -----------------------------------------------------------------------------------------------
    def memory_bytes(self):
        arrays = (self.codes, self.concept_bits, self.domain_codes, self.vocab, self.post_docs, self.post_weights, self.post_ptr)
        total = sum(a.nbytes for a in arrays)
        total += sum(len(s) * 2 for s in (self.texts or [])) + sum(len(s) for s in self.ids) + sum(len(s) for s in self.urls)
        total += sum(len(s) for s in self.titles)
        return total

    # -- filtering -------------------------------------------------------------------------------------------
    def _mask(self, domains, concepts):
        wanted = [self.domain_names.index(d) for d in {str(x).lower() for x in domains or []} if d in self.domain_names]
        if not wanted:
            return np.zeros(self.n, dtype=bool)
        mask = np.isin(self.domain_codes, np.array(wanted, dtype=np.int32))
        query = np.zeros(self.concept_words, dtype=np.uint64)
        known = 0
        for c in {str(x).lower() for x in concepts or []}:
            if c in self.concept_names:
                b = self.concept_names.index(c)
                query[b // 64] |= np.uint64(1) << np.uint64(b % 64)
                known += 1
        if not known:
            return np.zeros(self.n, dtype=bool)
        return mask & ((self.concept_bits & query) != 0).any(axis=1)

    # -- the two halves ----------------------------------------------------------------------------------------
    @staticmethod
    def _top(values, count, largest):
        """Positions of the count best values, best first. Equal values are taken in position order, so the answer
        never depends on how the partition happened to fall."""
        n = len(values)
        if n == 0:
            return np.zeros(0, dtype=np.int64)
        key = -values if largest else values
        if n <= count:
            chosen = np.arange(n)
        else:
            edge = np.partition(key, count - 1)[count - 1]
            better = np.flatnonzero(key < edge)
            tied = np.flatnonzero(key == edge)[: count - len(better)]
            chosen = np.concatenate([better, tied])
        return chosen[np.lexsort((chosen, key[chosen]))]

    def keyword_scores(self, terms):
        """BM25 score of every chunk for these word hashes (a chunk matches when it has any of them)."""
        scores = np.zeros(self.n, dtype=np.float32)
        if len(terms) and len(self.vocab):
            pos = np.searchsorted(self.vocab, terms)
            inside = pos < len(self.vocab)
            pos, wanted = pos[inside], terms[inside]
            for p in pos[self.vocab[pos] == wanted]:
                a, z = self.post_ptr[p], self.post_ptr[p + 1]
                scores[self.post_docs[a:z]] += self.post_weights[a:z]
        return scores

    def search(self, terms, qcode, domains, concepts, candidates=50, limit=5):
        """(hits, matching) where hits is [(row, score), ...] best first and matching counts the chunks that passed
        the filters. qcode may be None, which leaves out the vector half (keyword only)."""
        if not self.n:
            return [], 0
        rows = np.flatnonzero(self._mask(domains, concepts))
        if not len(rows):
            return [], 0
        fused = {}
        scores = self.keyword_scores(terms)[rows]
        hit = np.flatnonzero(scores > 0)
        for rank, pos in enumerate(self._top(scores[hit], candidates, True)):
            row = int(rows[hit[pos]])
            fused[row] = fused.get(row, 0.0) + 1.0 / (RRF_K + rank + 1)
        if qcode is not None and self.code_bytes:
            query = _words([qcode[: self.code_bytes].ljust(self.code_bytes, b"\0")], self.code_bytes)[0]
            distance = _popcount_rows(self.codes[rows] ^ query)
            for rank, pos in enumerate(self._top(distance, candidates, False)):
                row = int(rows[pos])
                fused[row] = fused.get(row, 0.0) + 1.0 / (RRF_K + rank + 1)
        ordered = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
        return ordered, int(len(rows))

    # -- reading pages -----------------------------------------------------------------------------------------
    def row_dict(self, i):
        return {"id": self.ids[i], "title": self.titles[i], "url": self.urls[i],
                "domain": self.domain_names[self.domain_codes[i]], "concepts": list(self.concepts[i])}


# ---------------------------------------------------------------------------------------------------------------
# Table Storage
# ---------------------------------------------------------------------------------------------------------------

class LocalIndexStore:
    """Reads and writes the chunk table and the version row. Takes table clients so tests can pass fakes."""

    def __init__(self, chunks=None, meta=None, connection_string=None, table_name=None):
        if chunks is None:
            from azure.core.exceptions import ResourceExistsError
            from azure.data.tables import TableServiceClient
            service = TableServiceClient.from_connection_string(connection_string or settings.local_index_connection)
            name = table_name or settings.local_index_table
            chunks = service.get_table_client(name)
            meta = service.get_table_client(name + "meta")
            for table in (chunks, meta):
                try:
                    table.create_table()
                except ResourceExistsError:
                    pass  # any other failure (credentials, network) must show, not surface later as a missing table
        self.chunks = chunks
        self.meta = meta

    # -- writing ---------------------------------------------------------------------------------------------
    def upsert_entities(self, entities, workers=4):
        """Write rows, one transaction per page (a transaction holds up to 100 rows of one partition)."""
        by_page = {}
        for e in entities:
            by_page.setdefault(e["PartitionKey"], []).append(e)
        jobs = []
        for page_rows in by_page.values():
            for start in range(0, len(page_rows), 100):
                jobs.append(page_rows[start:start + 100])
        if not jobs:
            return 0
        if workers > 1 and len(jobs) > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
                list(pool.map(self._write_group, jobs))
        else:
            for job in jobs:
                self._write_group(job)
        return len(entities)

    @staticmethod
    def _replace():
        try:
            from azure.data.tables import UpdateMode
            return UpdateMode.REPLACE
        except Exception:
            return "replace"

    def _write_group(self, group):
        mode = self._replace()
        try:
            self.chunks.submit_transaction([("upsert", e, {"mode": mode}) for e in group])
        except Exception:
            log.debug("Batch write failed; writing the rows one at a time", exc_info=True)
            for e in group:
                self.chunks.upsert_entity(e, mode=mode)

    def delete_ids(self, ids):
        """Delete chunks by id. An id that is not there is not an error."""
        by_page = {}
        for chunk_id in ids:
            try:
                page_id, number = split_chunk_id(chunk_id)
            except ValueError:
                continue
            by_page.setdefault(page_id, []).append(str(number).zfill(6))
        removed = 0
        for page_id, row_keys in by_page.items():
            for start in range(0, len(row_keys), 100):
                group = row_keys[start:start + 100]
                try:
                    self.chunks.submit_transaction([("delete", {"PartitionKey": page_id, "RowKey": rk}) for rk in group])
                except Exception:
                    for rk in group:
                        try:
                            self.chunks.delete_entity(page_id, rk)
                        except Exception:
                            pass  # not there
                removed += len(group)
        return removed

    def page_ids(self, page_id):
        """The ids of every stored chunk of one page."""
        rows = self.chunks.query_entities(f"PartitionKey eq '{page_id}'", select=["id", "RowKey"])
        return [str(r.get("id") or f"{page_id}-{int(r['RowKey'])}") for r in rows]

    def existing_ids(self, ids):
        """Which of these chunk ids are in the table (one keys-only query per page)."""
        by_page = {}
        for chunk_id in ids:
            try:
                page_id, number = split_chunk_id(chunk_id)
            except ValueError:
                continue
            by_page.setdefault(page_id, set()).add(str(number).zfill(6))
        found = set()
        for page_id, wanted in by_page.items():
            rows = self.chunks.query_entities(f"PartitionKey eq '{page_id}'", select=["RowKey"])
            for row in rows:
                if row["RowKey"] in wanted:
                    found.add(f"{page_id}-{int(row['RowKey'])}")
        return found

    def count(self, domain=None):
        """How many chunks the table holds (for one domain, when given). Reads keys and domains only, no text."""
        want = str(domain).strip().lower() if domain else None
        total = 0
        for entity in self.chunks.list_entities(select=["domain"], results_per_page=1000):
            if want is None or str(entity.get("domain") or "").lower() == want:
                total += 1
        return total

    def get_text(self, chunk_id):
        page_id, number = split_chunk_id(chunk_id)
        try:
            entity = self.chunks.get_entity(page_id, str(number).zfill(6), select=["text"])
        except Exception:
            return None
        return entity.get("text")

    # -- the version row -----------------------------------------------------------------------------------------
    def bump_version(self):
        token = uuid.uuid4().hex
        self.meta.upsert_entity({"PartitionKey": META_PARTITION, "RowKey": META_ROW, "token": token,
                                 "updated": datetime.now(timezone.utc).isoformat()}, mode=self._replace())
        return token

    def get_version(self):
        try:
            return self.meta.get_entity(META_PARTITION, META_ROW).get("token")
        except Exception:
            return None

    # -- reading everything ----------------------------------------------------------------------------------
    def scan(self, keep_text=True):
        select = ["id", "title", "url", "domain", "concepts", "vec", "tid", "tcnt"] + (["text"] if keep_text else [])
        for entity in self.chunks.list_entities(select=select, results_per_page=1000):
            yield entity


# ---------------------------------------------------------------------------------------------------------------
# Loading, refreshing and counting
# ---------------------------------------------------------------------------------------------------------------

def release_memory():
    """After a rebuild, hand freed memory back to the operating system (Python keeps it otherwise)."""
    gc.collect()
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass  # not Linux, or no glibc: nothing to do


def process_memory_mb():
    """The process's resident memory, from /proc (Linux), or None."""
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024)
    except Exception:
        return None
    return None


class LocalIndexManager:
    """Owns the index this instance searches. The first search waits for the load; later ones never do."""

    FAILURE_COOLDOWN_SECONDS = 15.0

    def __init__(self, store=None):
        self._store = store
        self._index = None
        self._version = None
        self._loaded_at = float("-inf")
        self._last_check = float("-inf")
        self._failed_at = 0.0
        self._load_lock = threading.Lock()
        self._checking = threading.Lock()
        self.stats = {"loads": 0, "load_seconds": None, "build_seconds": None, "last_error": None, "searches": 0,
                      "fallbacks": 0, "embed_ms_total": 0.0, "search_ms_total": 0.0, "keyword_only": 0,
                      "shadow": {"compared": 0, "overlap_sum": 0.0, "top1_same": 0, "failed": 0}}
        self._stats_lock = threading.Lock()

    @property
    def enabled(self):
        return self._store is not None or bool(settings.local_index_connection)

    def store(self):
        if self._store is None:
            self._store = LocalIndexStore()
        return self._store

    # -- using the index -----------------------------------------------------------------------------------------
    def current(self):
        index = self._index
        if index is None:
            self._load_first()
            index = self._index
            if index is None:
                raise LocalIndexUnavailable(self.stats["last_error"] or "the local index is not loaded")
        else:
            self._maybe_refresh()
        return index

    def _load_first(self):
        with self._load_lock:
            if self._index is not None:
                return
            if time.monotonic() - self._failed_at < self.FAILURE_COOLDOWN_SECONDS and self._failed_at:
                raise LocalIndexUnavailable(self.stats["last_error"] or "the local index could not be loaded")
            try:
                self._reload_locked()
            except Exception as exc:
                self._failed_at = time.monotonic()
                self.stats["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
                log.warning("Local index could not be loaded: %s", self.stats["last_error"])
                raise LocalIndexUnavailable(self.stats["last_error"]) from exc

    def _reload_locked(self):
        began = time.perf_counter()
        store = self.store()
        version = store.get_version()  # read first: a write during the scan then shows up as a newer token
        keep_text = settings.local_index_text_in_memory
        rows = (row_from_entity(e) for e in store.scan(keep_text))
        read_done = {}

        def counting():
            for r in rows:
                yield r
            read_done["t"] = time.perf_counter()

        index = LocalIndex(counting(), keep_text=keep_text)
        if not index.n:
            raise LocalIndexUnavailable("the local index table is empty; run the backfill first")
        now = time.perf_counter()
        self._index, self._version, self._loaded_at = index, version, time.monotonic()
        del rows
        release_memory()
        self.stats["loads"] += 1
        self.stats["load_seconds"] = round(now - began, 2)
        self.stats["build_seconds"] = round(now - read_done.get("t", began), 2)
        self.stats["last_error"] = None
        log.info("Local index loaded: %d chunks, %d pages, %.1f MB, %.1f s", index.n, len(index.pages),
                 index.memory_bytes() / 1e6, now - began)

    def reload(self):
        """Rebuild now, on this instance, and wait for it. Used by the reload route and the preload."""
        with self._load_lock:
            self._reload_locked()

    def _maybe_refresh(self):
        now = time.monotonic()
        if now - self._last_check < settings.local_index_poll_seconds or not self._checking.acquire(blocking=False):
            return
        self._last_check = now

        def work():
            try:
                token = self.store().get_version()
                due = now - self._loaded_at >= settings.local_index_min_reload_seconds
                if token and token != self._version and due and self._load_lock.acquire(blocking=False):
                    try:
                        self._reload_locked()
                    finally:
                        self._load_lock.release()
            except Exception as exc:
                self.stats["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
                log.warning("Local index refresh failed; the earlier index stays in use: %s", self.stats["last_error"])
            finally:
                self._checking.release()

        threading.Thread(target=work, name="local-index-refresh", daemon=True).start()

    # -- numbers for /health -----------------------------------------------------------------------------------
    def record_search(self, embed_ms, search_ms, keyword_only=False):
        with self._stats_lock:
            self.stats["searches"] += 1
            self.stats["embed_ms_total"] += embed_ms
            self.stats["search_ms_total"] += search_ms
            if keyword_only:
                self.stats["keyword_only"] += 1

    def record_fallback(self):
        with self._stats_lock:
            self.stats["fallbacks"] += 1

    def record_shadow(self, overlap, top1_same):
        with self._stats_lock:
            s = self.stats["shadow"]
            s["compared"] += 1
            s["overlap_sum"] += overlap
            s["top1_same"] += 1 if top1_same else 0

    def record_shadow_failure(self):
        with self._stats_lock:
            self.stats["shadow"]["failed"] += 1

    def describe(self):
        index = self._index
        s = self.stats
        n = max(1, s["searches"])
        shadow = s["shadow"]
        compared = shadow["compared"]
        return {
            "backend": settings.search_backend,
            "enabled": self.enabled,
            "loaded": index is not None,
            "chunks": index.n if index else 0,
            "pages": len(index.pages) if index else 0,
            "skipped_rows": index.skipped if index else 0,
            "memory_mb": round(index.memory_bytes() / 1e6, 1) if index else 0,
            "process_memory_mb": process_memory_mb(),
            "text_in_memory": settings.local_index_text_in_memory,
            "version": self._version,
            "age_seconds": round(time.monotonic() - self._loaded_at) if index else None,
            "loads": s["loads"],
            "load_seconds": s["load_seconds"],
            "build_seconds": s["build_seconds"],
            "searches": s["searches"],
            "avg_embed_ms": round(s["embed_ms_total"] / n, 1) if s["searches"] else None,
            "avg_search_ms": round(s["search_ms_total"] / n, 2) if s["searches"] else None,
            "keyword_only_searches": s["keyword_only"],
            "fallbacks_to_ai_search": s["fallbacks"],
            "last_error": s["last_error"],
            "shadow": {
                "compared": compared,
                "failed": shadow["failed"],
                "average_overlap": round(shadow["overlap_sum"] / compared, 3) if compared else None,
                "top_result_agrees": round(shadow["top1_same"] / compared, 3) if compared else None,
            },
        }
