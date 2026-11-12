"""Score the search on questions whose right answer you know.

    python tests/relevance_eval.py                    the deployed app
    python tests/relevance_eval.py --explain          ...and, for each question not answered first, why: where the right
                                                      page stands in each part of the ranking
    python tests/relevance_eval.py --try phrase_boost=0
                                                      ...and a second column: the same app with these ranking settings
                                                      for this run only (concept_boost, phrase_boost, max_per_page,
                                                      unique_pages)

The app's address is --url, else WEBSEARCH_URL, else the address crawl-helper.ps1 uses; "https://" is added when left off.
The Function key is --key, else WEBSEARCH_KEY; with neither it is asked for (hidden), as crawl-helper.ps1 does.

The questions are in tests/relevance_gold.json: each has a "question" and "expect", a list of pieces of URL. A search
answers a question when one of its results has a URL containing one of those pieces (case does not matter); the place of
the first such result is its rank. The file ships with a few questions as a start: replace them with questions your
students and mentors really ask, and the page each should lead to. Thirty to fifty make the numbers worth reading.

Reports, per column: how many questions had the right page first, in the top three, in the top five, and the mean
reciprocal rank (1 for first place, 1/2 for second, ... 0 when missing; higher is better).
"""
import argparse
import getpass
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "https://websearch-h9arcweeftfrd8e0.eastus-01.azurewebsites.net"   # the address crawl-helper.ps1 uses too


def normalize_url(url):
    """The app's address with a scheme and no trailing slash: 'host.net/' becomes 'https://host.net'."""
    text = str(url or "").strip().rstrip("/")
    if not text:
        return ""
    return text if "://" in text else "https://" + text


def rank_of(expect, urls):
    """The 1-based place of the first url containing any expected piece, or None."""
    wanted = [str(e).lower() for e in expect if str(e).strip()]
    for place, url in enumerate(urls, 1):
        if any(piece in str(url).lower() for piece in wanted):
            return place
    return None


def summarize(ranks):
    """{'questions', 'first', 'top3', 'top5', 'mrr'} from a list of ranks (None = not found)."""
    n = len(ranks)
    found = [r for r in ranks if r]
    return {
        "questions": n,
        "first": sum(1 for r in found if r == 1),
        "top3": sum(1 for r in found if r <= 3),
        "top5": sum(1 for r in found if r <= 5),
        "mrr": round(sum(1.0 / r for r in found) / n, 3) if n else 0.0,
    }


def parse_trial(pairs):
    """{'phrase_boost': 0.0, ...} from ['phrase_boost=0', ...]; raises ValueError on anything else."""
    allowed = ("concept_boost", "phrase_boost", "max_per_page", "unique_pages")
    out = {}
    for pair in pairs or []:
        name, eq, value = str(pair).partition("=")
        if not eq or name.strip() not in allowed:
            raise ValueError(f"--try takes NAME=VALUE with NAME one of {', '.join(allowed)}; got {pair!r}")
        out[name.strip()] = float(value)
    return out


def search(url, key, question, limit=5, timeout=60, ranking=None):
    """(urls, note): the result URLs in order, and a short note when the search did not complete."""
    body = {"query": question, "limit": limit}
    if ranking:
        body["ranking"] = ranking
    try:
        request = urllib.request.Request(normalize_url(url) + "/api/search", data=json.dumps(body).encode("utf-8"),
                                         headers={"content-type": "application/json", "x-functions-key": key or ""}, method="POST")
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            payload = json.loads(reply.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return [], f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:120]}"
    except Exception as exc:
        return [], f"unreachable: {exc.__class__.__name__}: {exc}"[:140]
    note = "" if payload.get("status") == "completed" else str(payload.get("status") or payload.get("error") or "no status")
    return [r.get("url") for r in payload.get("results") or []], note


def explain(url, key, question, expect, timeout=120, ranking=None):
    """The app's explanation of how the local index ranks a question, or {'error': ...}."""
    try:
        body = {"query": question, "expect": list(expect or [])}
        if ranking:
            body["ranking"] = ranking
        request = urllib.request.Request(normalize_url(url) + "/api/search/explain", data=json.dumps(body).encode("utf-8"),
                                         headers={"content-type": "application/json", "x-functions-key": key or ""}, method="POST")
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            return json.loads(reply.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"error": f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:160]}"}
    except Exception as exc:
        return {"error": f"{exc.__class__.__name__}: {exc}"[:160]}


