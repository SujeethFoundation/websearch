"""Build the in-memory index from the table in two passes, so the build never holds the whole table in memory.

LocalIndex(rows) first collects every row (text, vectors, word hashes) and then builds arrays from all of them at
once. That peaks several hundred MB above the finished index, too much for a 512 MB instance. This builds the same index
by reading the table twice:

  pass 1  only the word hashes, counts and the small columns. The keyword postings are built from them block by block, and
          each block's hashes are freed as soon as its postings are filled in.
  pass 2  the other columns (title, url, vectors, text), streamed straight into arrays of the final size. No row is kept.

Both passes read the table in the same order. A row inserted between the passes is skipped (the next reload has it, the
table's version has changed); a row deleted or changed beyond use makes the build start again (Inconsistent).
The result answers searches exactly like LocalIndex(rows) does (tests/test_lean_index.py compares them).
"""
import logging
from array import array

import numpy as np

from services.config import settings

log = logging.getLogger("sujeeth-web-search.lean-index")

BLOCK_ROWS = 2048   # rows whose postings are worked out together (about 55 bytes of temporary memory per posting)
ATTEMPTS = 3


class Inconsistent(RuntimeError):
    """The table changed between the two passes in a way the build cannot absorb."""


def _raw(value):
    value = getattr(value, "value", value)
    return bytes(value) if value is not None else b""


def _row_id(entity):
    return str(entity.get("id") or f"{entity['PartitionKey']}-{int(entity['RowKey'])}")


def _pack_block(hashes, counts, lengths):
    return (np.concatenate(hashes), np.concatenate(counts), np.array(lengths, dtype=np.int32))


# ---------------------------------------------------------------------------------------------------------------
# Pass 1: keyword postings
# ---------------------------------------------------------------------------------------------------------------

def _read_words(store, skip, split):
    """(keys, blocks, doc_len, domains, concepts, width, skipped): the usable rows' ids and their words."""
    keys, domains, concepts = [], set(), set()
    blocks, doc_len = [], array("d")
    hashes, counts, lengths = [], [], []
    width, skipped = 0, 0
    select = ["id", "domain", "concepts", "vec", "tid", "tcnt"]
    for entity in store.scan_columns(select, False, skip, split):
        vec, tid, tcnt = _raw(entity.get("vec")), _raw(entity.get("tid")), _raw(entity.get("tcnt"))
        if not vec or len(tid) % 8 or len(tid) // 8 != len(tcnt):
            skipped += 1
            continue
        width = width or len(vec)
        if len(vec) != width:
            skipped += 1
            continue
        keys.append(_row_id(entity))
        domains.add(str(entity.get("domain") or "").lower())
        for name in str(entity.get("concepts") or "").split(","):
            if name:
                concepts.add(name.lower())
        words, tf = np.frombuffer(tid, dtype="<u8"), np.frombuffer(tcnt, dtype=np.uint8)
        hashes.append(words)
        counts.append(tf)
        lengths.append(len(words))
        doc_len.append(float(tf.sum()))
        if len(lengths) >= BLOCK_ROWS:
            blocks.append(_pack_block(hashes, counts, lengths))
            hashes, counts, lengths = [], [], []
    if lengths:
        blocks.append(_pack_block(hashes, counts, lengths))
    return keys, blocks, np.frombuffer(doc_len, dtype=np.float64) if len(doc_len) else np.zeros(0), domains, concepts, width, skipped


