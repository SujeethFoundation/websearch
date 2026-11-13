"""A prebuilt copy of the in-memory search index, kept in Blob Storage.

Why: every instance that starts (or reloads) scans the whole index table, parses every row in Python and builds the same
numpy arrays from them (the postings, the code matrix, the page and concept lookups). That is the same work on every
instance, every time. One instance does it once, saves the finished arrays here, and the others read them back.

Layout, in the blob container the chunks already use, under a prefix nothing else lists:
  index-snapshot/latest.json                 the manifest: which build is current and what it holds (written last)
  index-snapshot/<build>/meta.json.gz        ids, titles, urls, concepts and the domain and concept names, as JSON (gzip)
  index-snapshot/<build>/<array>.bin         one raw numpy array each (codes, vec8, postings, text ...), shapes in the manifest

The chunk text is three of those arrays, not JSON: text_blob (every chunk's UTF-8 text end to end), text_off (where each
chunk starts and ends in it) and text_none (1 for a chunk whose text stays in the table). Loading them costs a read and a
view; no Python string exists for a chunk until a search asks for it (see LazyTexts).
  index-snapshot/<build>/claim.json          who is building it, so that instances starting together build it once

<build> is the table's version token plus a short fingerprint of what the snapshot holds (text or not, which domains
keep their text out, 8-bit vectors or not).

A snapshot is used only when the table's version row still equals the snapshot's version, so it is exactly as fresh as
a scan would be. Anything else (no snapshot, a different version, a different format, a missing piece, a failed
download) means the caller scans the table as before. Nothing here can make a search fail.
"""
import gzip
import hashlib
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import numpy as np

from services.config import settings

log = logging.getLogger("sujeeth-web-search.index-snapshot")

FORMAT_VERSION = 2               # 2: the chunk text is in raw arrays (text_blob, text_off, text_none), not in meta.json.gz
CLAIM_STALE_SECONDS = 20 * 60     # a claim this old belongs to a builder that died: take it over
DOWNLOAD_WORKERS = 12             # blobs fetched at once (each one is also fetched in parallel ranges), biggest first

# Arrays of a LocalIndex that every snapshot holds, and the two that only the 8-bit rescoring needs.
_ARRAYS = ("domain_codes", "page_codes", "chunk_numbers", "concept_bits", "codes",
           "vocab", "post_docs", "post_weights", "post_ptr")
_VECTOR_ARRAYS = ("vec8", "norm8")
_TEXT_ARRAYS = ("text_blob", "text_off", "text_none")   # only when the index keeps chunk text in memory
_SCALARS = ("n", "skipped", "code_bytes", "concept_words", "distinct_pages", "vec_dims", "rows_without_vectors")


class SnapshotError(RuntimeError):
    """The snapshot is missing a piece, does not add up, or was written by another version of the format."""


# ---------------------------------------------------------------------------------------------------------------
# Chunk text without a Python string per chunk
# ---------------------------------------------------------------------------------------------------------------

class LazyTexts:
    """The chunk texts of a restored index. Reads like the list a table-built index holds: texts[i] is the text of row i,
    or None when that row's text stays in the table. The text sits in one byte buffer and a string is made only when a
    row is asked for (a search asks for about 10 rows, and up to LOCAL_PHRASE_POOL more for phrase scoring), so loading
    makes no per-chunk objects and the process holds the text once, as bytes."""

    def __init__(self, blob, off, none):
        self._view = memoryview(blob)
        self._off = off
        self._none = np.array(none, dtype=np.uint8)   # a copy: the loaded array is read-only, and rows can be hidden
        self._nbytes = int(self._view.nbytes) + int(off.nbytes) + int(self._none.nbytes)
        if len(off) != len(self._none) + 1:
            raise SnapshotError("the text offsets do not match the text flags")
        if len(off) and int(off[-1]) != self._view.nbytes:
            raise SnapshotError("the text offsets do not match the text blob")

    def __len__(self):
        return len(self._none)

    def __getitem__(self, i):
        i = int(i)
        if i < 0:
            i += len(self._none)
        if not 0 <= i < len(self._none):
            raise IndexError(i)
        if self._none[i]:
            return None
        return bytes(self._view[int(self._off[i]):int(self._off[i + 1])]).decode("utf-8")

    def __setitem__(self, i, value):
        if value is not None:
            raise TypeError("only None can be assigned: a row's text can be hidden, not replaced")
        self._none[int(i)] = 1

    def __iter__(self):
        for i in range(len(self._none)):
            yield self[i]

    def hide_rows(self, rows):
        """Mark these rows as having no text in memory (their text is read from the table)."""
        self._none[np.asarray(rows, dtype=np.int64)] = 1

    def nbytes(self):
        """What this holds in memory. Cheap: nothing is decoded."""
        return self._nbytes