def explain_lines(e):
    """The explanation as a few lines of text."""
    if e.get("error") or e.get("status"):
        return [f"    could not explain: {e.get('error') or e.get('status')}"]
    place = lambda v: "-" if v is None else str(v)
    cut = lambda s, n=40: (str(s or "")[: n - 3] + "...") if len(str(s or "")) > n else str(s or "")

    def chunk(c, deep=False):
        text = f'"{cut(c.get("title"))}" <{short(c.get("url"), 46)}>  keyword {place(c.get("keyword_rank"))}'
        if deep and c.get("keyword_rank") is None and c.get("keyword_rank_all"):
            text += f' (place {c["keyword_rank_all"]} of all that match, too deep to count)'
        text += f', vector {place(c.get("vector_rank"))}'
        if deep and c.get("vector_rank") is None and c.get("vector_rank_all"):
            text += f' (place {c["vector_rank_all"]} of all, outside the nearest that count)'
        more = c.get("other_chunks") or 0
        text += f" (+{more} more chunk{'s' if more != 1 else ''} of this page, behind it)" if more else ""
        return text + (f', concept share {c.get("concept_share", 0):.2f}, word pairs {c.get("phrase_share", 0):.2f}'
                       + ("" if c.get("phrase_counted", True) or not c.get("phrase_share") else " (not counted: too far down)")
                       + f', score {place(c.get("score"))}')

    own = ", ".join(f"{k} {v:g}" for k, v in (e.get("question_concepts") or {}).items()) or "none"
    related = [m["concept"] for m in e.get("concept_matches") or [] if m.get("source") == "relationship"]
    lines = [f"    the question's own concepts: {own}" + (f"   added by relationship: {', '.join(related)}" if related else ""),
             f"    {e.get('passing_filters', 0):,} chunks pass the filters ({len(e.get('filter_concepts') or [])} concepts, "
             f"{len(e.get('filter_domains') or [])} domains); {e.get('candidates', 0):,} entered the ranking"]
    s = e.get("settings") or {}
    if s:
        lines.append(f"    ranking: concept boost {s.get('concept_boost', 0):g}, phrase boost {s.get('phrase_boost', 0):g}, "
                     + ("each page once" if s.get("unique_pages") else f"at most {s.get('max_per_page', 0)} chunks per page")
                     + f", rescoring {'on' if s.get('rescoring') else 'OFF'}"
                     + ("" if s.get("text_in_memory", True) else ", text not in memory (no phrase bonus)"))
    pairs = e.get("phrases") or []
    if pairs:
        lines.append("    word pairs it looks for side by side: " + ", ".join(f'"{p["phrase"]}" {p["weight"]:g}' for p in pairs[:5]))
    if e.get("note"):
        lines.append(f"    {e['note']}")
    x = e.get("expected") or {}
    if not x.get("chunks_in_index"):
        lines.append(f"    the expected page is NOT IN THE INDEX: no chunk's address contains {x.get('pieces')}")
    elif not x.get("chunks_passing_filters"):
        lines.append(f"    the expected page is in the index ({x['chunks_in_index']} chunks) but THE FILTERS LEAVE IT OUT:")
        for c in (x.get("filtered_out") or [])[:2]:
            lines.append(f'      <{short(c.get("url"), 46)}> tags [{", ".join(c.get("tags") or [])}]: {c.get("why")}')
    else:
        pages = x.get("pages_in_index")
        what = (f"the expected page{'s' if pages and pages > 1 else ''}: " + (f"{pages} page{'s' if pages != 1 else ''}, " if pages else "")
                + f"{x['chunks_in_index']} chunks in the index, {x['chunks_passing_filters']} pass the filters;")
        lines.append(f"    {what} " + ("where each stands among the answer's pages:" if s.get("unique_pages") else "its best chunks:"))
        for c in (x.get("best") or [])[:3]:
            lines.append(f"      place {place(c.get('position'))}: " + chunk(c, deep=True))
    for i, c in enumerate((e.get("answer") or [])[:2], 1):
        lines.append(f"    answer {i}: " + chunk(c))
    return lines


def evaluate(gold, ask):
    """[{question, rank, top, note}] using ask(question) -> (urls, note)."""
    rows = []
    for item in gold:
        urls, note = ask(item["question"])
        rows.append({"question": item["question"], "rank": rank_of(item.get("expect") or [], urls),
                     "top": urls[0] if urls else None, "note": note})
    return rows


def short(url, width=58):
    text = str(url or "-").replace("https://", "").replace("http://", "").replace("www.", "", 1)
    return text if len(text) <= width else text[: width - 3] + "..."