def _build_postings(blocks, doc_len, n):
    """(vocab, post_ptr, post_docs, post_weights), the same arrays LocalIndex builds, one block at a time.
    A block's hashes are dropped once its postings are in place."""
    from services.local_index import BM25_B, BM25_K1, POSTING_DOC_BYTES, pack_docs

    vocab = np.zeros(0, dtype=np.uint64)
    for hashes, _, _ in blocks:
        vocab = np.union1d(vocab, np.unique(hashes))
    size = len(vocab)
    df = np.zeros(size, dtype=np.int64)
    for hashes, _, _ in blocks:
        df += np.bincount(np.searchsorted(vocab, hashes), minlength=size)
    idf = np.log(1.0 + (n - df + 0.5) / (df + 0.5)).astype(np.float32)
    avg_len = float(doc_len.mean()) or 1.0
    doc_norm = (BM25_K1 * (1.0 - BM25_B + BM25_B * doc_len / avg_len)).astype(np.float32)

    keep_term = None
    max_df = settings.local_posting_max_df
    if max_df > 0:
        keep_term = df <= max(1, int(max_df * n))
        df = np.where(keep_term, df, 0)
    post_ptr = np.concatenate([[0], np.cumsum(df)]).astype(np.int64)
    total = int(post_ptr[-1])
    post_docs = np.empty((total, POSTING_DOC_BYTES), dtype=np.uint8)
    post_weights = np.empty(total, dtype=np.dtype(settings.local_posting_weight_dtype))
    fill = post_ptr[:-1].copy()

    first_doc = 0
    for number in range(len(blocks)):
        hashes, counts, lengths = blocks[number]
        blocks[number] = None
        cols = np.searchsorted(vocab, hashes)
        docs = np.repeat(np.arange(first_doc, first_doc + len(lengths), dtype=np.int32), lengths)
        first_doc += len(lengths)
        tf = counts.astype(np.float32)
        weights = tf * np.float32(BM25_K1 + 1.0)
        tf += doc_norm[docs]
        weights /= tf
        weights *= idf[cols]
        del hashes, counts, tf
        if keep_term is not None:
            chosen = keep_term[cols]
            cols, docs, weights = cols[chosen], docs[chosen], weights[chosen]
        if not len(cols):
            continue
        order = np.argsort(cols, kind="stable")
        cols = cols[order]
        starts = np.flatnonzero(np.concatenate(([True], cols[1:] != cols[:-1])))
        sizes = np.diff(np.concatenate((starts, [len(cols)])))
        place = fill[cols] + (np.arange(len(cols)) - np.repeat(starts, sizes))
        post_docs[place] = pack_docs(docs[order])
        post_weights[place] = weights[order]
        fill[cols[starts]] += sizes
    return vocab, post_ptr, post_docs, post_weights


# ---------------------------------------------------------------------------------------------------------------
# Pass 2: everything else, streamed into arrays of the final size
# ---------------------------------------------------------------------------------------------------------------