def pack_texts(texts):
    """(text_blob, text_off, text_none) arrays for a list of chunk texts (None for a row whose text stays in the table)."""
    n = len(texts)
    none = np.fromiter((t is None for t in texts), dtype=np.uint8, count=n)
    encoded = [b"" if t is None else t.encode("utf-8", "replace") for t in texts]
    off = np.zeros(n + 1, dtype=np.int64)
    if n:
        np.cumsum(np.fromiter((len(b) for b in encoded), dtype=np.int64, count=n), out=off[1:])
    blob = np.frombuffer(b"".join(encoded), dtype=np.uint8)
    return blob, off, none


# ---------------------------------------------------------------------------------------------------------------
# Packing and restoring an index
# ---------------------------------------------------------------------------------------------------------------

def build_id(version, keep_text, text_skip, has_vec8):
    """The directory name of one build: the table's version plus what the snapshot holds."""
    fingerprint = json.dumps([bool(keep_text), sorted(str(d).lower() for d in text_skip or ()), bool(has_vec8)])
    return f"{version}-{hashlib.sha1(fingerprint.encode()).hexdigest()[:8]}"


def pack(index):
    """(arrays, meta, scalars) for a built LocalIndex. Nothing is copied: the arrays are the index's own."""
    arrays = {name: getattr(index, name) for name in _ARRAYS}
    if index.vec8 is not None:
        arrays["vec8"], arrays["norm8"] = index.vec8, index.norm8
    meta = {"ids": index.ids, "titles": index.titles, "urls": index.urls, "concepts": index.concepts,
            "domain_names": index.domain_names, "concept_names": index.concept_names}
    if index.texts is not None:
        # None rows (domains whose text stays in the table) are flagged in text_none
        arrays["text_blob"], arrays["text_off"], arrays["text_none"] = pack_texts(index.texts)
    scalars = {name: int(getattr(index, name)) for name in _SCALARS}
    return arrays, meta, scalars


def compatible(manifest, *, version, keep_text, text_skip, rescore):
    """(True, "") when a snapshot can stand in for a table scan right now, else (False, the reason)."""
    if not isinstance(manifest, dict):
        return False, "there is no snapshot"
    if manifest.get("format") != FORMAT_VERSION:
        return False, f"the snapshot's format is {manifest.get('format')}, this code reads {FORMAT_VERSION}"
    if not version:
        return False, "the table's version row could not be read, so freshness cannot be checked"
    if manifest.get("version") != version:
        return False, "the table has changed since the snapshot was built"
    if keep_text:
        if not manifest.get("keep_text"):
            return False, "the snapshot holds no chunk text"
        lacking = sorted(set(manifest.get("text_skip") or ()) - {str(d).lower() for d in text_skip or ()})
        if lacking:
            return False, "the snapshot has no text for " + ", ".join(lacking)
    if rescore and not manifest.get("has_vec8"):
        return False, "the snapshot has no 8-bit vectors"
    return True, ""


