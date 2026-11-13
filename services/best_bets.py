"""Best bets: pages added by hand (domains marked manual_only in the ontology) that match a query.

They are searched separately from normal results, de-duplicated by URL, flagged best_bet=True
and placed first. Normal results keep their own limit, so a query can return up to
max_best + limit links.
"""


from services.local_index import page_key


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
    """One key for a page however it was reached: no scheme, no www., no fragment, no trailing slash, lower case."""
    return page_key(item.get("url")).lower()


def unique_pages(items):
    """The items with each page once: the first of any that share a page (www. and bare addresses are one page)."""
    seen, out = set(), []
    for item in items or []:
        key = _identity(item) or str(item.get("chunk_id") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(item)
    return out


def top_pages(results, count):
    """The page keys of the first `count` distinct pages of the main results (a page's chunks count once)."""
    keys = []
    for item in results or []:
        key = _identity(item)
        if key and key not in keys:
            keys.append(key)
        if len(keys) >= count:
            break
    return set(keys)


def merge_best_bets(results, manual_results, max_best=3, within_rank=0):
    """Return best bets first (one per URL, at most max_best), then the normal results.

    A URL that is a best bet is removed from the normal results so it appears once.

    within_rank turns on the relevance check: a manual page is a best bet only when it is also among the first
    `within_rank` distinct pages of the main results, whose ranking covers every selected domain. The manual pages
    are searched among themselves, so their own scores say only which of them is least unrelated; the main ranking
    says whether the page is a good answer. 0 keeps every manual page that was found. Returns the merged list; the
    number the check dropped is read with dropped_best_bets.
    """
    allowed = top_pages(results, within_rank) if within_rank and within_rank > 0 else None
    best, seen = [], set()
    for item in manual_results or []:
        key = _identity(item)
        if not key or key in seen:
            continue
        if allowed is not None and key not in allowed:
            continue
        if len(best) >= max_best:
            break
        seen.add(key)
        best.append({**item, "best_bet": True})
    rest = [{**item, "best_bet": False} for item in (results or []) if _identity(item) not in seen]
    return best + rest


def dropped_best_bets(results, manual_results, within_rank):
    """How many distinct manual pages the relevance check left out (for the best_bets block of the answer)."""
    if not within_rank or within_rank <= 0:
        return 0
    allowed = top_pages(results, within_rank)
    keys = {_identity(item) for item in manual_results or [] if _identity(item)}
    return len(keys - allowed)
