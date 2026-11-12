"""Score the search on questions whose right answer you know.

    python tests/relevance_eval.py                    the engine that is live (SEARCH_BACKEND)
    python tests/relevance_eval.py --backend local    one engine, whatever is live
    python tests/relevance_eval.py --compare          Azure AI Search and the local index side by side

Reads WEBSEARCH_URL and WEBSEARCH_KEY from the environment (the same ones crawl-helper.ps1 uses), or --url and --key.

The questions are in tests/relevance_gold.json: each has a "question" and "expect", a list of pieces of URL. A search
answers a question when one of its results has a URL containing one of those pieces (case does not matter); the place of
the first such result is its rank. The file ships with a few questions as a start: replace them with questions your
students and mentors really ask, and the page each should lead to. Thirty to fifty make the numbers worth reading.

Reports, per engine: how many questions had the right page first, in the top three, in the top five, and the mean
reciprocal rank (1 for first place, 1/2 for second, ... 0 when missing; higher is better).
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request


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


def search(url, key, question, backend=None, limit=5, timeout=60):
    """(urls, note): the result URLs in order, and a short note when the search did not complete."""
    body = {"query": question, "limit": limit}
    if backend:
        body["backend"] = backend
    request = urllib.request.Request(url.rstrip("/") + "/api/search", data=json.dumps(body).encode("utf-8"),
                                     headers={"content-type": "application/json", "x-functions-key": key or ""}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            payload = json.loads(reply.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return [], f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:120]}"
    except Exception as exc:
        return [], f"{exc.__class__.__name__}: {exc}"[:140]
    note = "" if payload.get("status") == "completed" else str(payload.get("status") or payload.get("error") or "no status")
    if backend and payload.get("results") and backend == "local" and payload.get("backend") != "local":
        note = (note + " answered by Azure, not local").strip()
    return [r.get("url") for r in payload.get("results") or []], note


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
    ap.add_argument("--url", default=os.getenv("WEBSEARCH_URL", ""))
    ap.add_argument("--key", default=os.getenv("WEBSEARCH_KEY", ""))
    ap.add_argument("--gold", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "relevance_gold.json"))
    ap.add_argument("--backend", choices=["local", "azure"], help="answer from this engine whatever SEARCH_BACKEND is")
    ap.add_argument("--compare", action="store_true", help="run both engines and show them side by side")
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args(argv)
    if not args.url:
        print("Set WEBSEARCH_URL (and WEBSEARCH_KEY), or pass --url and --key.")
        return 2
    with open(args.gold, encoding="utf-8") as handle:
        gold = json.load(handle)
    engines = ["azure", "local"] if args.compare else [args.backend]
    runs = {e or "live": evaluate(gold, lambda q, e=e: search(args.url, args.key, q, e, args.limit)) for e in engines}
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
    if len(names) == 2:
        a, b = runs[names[0]], runs[names[1]]
        better = sum(1 for x, y in zip(a, b) if (y["rank"] or 99) < (x["rank"] or 99))
        worse = sum(1 for x, y in zip(a, b) if (y["rank"] or 99) > (x["rank"] or 99))
        print(f"\n{names[1]} ranks the right page higher than {names[0]} on {better} question(s), lower on {worse}, the same on {len(gold) - better - worse}.")
    failed = [row for name in names for row in runs[name] if row["note"]]
    if failed:
        print(f"\n{len(failed)} search(es) did not complete normally; their notes are in brackets above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
