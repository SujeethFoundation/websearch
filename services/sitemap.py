"""Collect page URLs from a domain's sitemaps.

Sitemaps are streamed (see services/limits.py), never built into a tree, and every request stays on the
approved site and on public addresses.
"""
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse

import requests

from services.config import settings
from services.limits import TooLarge, iter_sitemap_locs, read_capped
from services.urlutil import host_variants, is_public_host, normalize_url, same_site

REDIRECTS = (301, 302, 303, 307, 308)


def collect_sitemap_urls(session, domain, seeds, keep=None, sitemap_filter=None, is_public=is_public_host):
    """Page URLs from the domain's sitemaps, in sitemap order, without duplicates.

    seeds are the Sitemap: lines from robots.txt. /sitemap.xml and /sitemap_index.xml on each approved host
    are tried after them. keep, if given, is called with each page URL and must return True to keep it. It
    runs before a URL counts toward the limit (the domain's page limit), so a domain with an allow-list
    spends the cap on the sections it wants. sitemap_filter, if given, is called with each nested sitemap
    file and must return True to open it, so a domain can skip huge sections such as job pages. The top-level
    sitemaps are always opened.

    Returns (urls, sitemaps_opened, failures).
    """
    candidates = list(seeds)
    for host in sorted(host_variants(domain)):
        candidates.extend([f"https://{host}/sitemap.xml", f"https://{host}/sitemap_index.xml"])
    visited, found, seen, failures = set(), [], set(), []
    limit = settings.page_limit_for(domain)
    while candidates and len(found) < limit and len(visited) < settings.max_sitemaps:
        sitemap_url = normalize_url(candidates.pop(0))
        if sitemap_url in visited:
            continue
        visited.add(sitemap_url)
        if not same_site(sitemap_url, domain) or not is_public(urlparse(sitemap_url).hostname or ""):
            continue
        try:
            response = session.get(sitemap_url, timeout=settings.timeout, allow_redirects=False, stream=True)
            if response.status_code in REDIRECTS:
                response.close()
                target = normalize_url(urljoin(sitemap_url, response.headers.get("Location", "")))
                if target and same_site(target, domain) and is_public(urlparse(target).hostname or ""):
                    candidates.append(target)
                else:
                    failures.append({"url": sitemap_url, "error": "off_site_sitemap_redirect"})
                continue
            if response.status_code >= 400:
                response.close()
                failures.append({"url": sitemap_url, "status_code": response.status_code})
                continue
            try:
                data = read_capped(response, settings.max_sitemap_bytes)
            except TooLarge:
                failures.append({"url": sitemap_url, "error": "sitemap_too_large"})
                continue
            # Stream the XML instead of building a tree: a 10 MB sitemap costs about 60 MB as a tree.
            for root_kind, loc in iter_sitemap_locs(data):
                target = normalize_url(loc)
                if not same_site(target, domain):
                    continue
                if root_kind == "sitemapindex" or urlparse(target).path.lower().endswith(".xml"):
                    if sitemap_filter is not None and not sitemap_filter(target):
                        continue
                    if target not in visited:
                        candidates.append(target)
                else:
                    if target in seen or (keep is not None and not keep(target)):
                        continue
                    seen.add(target)
                    found.append(target)
                    if len(found) >= limit:
                        break
            del data
        except (requests.RequestException, ET.ParseError, ValueError) as exc:
            failures.append({"url": sitemap_url, "error": str(exc)[:200]})
    return found, len(visited), failures
