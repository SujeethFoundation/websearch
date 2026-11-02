"""Check that the purge worked: stored chunks, vectors, and (optionally) the search index match the ontology's exclusions.

It only reads. Run it from the websearch folder, in the websearch virtual environment, after the queue has drained:

    $secure = Read-Host "Storage connection string" -AsSecureString
    $env:BCONN = [System.Net.NetworkCredential]::new("", $secure).Password
    python .\\verify_purge.py --ontology ..\\ontology\\ontology\\ontology.json
    python .\\verify_purge.py --ontology ..\\ontology\\ontology\\ontology.json --check-index sf-knowledge-v2   # also needs SEARCH_ADMIN_KEY

Checks (exit code 1 if any FAIL):
  1. No stored chunk is under an excluded path for its domain.
  2. No vector blob is left without its chunk blob.
  3. Chunks from domains the ontology no longer lists (warning only).
  4. With --check-index: no indexed document is under an excluded path.
  5. With --check-index: every indexed document still has its chunk blob (needed when the index is blob-backed).
"""
import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

import requests

from services.path_rules import is_excluded_url, normalize_rules

WORKERS = int(os.getenv("VERIFY_WORKERS", "16"))
DEFAULT_ENDPOINT = os.getenv("AZURE_SEARCH_ENDPOINT", "https://sujeeth-aisearch.search.windows.net")


def load_rules(path):
    data = json.load(open(path, encoding="utf-8"))
    rules, domains = {}, set()
    for row in data.get("domains", []):
        domain = str(row.get("domain") or "").lower()
        domains.add(domain)
        found = normalize_rules((row.get("crawl_policy") or {}).get("exclude_paths"))
        if found:
            rules[domain] = found
    return data.get("ontology_version"), rules, domains


def which_rule(url, rules):
    for rule in rules:
        if is_excluded_url(url, (rule,)):
            return rule
    return None


def read_chunk(container, name):
    for attempt in range(3):
        try:
            doc = json.loads(container.download_blob(name).readall())
            return str(doc.get("id") or ""), str(doc.get("domain") or "").lower(), str(doc.get("url") or "")
        except json.JSONDecodeError:
            return "", "", ""
        except Exception:
            time.sleep(0.5 * (attempt + 1))
    return "", "", ""


def scan_blobs(container, rules, workers=WORKERS, progress=True):
    names = [b.name for b in container.list_blobs(name_starts_with="documents/") if b.name.endswith(".json")]
    vector_ids = {b.name[len("vectors/"):-len(".json")] for b in container.list_blobs(name_starts_with="vectors/") if b.name.endswith(".json")}
    per_domain, offenders, unreadable = Counter(), defaultdict(list), 0
    offender_counts = defaultdict(Counter)
    chunk_ids = set()
    started = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for start in range(0, len(names), 1000):
            for doc_id, domain, url in pool.map(lambda n: read_chunk(container, n), names[start:start + 1000]):
                if not doc_id:
                    unreadable += 1
                    continue
                chunk_ids.add(doc_id)
                per_domain[domain] += 1
                rule = which_rule(url, rules.get(domain, ()))
                if rule:
                    offender_counts[domain][rule] += 1
                    if len(offenders[domain]) < 5:
                        offenders[domain].append(url)
            if progress:
                print(f"  read {min(start + 1000, len(names)):,} of {len(names):,} chunk blobs in {time.time() - started:,.0f}s", file=sys.stderr)
    return {"names": names, "chunk_ids": chunk_ids, "vector_ids": vector_ids, "per_domain": per_domain,
            "offender_counts": offender_counts, "offenders": offenders, "unreadable": unreadable}


def scan_index(session, endpoint, index, admin_key, api_version, rules):
    ids, offender_counts, offenders, skip = set(), defaultdict(Counter), defaultdict(list), 0
    while skip < 100000:
        r = session.post(f"{endpoint}/indexes/{index}/docs/search?api-version={api_version}",
                         headers={"api-key": admin_key, "Content-Type": "application/json"},
                         data=json.dumps({"search": "*", "select": "id,url,domain", "top": 1000, "skip": skip}), timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"Search index read failed: HTTP {r.status_code} {r.text[:200]}")
        page = r.json().get("value", [])
        if not page:
            break
        for doc in page:
            ids.add(str(doc.get("id")))
            domain, url = str(doc.get("domain") or "").lower(), str(doc.get("url") or "")
            rule = which_rule(url, rules.get(domain, ()))
            if rule:
                offender_counts[domain][rule] += 1
                if len(offenders[domain]) < 5:
                    offenders[domain].append(url)
        skip += 1000
    return {"ids": ids, "offender_counts": offender_counts, "offenders": offenders}