def main(argv=None):
    ap = argparse.ArgumentParser(description="Score the search on questions whose right answer is known.")
    ap.add_argument("--url", default=os.getenv("WEBSEARCH_URL", "") or DEFAULT_URL)
    ap.add_argument("--key", default=os.getenv("WEBSEARCH_KEY", ""))
    ap.add_argument("--gold", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "relevance_gold.json"))
    ap.add_argument("--explain", action="store_true",
                    help="for each question not answered first, show where the right page stands and why")
    ap.add_argument("--try", dest="trial", nargs="+", metavar="NAME=VALUE",
                    help="also run the app with these ranking settings for this run only, for example "
                         "--try phrase_boost=0 concept_boost=2 (names: concept_boost, phrase_boost, max_per_page, unique_pages)")
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args(argv)
    args.url = normalize_url(args.url)
    if not args.key:
        if sys.stdin is not None and sys.stdin.isatty():
            args.key = getpass.getpass("Function key for the websearch app (hidden): ")
        else:
            print("No Function key: pass --key, or set WEBSEARCH_KEY.")
            return 2
    with open(args.gold, encoding="utf-8") as handle:
        gold = json.load(handle)
    print(f"Searching {args.url}")
    first_urls, first_note = search(args.url, args.key, gold[0]["question"], args.limit)
    if first_note.startswith(("HTTP 401", "HTTP 403")):
        print(f"The app rejected the key ({first_note}). Check the Function key and try again.")
        return 2
    if first_note.startswith("unreachable:"):
        print(f"Could not reach the app: {first_note[len('unreachable:'):].strip()}\nCheck the address ({args.url}) and that the app is running.")
        return 2
    try:
        trial = parse_trial(args.trial)
    except ValueError as exc:
        print(exc)
        return 2
    runs = {"live": evaluate(gold, lambda q: search(args.url, args.key, q, args.limit))}
    if trial:
        runs["try"] = evaluate(gold, lambda q: search(args.url, args.key, q, args.limit, ranking=trial))
    names = list(runs)
    place = lambda r: str(r) if r else "-"
    print(f"{len(gold)} questions from {args.gold}\n")
    header = f"{'Question':46}" + "".join(f" {name:>6}" for name in names) + "  First result" + (f" ({names[-1]})" if len(names) > 1 else "")
    print(header)
    for i, item in enumerate(gold):
        cells = "".join(f" {place(runs[name][i]['rank']):>6}" for name in names)
        last = runs[names[-1]][i]
        note = f"   [{last['note']}]" if last["note"] else ""
        question = item["question"] if len(item["question"]) <= 46 else item["question"][:43] + "..."
        print(f"{question:46}{cells}  {short(last['top'])}{note}")
    print()
    for name in names:
        s = summarize([row["rank"] for row in runs[name]])
        print(f"{name:>6}: right page first {s['first']}/{s['questions']}   in the top 3 {s['top3']}/{s['questions']}   "
              f"in the top 5 {s['top5']}/{s['questions']}   mean reciprocal rank {s['mrr']:.3f}")
    if trial:
        print("   try = the app with " + ", ".join(f"{k}={v:g}" for k, v in trial.items()) + " for this run only")
    print()
    for first, second in zip(names, names[1:]):
        a, b = runs[first], runs[second]
        better = sum(1 for x, y in zip(a, b) if (y["rank"] or 99) < (x["rank"] or 99))
        worse = sum(1 for x, y in zip(a, b) if (y["rank"] or 99) > (x["rank"] or 99))
        print(f"{second} ranks the right page higher than {first} on {better} question(s), lower on {worse}, the same on {len(gold) - better - worse}.")
    if args.explain:
        local_name = "try" if "try" in runs else "live"
        misses = [(item, row) for item, row in zip(gold, runs[local_name]) if row["rank"] != 1]
        print(f"\nWhy the right page is not first ({len(misses)} question(s)):" if misses
              else "\nThe right page is first on every question; nothing to explain.")
        for item, row in misses:
            print(f"\n  {item['question']}   (right page at place {place(row['rank'])})")
            for line in explain_lines(explain(args.url, args.key, item["question"], item.get("expect"),
                                              ranking=trial if local_name == "try" else None)):
                print(line)
    failed = [row for name in names for row in runs[name] if row["note"]]
    if failed:
        print(f"\n{len(failed)} search(es) did not complete normally; their notes are in brackets above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
