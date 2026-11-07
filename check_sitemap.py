"""Show what the websearch crawler would collect from a domain's sitemaps, and where its URL cap lands.

    python check_sitemap.py www.themuse.com --include /advice
    python check_sitemap.py www.themuse.com --include /advice --max-urls 5000 --max-sitemaps 100

It copies the crawler's collection order (services/crawler.py _sitemap_urls): robots.txt "Sitemap:" lines first,
then /sitemap.xml and /sitemap_index.xml on the apex and www hosts, child sitemaps in first-in-first-out order,
stopping at max-urls page URLs or max-sitemaps sitemap files. It uses the crawler's user agent.
Read-only: it only fetches robots.txt and sitemap files. Needs `requests` (already in requirements.txt).

Reading the result:
  - "OLD behavior" is what the crawler does today: the cap fills in sitemap order, then the path filter runs.
  - "PATCHED behavior" applies the include rule while collecting (the sitemap_cap_fix.patch).
  - If OLD finds few or no matching URLs and PATCHED finds many, the sitemap cap was the problem.
"""
import argparse
import gzip
import io
import os
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from urllib.parse import urlparse, unquote

import requests

UA = "SujeethFoundationKnowledgeCrawler/0.7"


def matches(path, rules):
    candidate = "/" + unquote(path or "/").lower().strip("/")
    return any(candidate == r or candidate.startswith(r + "/") for r in rules)


class TooBig(Exception):
    pass


def fetch(session, url, max_bytes=10_000_000):
    """Stream the response and stop at max_bytes, like the crawler's read_capped (default 10,000,000)."""
    r = session.get(url, timeout=(10, 30), allow_redirects=False, stream=True)
    if r.status_code in (301, 302, 303, 307, 308) or r.status_code >= 400:
        r.close()
        return r, b""
    size = int(r.headers.get("Content-Length") or 0)
    if size > max_bytes:
        r.close()
        raise TooBig(f"{size:,} bytes (Content-Length)")
    chunks, got = [], 0
    for chunk in r.iter_content(65536):
        got += len(chunk)
        if got > max_bytes:
            r.close()
            raise TooBig(f"more than {max_bytes:,} bytes")
        chunks.append(chunk)
    data = b"".join(chunks)
    if url.lower().endswith(".gz") or data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except OSError:
            pass
    return r, data


