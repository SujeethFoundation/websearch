"""Count stored chunk blobs by domain and first path segment, and flag any the crawl rules would not allow:
under an excluded path (exclude_paths) or outside a domain's allow-list (include_paths).

It only reads. Every blob under documents/ is opened once, because blob names don't say which site a chunk came from.
Run it from the websearch folder, in the websearch virtual environment:

    $secure = Read-Host "Storage connection string" -AsSecureString
    $env:BCONN = [System.Net.NetworkCredential]::new("", $secure).Password
    python .\\blob_paths_report.py --ontology ..\\ontology\\ontology\\ontology.json
    python .\\blob_paths_report.py --ontology ..\\ontology\\ontology\\ontology.json --csv blob_paths.csv --top 25
    python .\\blob_paths_report.py --ontology ..\\ontology\\ontology\\ontology.json --domain tealhq.com
    Remove-Item Env:BCONN

Output:
  1. Chunks per domain (with pages and how many chunks are out of scope).
  2. Per domain, every first path segment with its chunk count, page count, and an OUT OF SCOPE flag.
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


def first_segment(url):
    segment = urlparse(url).path.strip("/").split("/")[0].lower()
    return segment or "(home)"


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


def scan(container, rules, only_domain=None, workers=WORKERS, progress=True, includes=None):
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
                section = first_segment(url)
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


def report(version, rules, includes, ontology_domains, result, top, csv_path):
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
    for domain, counter in sorted(chunks.items(), key=lambda kv: -sum(kv[1].values())):
        print(f"\n{domain}  (first path segment, by chunks)")
        print(f"  {'Segment':<34}{'Chunks':>9}{'Pages':>8}{'Out':>10}  Flag")
        shown = counter.most_common()
        for i, (section, count) in enumerate(shown):
            bad = flagged[domain][section]
            flag = status(count, bad)
            rows.append([domain, section, count, len(pages[domain][section]), bad, flag])
            # Always show flagged sections; cap the rest at --top.
            if i < top or bad:
                print(f"  {section:<34}{count:>9,}{len(pages[domain][section]):>8,}{bad:>10,}  {flag}")
        hidden = [s for i, (s, c) in enumerate(shown) if i >= top and not flagged[domain][s]]
        if hidden:
            print(f"  ... {len(hidden):,} more segments with {sum(counter[s] for s in hidden):,} chunks (use --top to show more, or --csv for all)")

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
            writer.writerow(["domain", "first_segment", "chunks", "pages", "chunks_out_of_scope", "flag"])
            writer.writerows(rows)
        print(f"\nFull table written to {csv_path}")
    return 1 if total_bad else 0


def main(argv=None, container=None):
    parser = argparse.ArgumentParser(description="Chunk blobs by domain and first path segment, flagging excluded paths and pages outside an allow-list.")
    parser.add_argument("--ontology", required=True, help="Path to ontology.json (the same version the app uses)")
    parser.add_argument("--domain", help="Only report this domain")
    parser.add_argument("--top", type=int, default=15, help="Segments to show per domain (flagged ones always show). Default 15")
    parser.add_argument("--csv", help="Also write the full domain x segment table to this file")
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
    result = scan(container, rules, only_domain=args.domain.lower() if args.domain else None, includes=includes)
    return report(version, rules, includes, ontology_domains, result, args.top, args.csv)


if __name__ == "__main__":
    sys.exit(main())
