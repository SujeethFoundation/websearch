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


def slug_suffix(path, words=2):
    """The last `words` hyphen-separated words of the final path segment: 'sales-engineer-interview-questions' -> 'interview-questions'."""
    slug = path.strip("/").split("/")[-1].lower()
    return "-".join(slug.split("-")[-words:]) if slug else "(none)"


def summarize(container, prefix="documents/", workers=WORKERS, progress=True, patterns_for=()):
    chunks = Counter()
    pages = defaultdict(set)
    sizes = Counter()
    sections = defaultdict(Counter)
    patterns = defaultdict(lambda: {1: {"chunks": Counter(), "pages": defaultdict(set)}, 2: {"chunks": Counter(), "pages": defaultdict(set)}})
    wanted = {tuple(x.lower().split("/", 1)) for x in patterns_for}
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
                    if (domain, section) in wanted:
                        for words in (1, 2):
                            suffix = slug_suffix(urlparse(url).path, words)
                            patterns[(domain, section)][words]["chunks"][suffix] += 1
                            patterns[(domain, section)][words]["pages"][suffix].add(url)
            batch.clear()
            if progress:
                print(f"  read {done:,} blobs in {time.time() - started:,.0f}s", file=sys.stderr)

        for name in names():
            batch.append(name)
            if len(batch) >= BATCH:
                flush()
        if batch:
            flush()
    return {"chunks": chunks, "pages": pages, "sizes": sizes, "sections": sections, "patterns": patterns,
            "unreadable": unreadable, "total": done}


def count_prefix(container, prefix):
    return sum(1 for _ in container.list_blobs(name_starts_with=prefix))


def print_report(result, vectors, extra_domains=(), pattern_targets=()):
    chunks, pages, sizes = result["chunks"], result["pages"], result["sizes"]
    print(f"\nChunk blobs under documents/: {result['total']:,}   (unreadable or no domain: {result['unreadable']:,})")
    print(f"Vector blobs under vectors/:  {vectors:,}\n")
    print(f"{'Domain':<32}{'Chunks':>9}{'Pages':>8}{'Avg KB':>9}")
    for domain, count in chunks.most_common():
        print(f"{domain:<32}{count:>9,}{len(pages[domain]):>8,}{sizes[domain] / count / 1024:>9.1f}")
    print(f"{'TOTAL':<32}{sum(chunks.values()):>9,}{sum(len(p) for p in pages.values()):>8,}")
    shown = [d for d, _ in chunks.most_common(3)]
    shown += [d.lower() for d in extra_domains if d.lower() not in shown]
    for domain in shown:
        limit = 8 if domain in [d for d, _ in chunks.most_common(3)] else 25
        print(f"\nTop sections in {domain} (chunks):")
        for section, count in result["sections"][domain].most_common(limit):
            print(f"  {section:<28}{count:>8,}")
    for target in pattern_targets:
        domain, _, section = target.lower().partition("/")
        found = result["patterns"].get((domain, section))
        if not found:
            print(f"\nPage-name endings under {domain}/{section}: nothing found; check the domain and section spelling")
            continue
        for words, label in ((1, "last word"), (2, "last two words")):
            print(f"\nPage-name endings under {domain}/{section} ({label} of each page name):")
            print(f"  {'ending':<34}{'chunks':>9}{'pages':>8}")
            for suffix, count in found[words]["chunks"].most_common(20):
                print(f"  {suffix:<34}{count:>9,}{len(found[words]['pages'][suffix]):>8,}")


def main(argv=None):
    import argparse
    from azure.storage.blob import BlobServiceClient

    parser = argparse.ArgumentParser(description="Count stored chunks by domain and section.")
    parser.add_argument("--domain", action="append", default=[], help="Also list the top sections of this domain (repeatable)")
    parser.add_argument("--patterns", action="append", default=[], metavar="DOMAIN/SECTION",
                        help="Group a section's pages by the ending of their names, for example tealhq.com/career-paths (repeatable)")
    args = parser.parse_args(argv)

    conn = os.environ.get("BCONN") or os.environ.get("QCONN")
    if not conn:
        sys.exit("Set BCONN to the storage account connection string first (see the top of this file).")
    container_name = os.getenv("BLOB_CONTAINER", "web-content")
    container = BlobServiceClient.from_connection_string(conn).get_container_client(container_name)
    print(f"Container: {container_name}", file=sys.stderr)
    result = summarize(container, patterns_for=args.patterns)
    print_report(result, count_prefix(container, "vectors/"), args.domain, args.patterns)


if __name__ == "__main__":
    main()
