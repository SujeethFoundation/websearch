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
from collections import deque
from datetime import datetime, timezone
from functools import lru_cache

import numpy as np

from services.config import settings

log = logging.getLogger("sujeeth-web-search.local-index")

FORMAT_VERSION = 1
SHADOW_DIFFS_KEPT = 25
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
# Words that hold a question together but say nothing about its topic. Two of the question's words are only looked for as
# a phrase when both are outside this list, so "how do" and "do I" can never earn the phrase bonus on a page that merely
# happens to be written as questions and answers.
FUNCTION_WORDS = STOPWORDS | frozenset(
    "about am any been can could did do does from get had has have how i just may me might must my our should so than "
    "those us we what when where which who why would you your describe explain please show tell".split()
)
PHRASE_SPAN = 4  # two of the question's content words make a phrase when at most this many words apart in the question
_TOKEN = re.compile(r"[^\W_]+")  # runs of letters and digits in any script


class LocalIndexUnavailable(RuntimeError):
    """The in-memory index is not loaded, is empty, or could not be loaded."""


# ---------------------------------------------------------------------------------------------------------------
# Text and vectors
# ---------------------------------------------------------------------------------------------------------------

def tokens(text, drop_stopwords=True):
    """Lower-cased words, without stop words unless drop_stopwords is False."""
    words = _TOKEN.findall(str(text or "").lower())
    return [t for t in words if t not in STOPWORDS] if drop_stopwords else words


@lru_cache(maxsize=500000)
def term_hash(token):
    """A stable 64-bit number for a word (Python's own hash() changes from run to run)."""
    return int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "little")


def query_terms(text):
    """The distinct words of a question, as an array of hashes. Common words are searched for like any other (as the
    Azure index does) unless LOCAL_INDEX_STOPWORDS=english."""
    seen = sorted({term_hash(t) for t in tokens(text, settings.local_index_stopwords == "english")})
    return np.array(seen, dtype=np.uint64)


def analyze(title, content, title_weight=3):
    """(hashes, counts) for one chunk as raw bytes: the word hashes in ascending order and how often each occurs.

    Words in the title count title_weight times, like the title boost in the wiki search. Counts stop at 255.
    Every word is kept, common ones too, as the Azure index (default Standard Lucene analyzer) keeps them: whether a
    question searches for them is decided when it is asked (LOCAL_INDEX_STOPWORDS), so no rewrite is needed to change it."""
    counts = {}
    for token in tokens(content, False):
        h = term_hash(token)
        counts[h] = counts.get(h, 0) + 1
    for token in tokens(title, False):
        h = term_hash(token)
        counts[h] = counts.get(h, 0) + max(1, int(title_weight))
    ordered = sorted(counts)
    hashes = np.array(ordered, dtype="<u8")
    values = np.array([min(255, counts[h]) for h in ordered], dtype=np.uint8)
    return hashes.tobytes(), values.tobytes()


def quantize_int8(vector):
    """The vector at 8 bits a dimension: scaled so its largest value is 127. Cosine similarity does not depend on a
    vector's overall scale, so the scale is not kept."""
    v = np.asarray(vector, dtype=np.float32)
    peak = float(np.abs(v).max()) if v.size else 0.0
    if peak == 0.0:
        return np.zeros(v.shape, dtype=np.int8).tobytes()
    return np.round(v * (127.0 / peak)).astype(np.int8).tobytes()


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


def page_key(url):
    """One key for a page however it was reached: no scheme, no leading www., no fragment, no trailing slash, host in
    lower case. https://www.sujeeth.org/circle/ and http://sujeeth.org/circle#top are the same page."""
    text = str(url or "").strip().split("#", 1)[0]
    text = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", text)
    host, slash, rest = text.partition("/")
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return (host + slash + rest).rstrip("/")


def split_chunk_id(chunk_id):
    """('<page id>', number) from '<page id>-<number>'."""
    page_id, _, number = str(chunk_id).rpartition("-")
    if not page_id or not number.isdigit():
        raise ValueError(f"chunk id must look like <page>-<number>: {chunk_id!r}")
    return page_id, int(number)


