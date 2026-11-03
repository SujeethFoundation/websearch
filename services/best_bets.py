"""Best bets: pages added by hand (domains marked manual_only in the ontology) that match a query.

They are searched separately from normal results, de-duplicated by URL, flagged best_bet=True
and placed first. Normal results keep their own limit, so a query can return up to
max_best + limit links.
"""


def best_bet_domains(search_manifest, selected_domains):
    """Selected domains that the ontology marks manual_only."""
    selected = {str(x).lower() for x in (selected_domains or [])}
    rows = (search_manifest or {}).get("search_domains", []) or []
    return sorted(
        str(row.get("domain") or "").lower()
        for row in rows
        if isinstance(row, dict) and row.get("manual_only") is True
        and str(row.get("domain") or "").lower() in selected
    )


def _identity(item):
    return str(item.get("url") or "").strip().lower()


def merge_best_bets(results, manual_results, max_best=3):
    """Return best bets first (one per URL, at most max_best), then the normal results.

    A URL that is a best bet is removed from the normal results so it appears once.
    """
    best, seen = [], set()
    for item in manual_results or []:
        key = _identity(item)
        if not key or key in seen:
            continue
        if len(best) >= max_best:
            break
        seen.add(key)
        best.append({**item, "best_bet": True})
    rest = [{**item, "best_bet": False} for item in (results or []) if _identity(item) not in seen]
    return best + rest
