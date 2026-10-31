import hashlib
import re
import time
import xml.etree.ElementTree as ET
from collections import deque
from urllib.parse import urlparse, urljoin, urldefrag, urlunparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from services.config import settings


class Crawler:
    """Bounded same-host crawler with sitemap discovery and breadth-first link traversal."""

    def __init__(self, ontology, storage, search):
        self.ontology = ontology
        self.storage = storage
        self.search = search
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "SujeethFoundationKnowledgeCrawler/0.3 (+contact site owner)"
        })

    def refresh(self, domains):
        results = []
        for item in domains:
            domain = item.get("domain") if isinstance(item, dict) else str(item)
            configured_cap = item.get("max_pages", settings.max_pages) if isinstance(item, dict) else settings.max_pages
            configured_depth = item.get("max_depth", settings.max_crawl_depth) if isinstance(item, dict) else settings.max_crawl_depth
            cap = max(1, min(int(configured_cap), settings.max_pages))
            depth = max(0, min(int(configured_depth), settings.max_crawl_depth))
            results.append(self.crawl_domain(domain, cap, depth))
        return {
            "status": "completed",
            "crawler_version": "0.3.0",
            "domains": results,
        }

    @staticmethod
    def _normalize_url(url):
        parsed = urlparse(url)
        # Drop fragments and common tracking parameters; preserve meaningful query strings.
        query_parts = [
            part for part in parsed.query.split("&")
            if part and not part.lower().startswith(("utm_", "fbclid=", "gclid="))
        ]
        normalized = parsed._replace(
            scheme=parsed.scheme.lower(),
            netloc=parsed.netloc.lower(),
            fragment="",
            query="&".join(query_parts),
        )
        path = re.sub(r"/{2,}", "/", normalized.path or "/")
        return urlunparse(normalized._replace(path=path))

    @staticmethod
    def _same_host(url, domain):
        parsed = urlparse(url)
        return parsed.scheme == "https" and parsed.hostname == domain and not parsed.username and not parsed.password

    def _robots(self, base_url):
        robots_url = urljoin(base_url, "/robots.txt")
        response = self.session.get(robots_url, timeout=settings.timeout)
        if response.status_code == 404:
            parser = RobotFileParser()
            parser.parse([])
            return parser, []
        response.raise_for_status()
        parser = RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(response.text.splitlines())
        sitemap_urls = []
        for line in response.text.splitlines():
            if line.strip().lower().startswith("sitemap:"):
                sitemap_urls.append(line.split(":", 1)[1].strip())
        return parser, sitemap_urls

    def _sitemap_urls(self, base_url, sitemap_seeds, domain):
        candidates = list(sitemap_seeds)
        candidates.extend([urljoin(base_url, "/sitemap.xml"), urljoin(base_url, "/sitemap_index.xml")])
        visited_sitemaps = set()
        found = []
        while candidates and len(found) < settings.max_sitemap_urls:
            sitemap_url = candidates.pop(0)
            sitemap_url = urldefrag(sitemap_url)[0]
            if sitemap_url in visited_sitemaps:
                continue
            visited_sitemaps.add(sitemap_url)
            try:
                response = self.session.get(sitemap_url, timeout=settings.timeout)
                if response.status_code >= 400:
                    continue
                if len(response.content) > 10_000_000:
                    continue
                root = ET.fromstring(response.content)
                for loc in root.iter():
                    if loc.tag.lower().endswith("loc") and loc.text:
                        target = self._normalize_url(loc.text.strip())
                        if not self._same_host(target, domain):
                            continue
                        if urlparse(target).path.lower().endswith(".xml"):
                            if target not in visited_sitemaps:
                                candidates.append(target)
                        else:
                            found.append(target)
                            if len(found) >= settings.max_sitemap_urls:
                                break
            except (requests.RequestException, ET.ParseError, ValueError):
                continue
        return found, len(visited_sitemaps)

    def crawl_domain(self, domain, cap, max_depth=None):
        domain = domain.lower().strip()
        if domain.startswith("https://"):
            domain = urlparse(domain).hostname or domain
        if domain.startswith("http://"):
            domain = urlparse(domain).hostname or domain
        max_depth = settings.max_crawl_depth if max_depth is None else max_depth
        base_url = f"https://{domain}/"

        try:
            robots, sitemap_seeds = self._robots(base_url)
        except requests.RequestException as exc:
            return {
                "domain": domain, "status": "robots_unavailable",
                "pages_crawled": 0, "errors": 1, "error_sample": str(exc)[:300],
            }

        sitemap_urls, sitemap_count = self._sitemap_urls(base_url, sitemap_seeds, domain)
        # Breadth-first traversal. Sitemap URLs are queued at depth 1 so they are not
        # treated as direct homepage links, but remain eligible under the depth cap.
        queue = deque([(base_url, 0)])
        queue.extend((url, 1) for url in sitemap_urls)
        queued = {self._normalize_url(base_url), *sitemap_urls}
        visited = set()
        pages_crawled = pages_skipped = errors = links_discovered = 0
        max_depth_reached = 0
        error_samples = []

        while queue and pages_crawled < cap:
            url, depth = queue.popleft()
            url = self._normalize_url(url)
            if url in visited:
                continue
            visited.add(url)
            if not self._same_host(url, domain):
                pages_skipped += 1
                continue
            if depth > max_depth:
                pages_skipped += 1
                continue
            if not robots.can_fetch(self.session.headers["User-Agent"], url):
                pages_skipped += 1
                continue

            try:
                response = self.session.get(url, timeout=settings.timeout, allow_redirects=True)
                response.raise_for_status()
                final_url = self._normalize_url(response.url)
                if not self._same_host(final_url, domain):
                    pages_skipped += 1
                    continue
                content_type = response.headers.get("content-type", "").lower()
                if "text/html" not in content_type or len(response.content) > 3_000_000:
                    pages_skipped += 1
                    continue

                soup = BeautifulSoup(response.text, "html.parser")
                # Discover links before removing navigation/header/footer so the crawler
                # can follow the site's actual information architecture.
                discovered = []
                for anchor in soup.find_all("a", href=True):
                    candidate = self._normalize_url(urljoin(final_url, anchor["href"]))
                    if self._same_host(candidate, domain) and candidate not in queued and candidate not in visited:
                        discovered.append(candidate)
                if depth < max_depth:
                    for candidate in discovered:
                        queued.add(candidate)
                        queue.append((candidate, depth + 1))
                        links_discovered += 1

                title = soup.title.get_text(" ", strip=True) if soup.title else final_url
                for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "svg"]):
                    tag.decompose()
                text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()

                if len(text) < 80:
                    pages_skipped += 1
                else:
                    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                    page_id = hashlib.sha256(final_url.encode("utf-8")).hexdigest()
                    chunk_count = 0
                    for index, start in enumerate(range(0, len(text), 1800)):
                        self.search.upsert({
                            "id": f"{page_id}-{index}",
                            "title": title,
                            "content": text[start:start + 1800],
                            "url": final_url,
                            "domain": domain,
                            "concepts": [],
                            "content_hash": digest,
                            "crawl_depth": depth,
                        })
                        chunk_count += 1
                    pages_crawled += 1
                max_depth_reached = max(max_depth_reached, depth)
            except requests.RequestException as exc:
                errors += 1
                if len(error_samples) < 5:
                    error_samples.append({"url": url, "error": str(exc)[:250]})

            if settings.crawl_delay > 0:
                time.sleep(settings.crawl_delay)

        return {
            "domain": domain,
            "status": "completed",
            "pages_discovered": len(queued),
            "pages_crawled": pages_crawled,
            "pages_skipped": pages_skipped,
            "errors": errors,
            "links_discovered": links_discovered,
            "sitemaps_found": sitemap_count,
            "sitemap_urls_discovered": len(sitemap_urls),
            "max_depth_reached": max_depth_reached,
            "max_depth_configured": max_depth,
            "page_limit": cap,
            "queue_remaining": len(queue),
            "error_samples": error_samples,
        }