def restore(index_cls, manifest, arrays, meta, *, keep_text, text_skip, rescore):
    """A LocalIndex from a snapshot's parts, without running its constructor (that is the slow part).

    The caller has already checked compatible(). `text_skip` may name more domains than the snapshot did: their text is
    dropped here. It may not name fewer (compatible() refuses that)."""
    from services.local_index import split_chunk_id

    s = manifest["scalars"]
    n = int(s["n"])
    for name in ("ids", "titles", "urls", "concepts"):
        if len(meta[name]) != n:
            raise SnapshotError(f"{name} holds {len(meta[name])} entries, expected {n}")
    for name in ("domain_codes", "page_codes", "chunk_numbers", "codes", "concept_bits"):
        if arrays[name].shape[0] != n:
            raise SnapshotError(f"{name} holds {arrays[name].shape[0]} rows, expected {n}")
    if int(arrays["post_ptr"][-1]) != len(arrays["post_docs"]) or len(arrays["post_docs"]) != len(arrays["post_weights"]):
        raise SnapshotError("the keyword postings do not add up")
    if len(arrays["post_ptr"]) != len(arrays["vocab"]) + 1:
        raise SnapshotError("the keyword vocabulary does not match its postings")

    index = index_cls.__new__(index_cls)
    index.n = n
    index.skipped = int(s["skipped"])
    index.ids, index.titles, index.urls, index.concepts = meta["ids"], meta["titles"], meta["urls"], meta["concepts"]
    index.domain_names, index.concept_names = meta["domain_names"], meta["concept_names"]
    for name in ("domain_codes", "page_codes", "chunk_numbers", "concept_bits", "codes", "vocab", "post_docs",
                 "post_weights", "post_ptr"):
        setattr(index, name, arrays[name])
    index.code_bytes = int(s["code_bytes"])
    index.concept_words = int(s["concept_words"])
    index.distinct_pages = int(s["distinct_pages"])

    skip = frozenset(str(d).lower() for d in text_skip or ())
    index.text_skip_domains = skip
    if keep_text:
        for name in _TEXT_ARRAYS:
            if name not in arrays:
                raise SnapshotError(f"the snapshot has no {name}")
        texts = LazyTexts(arrays["text_blob"], arrays["text_off"], arrays["text_none"])
        if len(texts) != n:
            raise SnapshotError(f"the text holds {len(texts)} entries, expected {n}")
        extra = skip - set(manifest.get("text_skip") or ())
        if extra:
            codes = [i for i, d in enumerate(index.domain_names) if d in extra]
            if codes:
                texts.hide_rows(np.flatnonzero(np.isin(index.domain_codes, np.array(codes, dtype=np.int32))))
        index.texts = texts
    else:
        index.texts = None

    if rescore and "vec8" in arrays:
        index.vec8, index.norm8, index.vec_dims = arrays["vec8"], arrays["norm8"], int(s["vec_dims"])
        index.rows_without_vectors = int(s["rows_without_vectors"])
    else:
        # What the constructor leaves when rescoring is off: no 8-bit vectors, and every row counted as without one.
        index.vec8, index.norm8, index.vec_dims = None, None, 0
        index.rows_without_vectors = n if not rescore else int(s["rows_without_vectors"])

    # The lookups the constructor derives from the ids and urls (cheap next to the postings).
    index.by_id = {cid: i for i, cid in enumerate(index.ids)}
    pages = {}
    for i, cid in enumerate(index.ids):
        page_id, number = split_chunk_id(cid)
        pages.setdefault(page_id, []).append((number, i))
    index.pages = {p: [i for _, i in sorted(items)] for p, items in pages.items()}
    by_url = {}
    for page_rows in index.pages.values():
        for i in page_rows:
            by_url.setdefault(index.urls[i], []).append(i)
    index.by_url = by_url
    index._rows_cache = {}
    return index


# ---------------------------------------------------------------------------------------------------------------
# Blob Storage
# ---------------------------------------------------------------------------------------------------------------