def chunk_number(chunk_id):
    """The number at the end of a chunk id, or 0 when it has none."""
    try:
        return split_chunk_id(chunk_id)[1]
    except ValueError:
        return 0


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
        "v8": quantize_int8(vector),
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
        "v8": raw(entity.get("v8")),
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
        # Which page each chunk belongs to (www. and non-www. copies are one page) and its number on that page, so an
        # answer can hold a page a limited number of times and never the same chunk of it twice.
        keys = {}
        self.page_codes = np.fromiter((keys.setdefault(page_key(u), len(keys)) for u in self.urls), dtype=np.int32, count=n)
        self.chunk_numbers = np.fromiter((chunk_number(i) for i in self.ids), dtype=np.int32, count=n)
        self.distinct_pages = len(keys)
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

        # Each vector at 8 bits a dimension, to re-rank the shortlist the codes pick. All or nothing: a mix of rows with
        # and without vectors would rank some chunks on a different scale from the rest.
        self.vec8, self.norm8, self.vec_dims = None, None, 0
        dims = len(usable[0].get("v8") or b"") if n else 0
        self.rows_without_vectors = n - sum(1 for r in usable if dims and len(r.get("v8") or b"") == dims)
        if n and dims and not self.rows_without_vectors and settings.local_rescore:
            vec8 = np.empty((n, dims), dtype=np.int8)
            for i, r in enumerate(usable):
                vec8[i] = np.frombuffer(r["v8"], dtype=np.int8)
            norm8 = np.empty(n, dtype=np.float32)
            for start in range(0, n, 4096):
                block = vec8[start:start + 4096].astype(np.float32)
                norm8[start:start + 4096] = np.sqrt((block * block).sum(axis=1))
            norm8[norm8 == 0] = 1.0
            self.vec8, self.norm8, self.vec_dims = vec8, norm8, dims

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

        # Which rows pass a given set of domains and concepts. The same sets come back all the time (the ontology picks
        # them), and a snapshot never changes, so each answer is kept for the life of the snapshot. Bounded.
        self._rows_cache = {}

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
        total += (self.vec8.nbytes + self.norm8.nbytes) if self.vec8 is not None else 0
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

    ROWS_CACHE_MAX = 64

    def _rows(self, domains, concepts):
        """The positions of the chunks that pass the filters, as int32 (kept per snapshot)."""
        key = (frozenset(str(d).lower() for d in domains or []), frozenset(str(c).lower() for c in concepts or []))
        rows = self._rows_cache.get(key)
        if rows is None:
            rows = np.flatnonzero(self._mask(domains, concepts)).astype(np.int32)
            if len(self._rows_cache) < self.ROWS_CACHE_MAX:
                self._rows_cache[key] = rows
        return rows

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

    def vector_ranking(self, rows, qcode, qvec, count, pool=None):
        """The `count` best of `rows` for a question, best first, as row numbers.

        The 1-bit codes pick a shortlist of `pool` chunks (all of them when fewer pass the filters); the shortlist is
        then ranked by cosine similarity on the 8-bit vectors. Without a question vector, or without vectors in the
        index, the codes alone rank (Hamming distance, which has many ties and ranks close neighbours poorly)."""
        pool = pool or settings.local_rescore_pool
        query = _words([qcode[: self.code_bytes].ljust(self.code_bytes, b"\0")], self.code_bytes)[0]
        if qvec is not None and self.vec8 is not None and len(qvec) == self.vec_dims:
            if len(rows) > max(pool, count):
                shortlist = rows[self._top(_popcount_rows(self.codes[rows] ^ query), max(pool, count), False)]
            else:
                shortlist = rows
            q = np.frombuffer(qvec, dtype=np.int8).astype(np.float32)
            norm = float(np.sqrt((q * q).sum())) or 1.0
            similarity = (self.vec8[shortlist].astype(np.float32) @ q) / (self.norm8[shortlist] * norm)
            return [int(shortlist[i]) for i in self._top(similarity, count, True)]
        distance = _popcount_rows(self.codes[rows] ^ query)
        return [int(rows[pos]) for pos in self._top(distance, count, False)]

    def concept_coverage(self, rows, concept_weights):
        """For each of `rows`, the share (0 to 1) of the weighted concepts it is tagged with."""
        total = sum(w for w in concept_weights.values() if w > 0)
        cover = np.zeros(len(rows), dtype=np.float64)
        if total <= 0 or not len(rows):
            return cover
        bits = self.concept_bits[rows]
        for concept, weight in concept_weights.items():
            c = str(concept).lower()
            if weight > 0 and c in self.concept_names:
                b = self.concept_names.index(c)
                cover += weight * ((bits[:, b // 64] >> np.uint64(b % 64)) & np.uint64(1)).astype(np.float64)
        return cover / total

    def term_idf(self, word):
        """(idf, chunks that have the word) for one lower-case word."""
        df = 0
        if len(self.vocab):
            h = np.uint64(term_hash(word))
            p = int(np.searchsorted(self.vocab, h))
            if p < len(self.vocab) and self.vocab[p] == h:
                df = int(self.post_ptr[p + 1] - self.post_ptr[p])
        return float(np.log(1.0 + (self.n - df + 0.5) / (df + 0.5))), df

    def question_phrases(self, question):
        """[(phrase, weight), ...]: for each two content words that follow each other in the question (function words
        between them allowed, at most PHRASE_SPAN words apart), the question's own wording from the first to the second:
        "reach out to a professional" gives "reach out" and "out to a professional". Function words are never an end of
        a phrase, so "how do" and "do i" are not looked for. A phrase weighs what the commoner of its two content words
        weighs. Phrases with a word no chunk has are left out: nothing can hold them."""
        words = tokens(question, False)
        content = [i for i, w in enumerate(words) if w not in FUNCTION_WORDS and (len(w) > 1 or w.isdigit())]
        seen, out = set(), []
        for i, j in zip(content, content[1:]):
            phrase = " ".join(words[i:j + 1])
            if words[i] == words[j] or j - i > PHRASE_SPAN or phrase in seen:
                continue
            seen.add(phrase)
            (ia, da), (ib, db) = self.term_idf(words[i]), self.term_idf(words[j])
            if da and db:
                out.append((phrase, min(ia, ib)))
        return out

    def phrase_share(self, row, phrases):
        """The weighted share (0 to 1) of the question's word pairs that this chunk's title or text holds side by side."""
        total = sum(w for _, w in phrases)
        if total <= 0 or self.texts is None:
            return 0.0
        hay = (self.titles[row] + " \n " + self.texts[row]).lower()
        found = sum(w for phrase, w in phrases if phrase in hay or phrase.replace(" ", "-") in hay)
        return found / total

    def search(self, terms, qcode, domains, concepts, candidates=50, limit=5, qvec=None, text_candidates=None,
               concept_weights=None, concept_boost=0.0, max_per_page=0, question=None, phrase_boost=0.0, phrase_pool=100,
               unique_pages=False):
        """(hits, matching) where hits is [(row, score), ...] best first and matching counts the chunks that passed
        the filters. qcode may be None, which leaves out the vector half (keyword only). qvec is the question's 8-bit
        vector; with it (and vectors in the index) the vector half is re-ranked by cosine similarity. `candidates` is how
        many vector results enter the rank fusion, `text_candidates` (default the same) how many keyword results do: Azure
        AI Search feeds up to 1,000 keyword results and the nearest k vectors, which the caller can mirror.

        Two things go beyond that fusion, both off by default here. concept_weights ({concept: weight}, the concepts the
        question itself named) with concept_boost adds to each candidate concept_boost * (share of those concepts the
        chunk is tagged with) on the scale of a first place in one list. max_per_page prefers at most that many chunks of
        one page (further chunks of a page are used only when too few pages matched to fill the answer), and never
        returns the same chunk of a page twice (the www. and non-www. copies of a page are one page). question (its text)
        with phrase_boost adds, for the best phrase_pool candidates, phrase_boost * (share of the question's neighbouring
        word pairs the chunk holds side by side), on the same scale."""
        rows, ordered, _ = self._rank(terms, qcode, domains, concepts, candidates, qvec, text_candidates,
                                      concept_weights, concept_boost, question, phrase_boost, phrase_pool)
        return self._pick(ordered, limit, max_per_page, unique_pages), int(len(rows))

    def _rank(self, terms, qcode, domains, concepts, candidates, qvec, text_candidates, concept_weights, concept_boost,
              question=None, phrase_boost=0.0, phrase_pool=100):
        """(rows that pass the filters, [(row, score), ...] every candidate best first, parts) where parts holds each
        candidate's 0-based place in the keyword list and the vector list, its share of the question's concepts and
        its share of the question's word pairs."""
        parts = {"keyword": {}, "vector": {}, "share": {}, "phrase": {}, "phrases": []}
        if not self.n:
            return np.zeros(0, dtype=np.int64), [], parts
        rows = self._rows(domains, concepts)
        if not len(rows):
            return rows, [], parts
        fused = {}
        scores = self.keyword_scores(terms)[rows]
        hit = np.flatnonzero(scores > 0)
        for rank, pos in enumerate(self._top(scores[hit], text_candidates or candidates, True)):
            row = int(rows[hit[pos]])
            parts["keyword"][row] = rank
            fused[row] = fused.get(row, 0.0) + 1.0 / (RRF_K + rank + 1)
        if qcode is not None and self.code_bytes:
            for rank, row in enumerate(self.vector_ranking(rows, qcode, qvec, candidates)):
                parts["vector"][row] = rank
                fused[row] = fused.get(row, 0.0) + 1.0 / (RRF_K + rank + 1)
        if concept_weights and concept_boost > 0 and fused:
            cand = np.fromiter(fused.keys(), dtype=np.int64, count=len(fused))
            for row, share in zip(cand.tolist(), self.concept_coverage(cand, concept_weights).tolist()):
                if share:
                    parts["share"][row] = share
                    fused[row] += concept_boost * share / (RRF_K + 1)
        if question and phrase_boost > 0 and fused and self.texts is not None:
            phrases = self.question_phrases(question)
            parts["phrases"] = phrases
            if phrases:
                best = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))[:max(1, int(phrase_pool))]
                for row, _ in best:
                    share = self.phrase_share(row, phrases)
                    if share:
                        parts["phrase"][row] = share
                        fused[row] += phrase_boost * share / (RRF_K + 1)
        return rows, sorted(fused.items(), key=lambda kv: (-kv[1], kv[0])), parts

    def _pick(self, ordered, limit, max_per_page, unique=False):
        """The answer from the ranked candidates: the best `limit`, preferring at most max_per_page chunks of a page.
        With unique, each page appears once, as its best chunk, and the answer is shorter than `limit` when fewer pages
        match (the www. and bare copies of an address are one page)."""
        if unique:
            kept, seen = [], set()
            for row, score in ordered:
                page = int(self.page_codes[row])
                if page in seen:
                    continue
                seen.add(page)
                kept.append((row, score))
                if len(kept) >= limit:
                    break
            return kept
        if max_per_page <= 0:
            return ordered[:limit]
        kept, spare, per_page, seen = [], [], {}, set()
        for row, score in ordered:
            page = int(self.page_codes[row])
            chunk = (page, int(self.chunk_numbers[row]))
            if chunk in seen:
                continue                      # the same chunk of the same page (its www. copy): never twice
            seen.add(chunk)
            if per_page.get(page, 0) >= max_per_page:
                if len(spare) < limit:
                    spare.append((row, score))
                continue
            per_page[page] = per_page.get(page, 0) + 1
            kept.append((row, score))
            if len(kept) >= limit:
                break
        if len(kept) < limit:
            # Too few pages matched to fill the answer: give the remaining places to further chunks of the same pages,
            # so the limit only ever trades a repeat for another page, never for an empty place.
            kept = sorted(kept + spare[: limit - len(kept)], key=lambda kv: (-kv[1], kv[0]))
        return kept

    def _vector_closeness(self, rows, qcode, qvec, block=4096):
        """How close every one of `rows` is to the question (higher is closer): cosine on the 8-bit vectors when the
        index has them, else minus the Hamming distance of the codes. Worked out a block at a time to stay small."""
        if qvec is not None and self.vec8 is not None and len(qvec) == self.vec_dims:
            q = np.frombuffer(qvec, dtype=np.int8).astype(np.float32)
            norm = float(np.sqrt((q * q).sum())) or 1.0
            out = np.empty(len(rows), dtype=np.float32)
            for start in range(0, len(rows), block):
                part = rows[start:start + block]
                out[start:start + block] = (self.vec8[part].astype(np.float32) @ q) / (self.norm8[part] * norm)
            return out
        query = _words([qcode[: self.code_bytes].ljust(self.code_bytes, b"\0")], self.code_bytes)[0]
        return -_popcount_rows(self.codes[rows] ^ query).astype(np.float32)

    def explain(self, terms, qcode, domains, concepts, expect, candidates=50, limit=5, qvec=None, text_candidates=None,
                concept_weights=None, concept_boost=0.0, max_per_page=0, max_chunks=6, question=None, phrase_boost=0.0,
                phrase_pool=100, unique_pages=False):
        """Why a search ranks as it does, and where the page it should have found stands.

        expect is a list of pieces of URL. Returns the answer with each result's place in the keyword list and the vector
        list and its concept share, and for the expected page: how many of its chunks are in the index, how many pass
        the filters (with the tags and domain of some that do not), and for its best chunks their place among all
        candidates, in the keyword list and in the vector list, counted over every chunk that passes the filters, not
        only the ones that entered the fusion."""
        rows, ordered, parts = self._rank(terms, qcode, domains, concepts, candidates, qvec, text_candidates,
                                          concept_weights, concept_boost, question, phrase_boost, phrase_pool)
        by_chunk = {row: i for i, (row, _) in enumerate(ordered)}     # place among all candidate chunks
        if unique_pages:
            # Places among pages, as in the answer: only a page's best chunk has one, the page's other chunks sit behind it.
            position, page_place = {}, {}
            for row, _ in ordered:
                page = int(self.page_codes[row])
                if page not in page_place:
                    page_place[page] = len(page_place)
                    position[row] = page_place[page]
        else:
            position = dict(by_chunk)
            page_place = {}
        phrases = parts["phrases"] or (self.question_phrases(question) if question and self.texts is not None else [])
        score = dict(ordered)
        share_all = {}

        def describe(row, deep=None):
            row = int(row)
            if row not in share_all and concept_weights:
                share_all[row] = float(self.concept_coverage(np.array([row]), concept_weights)[0])
            out = {"chunk": self.ids[row], "title": self.titles[row], "url": self.urls[row],
                   "domain": self.domain_names[self.domain_codes[row]], "tags": list(self.concepts[row]),
                   "position": position[row] + 1 if row in position else None,
                   "page_position": (page_place[int(self.page_codes[row])] + 1 if int(self.page_codes[row]) in page_place else None)
                   if unique_pages else (position[row] + 1 if row in position else None),
                   "score": round(score[row], 6) if row in score else None,
                   "keyword_rank": parts["keyword"][row] + 1 if row in parts["keyword"] else None,
                   "vector_rank": parts["vector"][row] + 1 if row in parts["vector"] else None,
                   "concept_share": round(share_all.get(row, 0.0), 3),
                   # the share of the question's word pairs the chunk holds; "counted" says whether it earned the bonus
                   "phrase_share": round(self.phrase_share(row, phrases), 3) if phrases else 0.0,
                   "phrase_counted": row in parts["phrase"]}
            if deep:
                out.update(deep.get(row, {}))
            return out

        answer = [describe(row) for row, _ in self._pick(ordered, limit, max_per_page, unique_pages)]
        pieces = [str(e).lower() for e in (expect or []) if str(e).strip()]
        wanted = [i for i, url in enumerate(self.urls) if any(p in url.lower() for p in pieces)] if pieces else []
        passing = set(int(r) for r in rows.tolist())
        inside = [i for i in wanted if i in passing]
        outside = [i for i in wanted if i not in passing]
        deep = {}
        if inside and len(rows):
            # Places over every chunk that passes the filters, so a chunk that missed the fusion still shows how far off it is.
            where = {int(r): pos for pos, r in enumerate(rows.tolist())}
            kscores = self.keyword_scores(terms)[rows]
            korder = np.argsort(-kscores, kind="stable")
            kplace = np.empty(len(rows), dtype=np.int64)
            kplace[korder] = np.arange(len(rows))
            vplace = None
            if qcode is not None and self.code_bytes:
                closeness = self._vector_closeness(rows, qcode, qvec)
                vorder = np.argsort(-closeness, kind="stable")
                vplace = {int(rows[p]): i for i, p in enumerate(vorder.tolist())}
            for i in inside:
                pos = where[i]
                deep[i] = {"keyword_rank_all": int(kplace[pos]) + 1 if kscores[pos] > 0 else None,
                           "vector_rank_all": vplace[i] + 1 if vplace is not None and i in vplace else None}
        inside.sort(key=lambda i: (by_chunk.get(i, 10 ** 9), deep.get(i, {}).get("vector_rank_all") or 10 ** 9))
        chunks_inside, pages_in_index = len(inside), len({int(self.page_codes[i]) for i in wanted})
        pages_inside = len({int(self.page_codes[i]) for i in inside})
        others = {}
        if unique_pages:
            # Each expected page once, as its best chunk, with how many more chunks of it there are.
            best_of, count_of = {}, {}
            for i in inside:
                page = int(self.page_codes[i])
                count_of[page] = count_of.get(page, 0) + 1
                best_of.setdefault(page, i)
            inside = list(best_of.values())
            others = {best_of[page]: count_of[page] - 1 for page in best_of}
        want_domains = {str(d).lower() for d in domains or []}
        want_concepts = {str(c).lower() for c in concepts or []}

        def why_not(row):
            domain = self.domain_names[self.domain_codes[row]]
            tags = list(self.concepts[row])
            reasons = []
            if domain not in want_domains:
                reasons.append("its domain is not one the ontology chose for this question")
            if not want_concepts & {t.lower() for t in tags}:
                reasons.append("it carries none of the question's concepts")
            return {"chunk": self.ids[row], "url": self.urls[row], "domain": domain, "tags": tags, "why": "; ".join(reasons)}

        return {
            "passing_filters": int(len(rows)), "candidates": len(ordered), "answer": answer,
            "phrases": [{"phrase": p, "weight": round(w, 2)} for p, w in sorted(phrases, key=lambda pw: -pw[1])],
            "expected": {"pieces": pieces, "chunks_in_index": len(wanted), "chunks_passing_filters": chunks_inside,
                         "pages_in_index": pages_in_index, "pages_passing_filters": pages_inside,
                         "best": [{**describe(i, deep), "other_chunks": others.get(i, 0)} for i in inside[:max_chunks]],
                         "filtered_out": [why_not(i) for i in outside[:max_chunks]]},
        }

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

    def counts_by_domain(self):
        """{domain: chunks} for everything the table holds, in one keys-and-domains scan (no text)."""
        counts = {}
        for entity in self.chunks.list_entities(select=["domain"], results_per_page=1000):
            domain = str(entity.get("domain") or "").lower()
            counts[domain] = counts.get(domain, 0) + 1
        return counts

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
        select = (["id", "title", "url", "domain", "concepts", "vec", "tid", "tcnt"]
                  + (["v8"] if settings.local_rescore else []) + (["text"] if keep_text else []))
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
        self.on_swap = None  # called after a rebuilt index replaces the old one (the search service clears its answers)
        self.stats = {"loads": 0, "load_seconds": None, "build_seconds": None, "last_error": None, "searches": 0,
                      "fallbacks": 0, "embed_ms_total": 0.0, "search_ms_total": 0.0, "keyword_only": 0, "rescored": 0,
                      "shadow": {"compared": 0, "overlap_sum": 0.0, "top1_same": 0, "failed": 0,
                                 "page_overlap_sum": 0.0, "top1_page_same": 0}}
        self._shadow_diffs = deque(maxlen=SHADOW_DIFFS_KEPT)
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
        if self.on_swap:
            try:
                self.on_swap()
            except Exception:
                log.warning("on_swap callback failed", exc_info=True)
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
    def record_search(self, embed_ms, search_ms, keyword_only=False, rescored=False):
        with self._stats_lock:
            self.stats["searches"] += 1
            if rescored:
                self.stats["rescored"] += 1
            self.stats["embed_ms_total"] += embed_ms
            self.stats["search_ms_total"] += search_ms
            if keyword_only:
                self.stats["keyword_only"] += 1

    def record_fallback(self):
        with self._stats_lock:
            self.stats["fallbacks"] += 1

    def record_shadow(self, overlap, top1_same, page_overlap=None, top1_page_same=None, example=None):
        """One shadow comparison. overlap and top1_same compare chunk ids; page_overlap and top1_page_same compare
        pages (a different chunk of the same page is the same answer to a visitor). example, kept when the top
        pages differ, is what each engine returned, so the difference can be looked at."""
        with self._stats_lock:
            s = self.stats["shadow"]
            s["compared"] += 1
            s["overlap_sum"] += overlap
            s["top1_same"] += 1 if top1_same else 0
            s["page_overlap_sum"] += overlap if page_overlap is None else page_overlap
            s["top1_page_same"] += 1 if (top1_same if top1_page_same is None else top1_page_same) else 0
            if example is not None:
                self._shadow_diffs.append(example)

    def shadow_diffs(self, limit=10):
        """The latest comparisons whose top pages differed, newest first."""
        with self._stats_lock:
            return list(self._shadow_diffs)[::-1][:max(1, int(limit))]

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
            "writes": {"local_index_write": settings.local_index_write, "ai_search_writes": settings.ai_search_writes},
            "version": self._version,
            "age_seconds": round(time.monotonic() - self._loaded_at) if index else None,
            "loads": s["loads"],
            "load_seconds": s["load_seconds"],
            "build_seconds": s["build_seconds"],
            "searches": s["searches"],
            "avg_embed_ms": round(s["embed_ms_total"] / n, 1) if s["searches"] else None,
            "avg_search_ms": round(s["search_ms_total"] / n, 2) if s["searches"] else None,
            "keyword_only_searches": s["keyword_only"],
            "rescoring": {
                "enabled": settings.local_rescore,
                "active": bool(index is not None and index.vec8 is not None),
                "rows_without_vectors": index.rows_without_vectors if index else None,
                "pool": settings.local_rescore_pool,
                "rescored_searches": s["rescored"],
            },
            "fallbacks_to_ai_search": s["fallbacks"],
            "last_error": s["last_error"],
            "shadow": {
                "compared": compared,
                "failed": shadow["failed"],
                "average_overlap": round(shadow["overlap_sum"] / compared, 3) if compared else None,
                "top_result_agrees": round(shadow["top1_same"] / compared, 3) if compared else None,
                "average_page_overlap": round(shadow["page_overlap_sum"] / compared, 3) if compared else None,
                "top_page_agrees": round(shadow["top1_page_same"] / compared, 3) if compared else None,
            },
        }
