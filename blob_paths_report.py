"""Count stored chunk blobs by domain and two-level path section, and flag any the crawl rules would not allow:
under an excluded path (exclude_paths) or outside a domain's allow-list (include_paths).

It only reads. Every blob under documents/ is opened once, because blob names don't say which site a chunk came from.
Run it from the websearch folder, in the websearch virtual environment:

    $secure = Read-Host "Storage connection string" -AsSecureString
    $env:BCONN = [System.Net.NetworkCredential]::new("", $secure).Password
    python .\\blob_paths_report.py --ontology ..\\ontology\\ontology\\ontology.json
    python .\\blob_paths_report.py --ontology ..\\ontology\\ontology\\ontology.json --csv blob_paths.csv --top 25
    python .\\blob_paths_report.py --ontology ..\\ontology\\ontology\\ontology.json --domain tealhq.com
    python .\\blob_paths_report.py --ontology ..\\ontology\\ontology\\ontology.json --domain tealhq.com --depth 1 --top 15
    Remove-Item Env:BCONN

Output:
  1. Chunks per domain (with pages and how many chunks are out of scope).
  2. Per domain, every path section with its chunk count, page count, and an OUT OF SCOPE flag. A section is the
     first path segment (/post), or the first two (/post/some-article), which is the default (--depth 1 gives
     one). Only the top 10 sections per domain are printed (--top); the rest are summarized on one line, with
     how many of their chunks are out of scope. With --collapse N, a segment with more than N children is shown as one row, for example career-paths/*.
     --csv always writes the full, uncollapsed table.
  3. A violations list: each (domain, rule) with its blob count and sample URLs. A chunk outside an
     allow-list is listed under "outside include_paths [...]".

Exit code 1 if any chunk is out of scope, so it can gate a pipeline step.

A section is flagged OUT OF SCOPE when every chunk in it is excluded or outside the allow-list, and
PARTIAL when only some are (for example a wildcard rule like /career-paths/*-certifications hits only
part of /career-paths). The bare homepage is never out of scope, even with an allow-list.
Matching uses services.path_rules, the same code the crawler and purge use.
"""
import argparse
import csv
import json
import os
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from services.path_rules import is_excluded_url, is_outside_includes, normalize_rules

WORKERS = int(os.getenv("BLOB_PATHS_WORKERS", "16"))
BATCH = 1000
SAMPLES = 5


def load_rules(path):
    """Return (ontology_version, exclude rules by domain, include rules by domain, all domains)."""
    data = json.load(open(path, encoding="utf-8"))
    rules, includes, domains = {}, {}, set()
    for row in data.get("domains", []):
        domain = str(row.get("domain") or "").lower()
        domains.add(domain)
        policy = row.get("crawl_policy") or {}
        found = normalize_rules(policy.get("exclude_paths"))
        if found:
            rules[domain] = found
        allowed = normalize_rules(policy.get("include_paths"))
        if allowed:
            includes[domain] = allowed
    return data.get("ontology_version"), rules, includes, domains


DEFAULT_DEPTH = 2      # path segments per section: /post/some-article
DEFAULT_TOP = 10       # sections shown per domain, largest first
DEFAULT_COLLAPSE = 0   # 0 = list real two-level sections; set e.g. 10 to fold wide ones into career-paths/*


def path_section(url, depth=1):
    """The first `depth` path segments of a URL, lowercased, or "(home)" for the bare domain."""
    parts = [part for part in urlparse(url).path.lower().split("/") if part][:max(1, depth)]
    return "/".join(parts) or "(home)"


def first_segment(url):
    return path_section(url, 1)


def group_sections(counter, flagged, pages, depth, collapse):
    """Fold wide second levels into one "segment/*" row. Only used for the console table.

    With depth 1, or collapse 0, the table is returned unchanged. Otherwise a first segment that
    has more than `collapse` distinct sections beneath it becomes a single row with their chunks,
    pages and out-of-scope counts added together.
    """
    if depth <= 1 or collapse <= 0:
        return Counter(counter), Counter(flagged), {s: set(p) for s, p in pages.items()}
    children = defaultdict(list)
    for section in counter:
        children[section.split("/")[0]].append(section)
    out_chunks, out_flagged, out_pages = Counter(), Counter(), {}
    for first, sections in children.items():
        if len(sections) > collapse:
            key = first + "/*"
            out_chunks[key] = sum(counter[s] for s in sections)
            out_flagged[key] = sum(flagged[s] for s in sections)
            out_pages[key] = set().union(*(pages[s] for s in sections))
        else:
            for section in sections:
                out_chunks[section] = counter[section]
                out_flagged[section] = flagged[section]
                out_pages[section] = set(pages[section])
    return out_chunks, out_flagged, out_pages