def verdict(ok):
    return "PASS" if ok else "FAIL"


def report(version, rules, ontology_domains, blobs, index_result=None, index_name=None):
    failures = 0
    print(f"\nOntology {version}: exclusion rules for {', '.join(f'{d} ({len(r)})' for d, r in sorted(rules.items())) or 'no domains'}")
    print(f"Chunk blobs: {len(blobs['names']):,}   readable: {len(blobs['names']) - blobs['unreadable']:,}   vector blobs: {len(blobs['vector_ids']):,}")
    print(f"\n{'Domain':<34}{'Chunks':>9}")
    for domain, count in blobs["per_domain"].most_common():
        print(f"{domain:<34}{count:>9,}")

    stray = sum(sum(c.values()) for c in blobs["offender_counts"].values())
    failures += stray > 0
    print(f"\nCHECK 1  stored chunks under an excluded path   : {stray:,}   {verdict(stray == 0)}")
    for domain, counts in blobs["offender_counts"].items():
        for rule, count in counts.most_common():
            print(f"           {domain}  {rule}: {count:,}")
        for url in blobs["offenders"][domain]:
            print(f"             e.g. {url}")

    orphans = sorted(blobs["vector_ids"] - blobs["chunk_ids"])
    failures += len(orphans) > 0
    print(f"CHECK 2  vector blobs without a chunk blob      : {len(orphans):,}   {verdict(not orphans)}")
    for doc_id in orphans[:5]:
        print(f"             e.g. vectors/{doc_id}.json")

    unknown = {d: c for d, c in blobs["per_domain"].items() if d not in ontology_domains}
    print(f"CHECK 3  chunks from domains not in the ontology: {sum(unknown.values()):,}   {'OK' if not unknown else 'WARN'}"
          + (f"   ({', '.join(f'{d}: {c:,}' for d, c in unknown.items())})" if unknown else ""))

    missing_vectors = len(blobs["chunk_ids"] - blobs["vector_ids"])
    print(f"INFO     chunks with no stored vector yet       : {missing_vectors:,} (embedded when they are first indexed)")

    if index_result is not None:
        stray_ix = sum(sum(c.values()) for c in index_result["offender_counts"].values())
        failures += stray_ix > 0
        print(f"\nCHECK 4  indexed documents under an excluded path: {stray_ix:,}   {verdict(stray_ix == 0)}   (index {index_name})")
        for domain, counts in index_result["offender_counts"].items():
            for rule, count in counts.most_common():
                print(f"           {domain}  {rule}: {count:,}")
            for url in index_result["offenders"][domain]:
                print(f"             e.g. {url}")
        no_blob = sorted(index_result["ids"] - blobs["chunk_ids"])
        failures += len(no_blob) > 0
        print(f"CHECK 5  indexed documents with no chunk blob   : {len(no_blob):,}   {verdict(not no_blob)}")
        for doc_id in no_blob[:5]:
            print(f"             e.g. {doc_id}")
        print(f"INFO     indexed documents: {len(index_result['ids']):,}   chunk blobs not indexed yet: {len(blobs['chunk_ids'] - index_result['ids']):,}")
    print("\nRESULT:", "everything checked is clean." if not failures else f"{failures} check(s) failed. If the queue is still draining, wait and run it again.")
    return failures


def main(argv=None, container=None, session=None):
    parser = argparse.ArgumentParser(description="Verify the purge of excluded content.")
    parser.add_argument("--ontology", required=True, help="Path to ontology.json (the same version the app uses)")
    parser.add_argument("--check-index", metavar="INDEX", help="Also check this search index (needs SEARCH_ADMIN_KEY)")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--api-version", default=os.getenv("AZURE_SEARCH_API_VERSION", "2025-09-01"))
    args = parser.parse_args(argv)
    version, rules, ontology_domains = load_rules(args.ontology)
    if container is None:
        from azure.storage.blob import BlobServiceClient
        conn = os.environ.get("BCONN") or os.environ.get("QCONN")
        if not conn:
            sys.exit("Set BCONN to the storage account connection string first (see the top of this file).")
        container = BlobServiceClient.from_connection_string(conn).get_container_client(os.getenv("BLOB_CONTAINER", "web-content"))
    blobs = scan_blobs(container, rules)
    index_result = None
    if args.check_index:
        key = os.environ.get("SEARCH_ADMIN_KEY")
        if not key:
            sys.exit("Set SEARCH_ADMIN_KEY to check the index.")
        index_result = scan_index(session or requests.Session(), args.endpoint, args.check_index, key, args.api_version, rules)
    return 1 if report(version, rules, ontology_domains, blobs, index_result, args.check_index) else 0


if __name__ == "__main__":
    sys.exit(main())