class SnapshotStore:
    """Reads and writes snapshots in one blob container. Takes a ContainerClient so tests can pass a fake."""

    def __init__(self, container, prefix=None):
        self.container = container
        self.prefix = (prefix or settings.index_snapshot_prefix).strip("/")

    @classmethod
    def from_settings(cls):
        """The store for the configured container, or None when snapshots are off or there is no blob connection."""
        if not settings.index_snapshot_enabled or not settings.blob_connection_string:
            return None
        try:
            from azure.storage.blob import BlobServiceClient
        except ImportError:
            return None
        service = BlobServiceClient.from_connection_string(settings.blob_connection_string)
        return cls(service.get_container_client(settings.blob_container))

    @property
    def manifest_name(self):
        return f"{self.prefix}/latest.json"

    # -- small helpers ----------------------------------------------------------------------------------------
    def _put(self, name, data, overwrite=True):
        self.container.upload_blob(name, data, overwrite=overwrite, max_concurrency=settings.index_snapshot_concurrency)

    def _get(self, name):
        return self.container.download_blob(name, max_concurrency=settings.index_snapshot_concurrency).readall()

    def _modified(self, name):
        """When a blob was last written (UTC), or None when it is not there."""
        for blob in self.container.list_blobs(name_starts_with=name):
            if blob.name == name:
                return blob.last_modified
        return None

    # -- reading ----------------------------------------------------------------------------------------------
    def read_manifest(self):
        """The current manifest, or None when no snapshot has been published."""
        from azure.core.exceptions import ResourceNotFoundError
        try:
            return json.loads(self._get(self.manifest_name))
        except ResourceNotFoundError:
            return None

    def load(self, manifest, index_cls, *, keep_text, text_skip, rescore):
        """The LocalIndex a manifest describes. Without rescoring the 8-bit vectors (the largest blob) are not fetched."""
        base = f"{self.prefix}/{manifest['build']}"
        specs = manifest["arrays"]
        names = [name for name in specs
                 if (rescore or name not in _VECTOR_ARRAYS) and (keep_text or name not in _TEXT_ARRAYS)]
        for name in _ARRAYS:
            if name not in names:
                raise SnapshotError(f"the manifest lists no {name}")
        if keep_text:
            for name in _TEXT_ARRAYS:
                if name not in names:
                    raise SnapshotError(f"the manifest lists no {name}")
        # The biggest blobs start first, so the slowest download does not wait behind the small ones.
        names.sort(key=lambda name: -int(specs[name]["bytes"]))
        began = time.perf_counter()
        with ThreadPoolExecutor(max_workers=max(1, min(DOWNLOAD_WORKERS, len(names) + 1))) as pool:
            blobs = list(pool.map(self._get, [f"{base}/meta.json.gz"] + [f"{base}/{name}.bin" for name in names]))
        downloaded = time.perf_counter()
        meta = json.loads(gzip.decompress(blobs[0]))
        blobs[0] = None
        parsed = time.perf_counter()
        arrays = {}
        for name, data in zip(names, blobs[1:]):
            spec = specs[name]
            if len(data) != spec["bytes"]:
                raise SnapshotError(f"{name} is {len(data)} bytes, the manifest says {spec['bytes']}")
            arrays[name] = np.frombuffer(data, dtype=np.dtype(spec["dtype"])).reshape(spec["shape"])
        del blobs
        index = restore(index_cls, manifest, arrays, meta, keep_text=keep_text, text_skip=text_skip, rescore=rescore)
        log.info("Index snapshot read: download %.1f s, metadata %.1f s, restore %.1f s",
                 downloaded - began, parsed - downloaded, time.perf_counter() - parsed)
        return index

    # -- writing ----------------------------------------------------------------------------------------------
    def claim(self, build):
        """True when this caller should build `build`: nobody has, or the last builder died. Instances that start
        together all try; one wins."""
        from azure.core.exceptions import ResourceExistsError
        name = f"{self.prefix}/{build}/claim.json"
        body = json.dumps({"at": datetime.now(timezone.utc).isoformat(), "pid": os.getpid()}).encode()
        try:
            self._put(name, body, overwrite=False)
            return True
        except ResourceExistsError:
            modified = self._modified(name)
            if modified is not None and (datetime.now(timezone.utc) - modified).total_seconds() < CLAIM_STALE_SECONDS:
                return False
            self._put(name, body, overwrite=True)
            return True

    def publish(self, index, version, text_skip, *, force=False):
        """Save a built index as the current snapshot. Returns the manifest, or None when another instance is
        already building the same snapshot (unless force). The manifest goes last, so readers never see a half-written
        build."""
        arrays, meta, scalars = pack(index)
        keep_text = index.texts is not None
        skip = sorted(str(d).lower() for d in text_skip or ())
        build = build_id(version, keep_text, skip, "vec8" in arrays)
        if not force and not self.claim(build):
            return None
        base = f"{self.prefix}/{build}"
        try:
            payload = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "replace")
            self._put(f"{base}/meta.json.gz", gzip.compress(payload, compresslevel=3))
            del payload
            files = {}
            for name, array in arrays.items():
                array = np.ascontiguousarray(array)
                self._put(f"{base}/{name}.bin", array.tobytes())
                files[name] = {"dtype": array.dtype.str, "shape": list(array.shape), "bytes": int(array.nbytes)}
            manifest = {"format": FORMAT_VERSION, "version": version, "build": build,
                        "built_at": datetime.now(timezone.utc).isoformat(), "keep_text": keep_text,
                        "text_skip": skip, "has_vec8": "vec8" in arrays, "scalars": scalars, "arrays": files}
            self._put(self.manifest_name, json.dumps(manifest).encode("utf-8"))
        except Exception:
            try:
                self.container.delete_blob(f"{base}/claim.json")  # let the next instance try at once
            except Exception:
                pass
            raise
        try:
            self.cleanup(build)
        except Exception:
            log.warning("Index snapshot cleanup failed", exc_info=True)
        return manifest

    def cleanup(self, keep_build, keep_seconds=None):
        """Delete the files of builds other than `keep_build` that nothing has written to for keep_seconds (a reader
        may still be downloading the previous build)."""
        keep_seconds = settings.index_snapshot_keep_seconds if keep_seconds is None else keep_seconds
        now = datetime.now(timezone.utc)
        newest, names = {}, {}
        for blob in self.container.list_blobs(name_starts_with=self.prefix + "/"):
            rest = blob.name[len(self.prefix) + 1:]
            build, slash, _ = rest.partition("/")
            if not slash:
                continue  # latest.json
            names.setdefault(build, []).append(blob.name)
            if build not in newest or blob.last_modified > newest[build]:
                newest[build] = blob.last_modified
        removed = 0
        for build, files in names.items():
            if build != keep_build and (now - newest[build]).total_seconds() >= keep_seconds:
                for name in files:
                    self.container.delete_blob(name)
                    removed += 1
        return removed