def matching_rule(url, rules, includes=()):
    """Why the crawl rules would not allow this URL, or None if they would.

    A matching exclude rule is reported first (exclude_paths wins over include_paths).
    """
    for rule in rules:
        if is_excluded_url(url, (rule,)):
            return rule
    if is_outside_includes(url, includes):
        return "outside include_paths [" + ", ".join(includes) + "]"
    return None


def read_one(container, name):
    """Return (domain, url) for one chunk blob, or (None, None) if it can't be read."""
    for attempt in range(3):
        try:
            doc = json.loads(container.download_blob(name).readall())
            return str(doc.get("domain") or "").lower() or None, str(doc.get("url") or "")
        except json.JSONDecodeError:
            return None, None
        except Exception:
            time.sleep(0.5 * (attempt + 1))
    return None, None


def scan(container, rules, only_domain=None, workers=WORKERS, progress=True, includes=None, depth=DEFAULT_DEPTH):
    includes = includes or {}
    names = [b.name for b in container.list_blobs(name_starts_with="documents/") if b.name.endswith(".json")]
    chunks = defaultdict(Counter)        # domain -> section -> chunks
    flagged = defaultdict(Counter)       # domain -> section -> chunks out of scope (excluded or outside the allow-list)
    pages = defaultdict(lambda: defaultdict(set))
    violations = defaultdict(Counter)    # domain -> rule -> chunks
    samples = defaultdict(lambda: defaultdict(list))
    unreadable = no_url = skipped = 0
    started = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for start in range(0, len(names), BATCH):
            for domain, url in pool.map(lambda n: read_one(container, n), names[start:start + BATCH]):
                if not domain:
                    unreadable += 1
                    continue
                if only_domain and domain != only_domain:
                    skipped += 1
                    continue
                if not url:
                    no_url += 1
                    chunks[domain]["(no url)"] += 1
                    continue
                section = path_section(url, depth)
                chunks[domain][section] += 1
                pages[domain][section].add(url)
                rule = matching_rule(url, rules.get(domain, ()), includes.get(domain, ()))
                if rule:
                    flagged[domain][section] += 1
                    violations[domain][rule] += 1
                    if len(samples[domain][rule]) < SAMPLES:
                        samples[domain][rule].append(url)
            if progress:
                print(f"  read {min(start + BATCH, len(names)):,} of {len(names):,} blobs in {time.time() - started:,.0f}s", file=sys.stderr)
    return {"total": len(names), "chunks": chunks, "flagged": flagged, "pages": pages, "violations": violations,
            "samples": samples, "unreadable": unreadable, "no_url": no_url, "skipped": skipped}


def status(total, bad):
    if not bad:
        return ""
    return "OUT OF SCOPE" if bad == total else "PARTIAL"


