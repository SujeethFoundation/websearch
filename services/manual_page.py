"""Build search chunks for one page whose text is supplied by hand instead of crawled.

Used by POST /api/index/page for pages a crawler cannot fetch (bot-blocked or login-gated
sites). The chunks have the same shape and ids the crawler writes, so search, document
lookup, retagging and backfill treat them like any other page.
"""
import hashlib
import re
from datetime import datetime, timezone
from urllib.parse import urlparse


def build_page_documents(*, url, title, text, allowed_concepts, tag_fn, chunk_chars, min_chars,
                         explicit_concepts=None, now=None):
    """Return (documents, domain, content_hash). Raises ValueError for input the caller must fix.

    url must already be normalized. Concepts come from explicit_concepts (which must all be
    allowed for the domain) or, when omitted, from tag_fn applied to each chunk.
    """
    parsed = urlparse(str(url or ""))
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("url must be an absolute https URL")
    title = re.sub(r"\s+", " ", str(title or "")).strip()
    if not title:
        raise ValueError("title is required")
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) < min_chars:
        raise ValueError(f"content must be at least {min_chars} characters")

    allowed = [str(x).strip() for x in (allowed_concepts or []) if str(x).strip()]
    forced = None
    if explicit_concepts is not None:
        forced = [str(x).strip() for x in explicit_concepts if str(x).strip()]
        outside = sorted(set(forced) - set(allowed))
        if outside:
            raise ValueError("concepts not associated with this domain in the ontology: " + ", ".join(outside))

    domain = parsed.hostname.lower()
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    page_id = hashlib.sha256(url.encode()).hexdigest()
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    documents = []
    for index, start in enumerate(range(0, len(text), chunk_chars)):
        chunk = text[start:start + chunk_chars]
        concepts = list(forced) if forced is not None else tag_fn(title, chunk, allowed)
        documents.append({
            "id": f"{page_id}-{index}", "title": title, "content": chunk, "url": url,
            "domain": domain, "concepts": concepts, "content_hash": digest, "page_hash": digest,
            "crawl_depth": 0, "page_type": "manual", "etag": None, "last_modified": None,
            "crawled_utc": stamp,
        })
    return documents, domain, digest
