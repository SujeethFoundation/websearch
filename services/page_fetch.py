"""Fetch one page for POST /api/index/page, the way the crawler does.

Same rules as the crawl: the crawler's own user agent, robots.txt respected, public hosts only,
redirects stay on the site, size capped. A site that refuses the crawler (403, 429, a bot check)
is reported, not worked around; supply the page text instead.
"""
import ipaddress
import socket
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

from services.config import settings
from services.page_extract import extract_html
from services.urlutil import normalize_url, same_site

BOT_CHECK_MARKERS = ("verify you are human", "access denied", "captcha", "enable javascript and cookies")


class PageFetchError(Exception):
    """The page could not be crawled. The message says what to do next."""


def _public(host):
    try:
        return all(ipaddress.ip_address(i[4][0]).is_global for i in socket.getaddrinfo(host, None))
    except Exception:
        return False


def fetch_page(url, session=None, is_public=_public):
    session = session or requests.Session()
    session.headers.update({"User-Agent": settings.crawler_user_agent})
    host = (urlparse(url).hostname or "").lower()
    if not is_public(host):
        raise PageFetchError(f"{host} does not resolve to a public address.")
    robots = RobotFileParser()
    try:
        r = session.get(f"https://{urlparse(url).netloc}/robots.txt", timeout=settings.timeout, allow_redirects=False)
    except requests.RequestException as exc:
        raise PageFetchError(f"robots.txt could not be checked ({exc}). Send content instead.") from exc
    if r.status_code in (401, 403):
        raise PageFetchError("robots.txt denies the crawler. Send content instead.")
    if r.status_code not in (200, 404):
        raise PageFetchError(f"robots.txt returned HTTP {r.status_code}. Try again, or send content instead.")
    robots.parse(r.text.splitlines() if r.status_code == 200 else [])
    if not robots.can_fetch(settings.crawler_user_agent, url):
        raise PageFetchError("robots.txt disallows this page for the crawler. Send content instead.")

    current = url
    for _ in range(6):
        if not same_site(current, host) or not is_public(urlparse(current).hostname or ""):
            raise PageFetchError("The page redirects off the site or to a non-public address.")
        resp = session.get(current, timeout=settings.timeout, allow_redirects=False, stream=True)
        if resp.status_code in (301, 302, 303, 307, 308):
            resp.close()
            location = resp.headers.get("Location")
            if not location:
                raise PageFetchError("The page redirected without a destination.")
            current = normalize_url(urljoin(current, location))
            continue
        break
    else:
        raise PageFetchError("Too many redirects.")
    if resp.status_code >= 400:
        resp.close()
        raise PageFetchError(f"The site returned HTTP {resp.status_code} to the crawler. If it blocks automated "
                             "access, copy the page text and send it as content.")
    ctype = resp.headers.get("content-type", "").lower()
    if "text/html" not in ctype and "application/xhtml" not in ctype:
        resp.close()
        raise PageFetchError(f"Only HTML pages can be crawled here (got {ctype or 'unknown type'}).")
    raw = bytearray()
    for block in resp.iter_content(chunk_size=65536):
        raw.extend(block)
        if len(raw) > settings.max_page_bytes:
            resp.close()
            raise PageFetchError("The page is larger than the crawler's size limit.")
    page = extract_html(bytes(raw).decode(resp.encoding or "utf-8", errors="replace"))
    lowered = page["text"][:2000].lower()
    if len(page["text"]) < 1500 and any(m in lowered for m in BOT_CHECK_MARKERS):
        raise PageFetchError("The site returned a bot-check page. Copy the page text and send it as content.")
    page["final_url"] = current
    return page