def _read_rows(store, keep_text, skip, split, keys, domains, concepts, width):
    from services.local_index import RescoreBuilder, chunk_number, page_key, share_lists
    from services.index_snapshot import LazyTexts, compress_text

    n = len(keys)
    keyset = set(keys)
    domain_names = sorted(domains)
    domain_code = {d: i for i, d in enumerate(domain_names)}
    concept_names = sorted(concepts)
    concept_bit = {c: i for i, c in enumerate(concept_names)}
    concept_words = max(1, (len(concept_names) + 63) // 64)
    skip_set = frozenset(str(d).lower() for d in skip or ())
    rescore = settings.local_rescore
    compress = bool(settings.local_index_text_compress)

    padded = (width + 7) // 8 * 8
    codes = np.zeros((n, padded), dtype=np.uint8)
    bits = np.zeros((n, concept_words), dtype=np.uint64)
    domain_codes = np.zeros(n, dtype=np.int32)
    page_codes = np.zeros(n, dtype=np.int32)
    chunk_numbers = np.zeros(n, dtype=np.int32)
    ids, titles, urls, concept_lists = list(keys), [""] * n, [""] * n, [None] * n
    pages, shared_title, shared_url, shared_lists = {}, {}, {}, {}
    builder = None
    dims, missing_v8 = 0, 0
    text_blob, text_off, text_none = bytearray(), array("q", [0]), bytearray(n)

    select = ["id", "title", "url", "domain", "concepts", "vec"] + (["v8"] if rescore else [])
    row = 0
    for entity in store.scan_columns(select, keep_text, skip, split):
        key = _row_id(entity)
        if row >= n or key != keys[row]:
            if key in keyset:
                raise Inconsistent(f"row {key} moved or a row before it was deleted")
            continue                                   # a row added since pass 1: the next reload has it
        vec = _raw(entity.get("vec"))
        if len(vec) != width:
            raise Inconsistent(f"row {key} changed")
        codes[row, :width] = np.frombuffer(vec, dtype=np.uint8)
        title, url = entity.get("title") or "", entity.get("url") or ""
        titles[row] = shared_title.setdefault(title, title)
        urls[row] = shared_url.setdefault(url, url)
        names = [c for c in str(entity.get("concepts") or "").split(",") if c]
        concept_lists[row] = names
        for name in names:
            b = concept_bit.get(name.lower())
            if b is not None:
                bits[row, b // 64] |= np.uint64(1) << np.uint64(b % 64)
        domain = str(entity.get("domain") or "").lower()
        domain_codes[row] = domain_code.get(domain, 0)
        page_codes[row] = pages.setdefault(page_key(url), len(pages))
        chunk_numbers[row] = chunk_number(key)
        if rescore:
            v8 = _raw(entity.get("v8"))
            if builder is None and v8 and not missing_v8:
                dims = len(v8)
                builder = RescoreBuilder(n, dims)
            if builder is not None and len(v8) == dims:
                builder.add(v8)
            else:
                missing_v8 += 1
        if keep_text:
            if domain in skip_set:
                text_none[row] = 1
            else:
                text_blob += (compress_text(entity.get("text")) if compress else str(entity.get("text") or "").encode("utf-8", "replace"))
            text_off.append(len(text_blob))
        row += 1
    if row != n:
        raise Inconsistent(f"{n - row} row(s) were deleted since the first pass")

    rows_without_vectors = n
    rvec = rnorm = None
    rescore_dims = rescore_bits = 0
    if rescore and builder is not None and not missing_v8:
        rows_without_vectors = 0
        rvec, rnorm = builder.finish()
        rescore_dims, rescore_bits = builder.dims, builder.bits
    else:
        dims = 0
        if rescore:
            rows_without_vectors = max(1, missing_v8) if n else 0
    texts = None
    if keep_text:
        texts = LazyTexts(np.frombuffer(text_blob, dtype=np.uint8), np.frombuffer(text_off, dtype=np.int64),
                          np.frombuffer(text_none, dtype=np.uint8), compressed=compress)
    return {
        "ids": ids, "titles": titles, "urls": urls, "concepts": share_lists(concept_lists), "texts": texts,
        "domain_names": domain_names, "domain_codes": domain_codes, "page_codes": page_codes,
        "chunk_numbers": chunk_numbers, "distinct_pages": len(pages), "concept_names": concept_names,
        "concept_words": concept_words, "concept_bits": bits, "code_bytes": width,
        "codes": np.ascontiguousarray(codes).view(np.uint64), "rvec": rvec, "rnorm": rnorm, "vec_dims": dims,
        "rescore_dims": rescore_dims, "rescore_bits": rescore_bits,
        "rows_without_vectors": rows_without_vectors,
    }


# ---------------------------------------------------------------------------------------------------------------

def build_lean(store, keep_text=True, text_skip=(), attempts=ATTEMPTS):
    """A LocalIndex built from the table in two passes (see the module text)."""
    from services.local_index import LocalIndex, LocalIndexUnavailable

    skip = sorted({str(d).lower() for d in text_skip or () if d})
    split = bool(keep_text and skip)      # both passes must read the table in the same order
    last = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            keys, blocks, doc_len, domains, concepts, width, skipped = _read_words(store, skip, split)
            if skipped:
                log.warning("Local index: %d row(s) were unusable (missing or inconsistent vector or word data) and were left out", skipped)
            if not keys:
                raise LocalIndexUnavailable("the local index table is empty; run the backfill first")
            vocab, post_ptr, post_docs, post_weights = _build_postings(blocks, doc_len, len(keys))
            del blocks
            parts = _read_rows(store, keep_text, skip, split, keys, domains, concepts, width)
        except Inconsistent as exc:
            last = exc
            log.info("Lean index build attempt %d of %d: the table changed during the build (%s)", attempt, attempts, exc)
            continue
        index = LocalIndex.__new__(LocalIndex)
        index.n, index.skipped = len(keys), skipped
        index.vocab, index.post_ptr, index.post_docs, index.post_weights = vocab, post_ptr, post_docs, post_weights
        index.text_skip_domains = frozenset(skip)
        for name, value in parts.items():
            setattr(index, name, value)
        index._finish_lookups()
        return index
    raise LocalIndexUnavailable(f"the table kept changing while the index was built ({last})")