def locs(data):
    root = None
    out = []
    for _, el in ET.iterparse(io.BytesIO(data), events=("end",)):
        tag = el.tag.rsplit("}", 1)[-1].lower()
        if root is None and tag in ("sitemapindex", "urlset"):
            root = tag
        if tag == "loc" and el.text:
            out.append(el.text.strip())
        el.clear()
    return (root or "urlset"), out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("domain")
    ap.add_argument("--include", action="append", default=[], help="allow-list path rule, repeatable (e.g. /advice)")
    ap.add_argument("--max-urls", type=int, default=None,
                    help="page limit for the domain, which caps the crawl's total pages and the page URLs collected from sitemaps; "
                         "default: its MAX_PAGES_BY_DOMAIN entry, else MAX_PAGES_PER_DOMAIN, else 10000 (read from your shell environment)")
    ap.add_argument("--max-sitemaps", type=int, default=100)
    ap.add_argument("--max-bytes", type=int, default=10_000_000, help="crawler MAX_SITEMAP_BYTES (default 10,000,000)")
    ap.add_argument("--time-budget", type=int, default=300, help="stop fetching after this many seconds and report what was found")
    ap.add_argument("--list-index", action="store_true",
                    help="print every child sitemap of the top-level index with its position, then stop (fast)")
    ap.add_argument("--only", action="append", default=[],
                    help="only open child sitemaps whose URL contains this text (repeatable), e.g. --only advice")
    ap.add_argument("--show", type=int, default=0, metavar="N",
                    help="print the first N and last N URLs the patched crawler would keep, to judge the sitemap's order")
    ap.add_argument("--base", help="override https://<host> (for testing against a local server)")
    args = ap.parse_args()

    rules = ["/" + r.strip().lower().strip("/") for r in args.include if r.strip("/ ")]
    host = args.domain.lower().strip(".")
    apex = host[4:] if host.startswith("www.") else host
    hosts = sorted({apex, "www." + apex})

    # Same rule as the crawler (services/config.py page_limit_for): the domain's MAX_PAGES_BY_DOMAIN entry, else MAX_PAGES_PER_DOMAIN.
    source = "--max-urls" if args.max_urls else "default"
    if not args.max_urls:
        args.max_urls = int(os.environ.get("MAX_PAGES_PER_DOMAIN", "10000") or 10000)
        source = "MAX_PAGES_PER_DOMAIN environment variable" if os.environ.get("MAX_PAGES_PER_DOMAIN") else "default 10000 (your deployed MAX_PAGES_PER_DOMAIN)"
        for part in os.environ.get("MAX_PAGES_BY_DOMAIN", "").split(","):
            name, _, value = part.partition("=")
            if name.strip().lower() in hosts and value.strip().isdigit() and int(value) > 0:
                args.max_urls, source = int(value), "MAX_PAGES_BY_DOMAIN environment variable"
    print(f"Page limit used: {args.max_urls} ({source})")
    base = (lambda h: args.base.rstrip("/")) if args.base else (lambda h: f"https://{h}")

    s = requests.Session()
    s.headers["User-Agent"] = UA

    # ---- robots.txt
    seeds = []
    robots_url = f"{base(host)}/robots.txt"
    try:
        r, data = fetch(s, robots_url)
        print(f"robots.txt  HTTP {r.status_code}  {robots_url}")
        if r.status_code in (401, 403):
            print("  -> the crawler would skip every page as robots_denied and never read the sitemap.")
        elif r.status_code >= 500:
            print("  -> the crawler treats this as a failure, retries, then poisons the message.")
        elif r.status_code == 200:
            for line in data.decode("utf-8", "replace").splitlines():
                if line.strip().lower().startswith("sitemap:"):
                    seeds.append(line.split(":", 1)[1].strip())
            print(f"  Sitemap lines in robots.txt: {len(seeds)}")
            for u in seeds[:10]:
                print("   ", u)
    except requests.RequestException as exc:
        print(f"robots.txt  ERROR {exc}")

    # ---- replay the crawler's collection (no URL cap, so we can see the whole picture, then apply the cap ourselves)
    candidates = list(seeds)
    for h in hosts:
        candidates += [f"{base(h)}/sitemap.xml", f"{base(h)}/sitemap_index.xml"]
    visited, files, ordered = set(), [], []          # ordered: (url, sitemap_file) in crawler order
    problems = []
    started = time.monotonic()
    while candidates and len(visited) < args.max_sitemaps:
        if time.monotonic() - started > args.time_budget:
            print(f"\n(time budget of {args.time_budget}s reached; reporting what was collected so far)")
            break
        url = candidates.pop(0)
        if url in visited:
            continue
        visited.add(url)
        print(f"fetching {url} ...", flush=True)
        t0 = time.monotonic()
        try:
            r, data = fetch(s, url, args.max_bytes)
        except TooBig as exc:
            msg = f"{url}: sitemap_too_large ({exc}); the crawler skips this file and collects nothing from it"
            problems.append(msg)
            print("  ->", msg.split(': ', 1)[1], flush=True)
            continue
        except requests.RequestException as exc:
            problems.append(f"{url}: {exc}")
            print(f"  -> error: {exc}", flush=True)
            continue
        print(f"  -> HTTP {r.status_code}, {len(data):,} bytes, {time.monotonic() - t0:.1f}s", flush=True)
        if r.status_code in (301, 302, 303, 307, 308):
            candidates.append(requests.compat.urljoin(url, r.headers.get("Location", "")))
            continue
        if r.status_code >= 400:
            problems.append(f"{url}: HTTP {r.status_code}")
            continue
        try:
            kind, found = locs(data)
        except ET.ParseError as exc:
            problems.append(f"{url}: not parseable XML ({exc})")
            continue
        pages = 0
        if kind == "sitemapindex" and args.list_index:
            print(f"\n{url} lists {len(found)} child sitemaps, in the order the crawler opens them:")
            for i, loc in enumerate(found, 1):
                print(f"  {i:>3}  {loc}")
            print("\nThe crawler stops after", args.max_sitemaps, "sitemap files in total (failed and apex-host attempts count too).")
            return
        for loc in found:
            if kind == "sitemapindex" or urlparse(loc).path.lower().endswith(".xml"):
                if args.only and kind == "sitemapindex" and not any(o in loc for o in args.only):
                    continue
                if loc not in visited:
                    candidates.append(loc)
                if kind == "sitemapindex" and urlparse(loc).path.lower().endswith(".gz"):
                    problems.append(f"{loc}: child sitemap ends in .gz; the crawler only follows .xml and would treat it as a page")
            else:
                ordered.append((loc, url))
                pages += 1
        files.append((url, kind, pages))

    print(f"\nSitemap files visited: {len(files)} (limit {args.max_sitemaps}); queued but not visited: {len(candidates)}")
    for url, kind, pages in files[:25]:
        print(f"  {kind:<12} {pages:>7} URLs  {url}")
    if len(files) > 25:
        print(f"  ... {len(files) - 25} more")
    for p in problems[:10]:
        print("  PROBLEM:", p)
    if candidates:
        print(f"  NOTE: {len(candidates)} sitemap files were never opened because the sitemap-file limit was reached.")

    total = len(ordered)
    print(f"\nPage URLs found in the visited sitemaps: {total}")
    first_cap = ordered[: args.max_urls]
    print(f"\nOLD behavior (cap {args.max_urls} in sitemap order, filter afterward)")
    seg = Counter((urlparse(u).path.strip("/").split("/")[0] or "(home)") for u, _ in first_cap)
    print("  top sections inside the cap:", ", ".join(f"/{k} {v}" for k, v in seg.most_common(8)))
    if rules:
        kept_old = [u for u, _ in first_cap if matches(urlparse(u).path, rules)]
        print(f"  URLs matching {rules} inside the cap: {len(kept_old)}")
        pos = next((i for i, (u, _) in enumerate(ordered) if matches(urlparse(u).path, rules)), None)
        print("  first matching URL appears at position:", "never" if pos is None else f"{pos + 1} of {total}")
        kept_new = [u for u, _ in ordered if matches(urlparse(u).path, rules)][: args.max_urls]
        print(f"\nPATCHED behavior (filter while collecting)\n  URLs matching {rules}: {len(kept_new)}"
              + (f"  (limit of {args.max_urls} reached; the sitemap lists more)" if len(kept_new) >= args.max_urls else ""))
        if args.show and kept_new:
            print(f"\n  first {min(args.show, len(kept_new))} kept:")
            for u in kept_new[: args.show]:
                print("   ", u)
            print(f"  last {min(args.show, len(kept_new))} kept (URL number {len(kept_new)} and earlier):")
            for u in kept_new[-args.show:]:
                print("   ", u)
        by_file = Counter(f for u, f in ordered if matches(urlparse(u).path, rules))
        for f, n in by_file.most_common(5):
            print(f"    {n:>6}  {f}")
    else:
        print("  (no --include given, so the filter comparison is skipped)")
    if total >= args.max_urls and not rules:
        print("  The cap was reached: later sitemap entries are not crawled.")


if __name__ == "__main__":
    sys.exit(main())