def report(version, rules, includes, ontology_domains, result, top, csv_path, depth=DEFAULT_DEPTH, collapse=DEFAULT_COLLAPSE):
    chunks, flagged, pages = result["chunks"], result["flagged"], result["pages"]
    print(f"\nOntology {version}: exclusion rules for "
          + (", ".join(f"{d} {list(r)}" for d, r in sorted(rules.items())) or "no domains"))
    print("Allow-lists (include_paths) for "
          + (", ".join(f"{d} {list(r)}" for d, r in sorted(includes.items())) or "no domains"))
    print(f"Chunk blobs under documents/: {result['total']:,}   unreadable: {result['unreadable']:,}   "
          f"no url: {result['no_url']:,}" + (f"   other domains skipped: {result['skipped']:,}" if result["skipped"] else ""))

    print(f"\n{'Domain':<34}{'Chunks':>9}{'Pages':>8}{'Out of scope':>20}")
    for domain, counter in sorted(chunks.items(), key=lambda kv: -sum(kv[1].values())):
        n_pages = sum(len(p) for p in pages[domain].values())
        bad = sum(flagged[domain].values())
        print(f"{domain:<34}{sum(counter.values()):>9,}{n_pages:>8,}{bad:>20,}")
    unknown = [d for d in chunks if d not in ontology_domains]
    if unknown:
        print(f"WARN  domains in storage but not in the ontology: {', '.join(unknown)}")

    rows = []
    levels = "first path segment" if depth <= 1 else f"first {depth} path segments"
    for domain, counter in sorted(chunks.items(), key=lambda kv: -sum(kv[1].values())):
        print(f"\n{domain}  ({levels}, by chunks)")
        for section, count in counter.most_common():  # the CSV keeps every section, uncollapsed
            rows.append([domain, section, count, len(pages[domain][section]), flagged[domain][section],
                         status(count, flagged[domain][section])])
        g_chunks, g_flagged, g_pages = group_sections(counter, flagged[domain], pages[domain], depth, collapse)
        shown = g_chunks.most_common()
        width = min(60, max(34, max((len(section) for section, _ in shown), default=0) + 2))
        print(f"  {'Segment':<{width}}{'Chunks':>9}{'Pages':>8}{'Out':>10}  Flag")
        for section, count in shown[:top]:
            bad = g_flagged[section]
            print(f"  {section:<{width}}{count:>9,}{len(g_pages[section]):>8,}{bad:>10,}  {status(count, bad)}")
        hidden = shown[top:]
        if hidden:
            hidden_bad = sum(g_flagged[section] for section, _ in hidden)
            note = f", {hidden_bad:,} of them out of scope" if hidden_bad else ""
            print(f"  ... {len(hidden):,} more sections with {sum(count for _, count in hidden):,} chunks{note} "
                  "(use --top to show more, or --csv for all)")

    total_bad = sum(sum(c.values()) for c in result["violations"].values())
    print(f"\nOUT-OF-SCOPE BLOBS: {total_bad:,}   {'PASS' if not total_bad else 'FAIL'}")
    for domain, counts in result["violations"].items():
        for rule, count in counts.most_common():
            print(f"  {domain}  {rule}: {count:,}")
            for url in result["samples"][domain][rule]:
                print(f"      e.g. {url}")

    if csv_path:
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["domain", "section", "chunks", "pages", "chunks_out_of_scope", "flag"])
            writer.writerows(rows)
        print(f"\nFull table written to {csv_path}")
    return 1 if total_bad else 0


def main(argv=None, container=None):
    parser = argparse.ArgumentParser(description="Chunk blobs by domain and path section, flagging excluded paths and pages outside an allow-list.")
    parser.add_argument("--ontology", required=True, help="Path to ontology.json (the same version the app uses)")
    parser.add_argument("--domain", help="Only report this domain")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP, help=f"Sections to show per domain, largest first; the rest are summarized on one line. Default {DEFAULT_TOP}")
    parser.add_argument("--csv", help="Also write the full domain x section table to this file (never collapsed)")
    parser.add_argument("--depth", type=int, default=DEFAULT_DEPTH, choices=[1, 2, 3, 4],
                        help=f"Path segments to group by: 1 = /post, 2 = /post/some-article. Default {DEFAULT_DEPTH}")
    parser.add_argument("--collapse", type=int, default=DEFAULT_COLLAPSE,
                        help="With --depth above 1, show a segment with more than this many children as one row "
                             f"(for example career-paths/*). 0 lists every section. Default {DEFAULT_COLLAPSE}")
    args = parser.parse_args(argv)
    version, rules, includes, ontology_domains = load_rules(args.ontology)
    if container is None:
        from azure.storage.blob import BlobServiceClient
        conn = os.environ.get("BCONN") or os.environ.get("QCONN")
        if not conn:
            sys.exit("Set BCONN to the storage account connection string first (see the top of this file).")
        name = os.getenv("BLOB_CONTAINER", "web-content")
        print(f"Container: {name}", file=sys.stderr)
        container = BlobServiceClient.from_connection_string(conn).get_container_client(name)
    result = scan(container, rules, only_domain=args.domain.lower() if args.domain else None,
                  includes=includes, depth=args.depth)
    return report(version, rules, includes, ontology_domains, result, args.top, args.csv,
                  depth=args.depth, collapse=args.collapse)


if __name__ == "__main__":
    sys.exit(main())
