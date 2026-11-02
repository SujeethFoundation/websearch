"""Count the chunks stored in Blob Storage by domain (and by first path section).

Blob names don't say which site a chunk came from, so every blob under documents/ is read once.
It only reads. Run it from the websearch folder in the websearch virtual environment:

    $secure = Read-Host "Storage connection string" -AsSecureString
    $env:BCONN = [System.Net.NetworkCredential]::new("", $secure).Password
    python .\\blob_stats.py
    Remove-Item Env:BCONN
"""
import json
import os
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

WORKERS = int(os.getenv("BLOB_STATS_WORKERS", "16"))
BATCH = 1000


def read_one(container, name):
    """Return (domain, url, size_bytes) for one blob, or (None, None, size) if unreadable."""
    for attempt in range(3):
        try:
            raw = container.download_blob(name).readall()
            doc = json.loads(raw)
            return str(doc.get("domain") or "").lower() or None, str(doc.get("url") or ""), len(raw)
        except json.JSONDecodeError:
            return None, None, 0
        except Exception:
            time.sleep(0.5 * (attempt + 1))
    return None, None, 0


def summarize(container, prefix="documents/", workers=WORKERS, progress=True):
    chunks = Counter()
    pages = defaultdict(set)
    sizes = Counter()
    sections = defaultdict(Counter)
    unreadable = 0
    done = 0
    started = time.time()

    def names():
        for blob in container.list_blobs(name_starts_with=prefix):
            if blob.name.endswith(".json"):
                yield blob.name

    with ThreadPoolExecutor(max_workers=workers) as pool:
        batch = []

        def flush():
            nonlocal unreadable, done
            for domain, url, size in pool.map(lambda n: read_one(container, n), batch):
                done += 1
                if not domain:
                    unreadable += 1
                    continue
                chunks[domain] += 1
                sizes[domain] += size
                if url:
                    pages[domain].add(url)
                    section = urlparse(url).path.strip("/").split("/")[0]
                    sections[domain][section or "(home)"] += 1
            batch.clear()
            if progress:
                print(f"  read {done:,} blobs in {time.time() - started:,.0f}s", file=sys.stderr)

        for name in names():
            batch.append(name)
            if len(batch) >= BATCH:
                flush()
        if batch:
            flush()
    return {"chunks": chunks, "pages": pages, "sizes": sizes, "sections": sections, "unreadable": unreadable, "total": done}


def count_prefix(container, prefix):
    return sum(1 for _ in container.list_blobs(name_starts_with=prefix))


def print_report(result, vectors):
    chunks, pages, sizes = result["chunks"], result["pages"], result["sizes"]
    print(f"\nChunk blobs under documents/: {result['total']:,}   (unreadable or no domain: {result['unreadable']:,})")
    print(f"Vector blobs under vectors/:  {vectors:,}\n")
    print(f"{'Domain':<32}{'Chunks':>9}{'Pages':>8}{'Avg KB':>9}")
    for domain, count in chunks.most_common():
        print(f"{domain:<32}{count:>9,}{len(pages[domain]):>8,}{sizes[domain] / count / 1024:>9.1f}")
    print(f"{'TOTAL':<32}{sum(chunks.values()):>9,}{sum(len(p) for p in pages.values()):>8,}")
    for domain, _ in chunks.most_common(3):
        print(f"\nTop sections in {domain}:")
        for section, count in result["sections"][domain].most_common(8):
            print(f"  {section:<28}{count:>8,}")


def main():
    from azure.storage.blob import BlobServiceClient

    conn = os.environ.get("BCONN") or os.environ.get("QCONN")
    if not conn:
        sys.exit("Set BCONN to the storage account connection string first (see the top of this file).")
    container_name = os.getenv("BLOB_CONTAINER", "web-content")
    container = BlobServiceClient.from_connection_string(conn).get_container_client(container_name)
    print(f"Container: {container_name}", file=sys.stderr)
    result = summarize(container)
    print_report(result, count_prefix(container, "vectors/"))


if __name__ == "__main__":
    main()
