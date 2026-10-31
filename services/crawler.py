import hashlib
import re
import time
import xml.etree.ElementTree as ET
from collections import deque
from urllib.parse import urlparse, urljoin, urldefrag, urlunparse

import requests
from bs4 import BeautifulSoup
from urllib.robotparser import RobotFileParser

from services.config import settings


class Crawler:
    """Bounded sitemap-first crawler; treats apex and www as one approved site."""

    def __init__(self, ontology, storage, search, concept_resolver=None):
        self.ontology = ontology
        self.storage = storage
        self.search = search
        self.concept_resolver = concept_resolver
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "SujeethFoundationKnowledgeCrawler/0.3.1 (+contact site owner)"
        })

    @staticmethod
    def _host_variants(domain):
        host = (urlparse(domain).hostname or domain).lower().strip(".")
        if host.startswith("www."):
            apex = host[4:]
        else:
            apex = host
        return {apex, "www." + apex}

    @classmethod
    def _same_site(cls, url, domain):
        parsed = urlparse(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname in cls._host_variants(domain)
            and not parsed.username
            and not parsed.password
        )

    @staticmethod
    def _normalize_url(url):
        parsed = urlparse(url)
        query_parts = [
            part for part in parsed.query.split("&")
            if part and not part.lower().startswith(("utm_", "fbclid=", "gclid="))
        ]
        path = re.sub(r"/{2,}", "/", parsed.path or "/")
        return urlunparse(parsed._replace(
            scheme=parsed.scheme.lower(),
            netloc=parsed.netloc.lower(),
            fragment="",
            query="&".join(query_parts),
            path=path,
        ))

    @staticmethod
    def _canonical_host(domain):
        host = (urlparse(domain).hostname or domain).lower().strip(".")
        return host[4:] if host.startswith("www.") else host

    def refresh(self, domains):
        results = []
        for item in domains:
            domain = item.get("domain") if isinstance(item, dict) else str(item)
            configured_cap = item.get("max_pages", settings.max_pages) if isinstance(item, dict) else settings.max_pages
            configured_depth = item.get("max_depth", settings.max_crawl_depth) if isinstance(item, dict) else settings.max_crawl_depth
            cap = max(1, min(int(configured_cap), settings.max_pages))
            depth = max(0, min(int(configured_depth), settings.max_crawl_depth))
            results.append(self.crawl_domain(domain, cap, depth))
        return {"status": "completed", "crawler_version": "0.4.0", "domains": results}

    def _robots(self, domain):
        # Robots policy is evaluated separately for each permitted host variant.
        parsers = {}
        sitemap_seeds = []
        errors = []
        for host in sorted(self._host_variants(domain)):
            robots_url = f"https://{host}/robots.txt"
            parser = RobotFileParser()
            try:
                response = self.session.get(robots_url, timeout=settings.timeout)
                if response.status_code == 404:
                    parser.parse([])
                else:
                    response.raise_for_status()
                    parser.parse(response.text.splitlines())
                    for line in response.text.splitlines():
                        if line.strip().lower().startswith("sitemap:"):
                            sitemap_seeds.append(line.split(":", 1)[1].strip())
                parsers[host] = parser
            except requests.RequestException as exc:
                errors.append({"url": robots_url, "error": str(exc)[:200]})
                # Do not assume permission if robots policy cannot be retrieved.
        return parsers, sitemap_seeds, errors

    def _allowed_by_robots(self, parsers, url):
        host = urlparse(url).hostname
        parser = parsers.get(host)
        if parser is None:
            return False
        return parser.can_fetch(self.session.headers["User-Agent"], url)

    def _sitemap_urls(self, domain, sitemap_seeds):
        canonical = self._canonical_host(domain)
        candidates = list(sitemap_seeds)
        for host in sorted(self._host_variants(domain)):
            candidates.extend([
                f"https://{host}/sitemap.xml",
                f"https://{host}/sitemap_index.xml",
            ])
        visited_sitemaps = set()
        found = []
        metadata = {}
        failures = []
        while candidates and len(found) < settings.max_sitemap_urls:
            sitemap_url = self._normalize_url(candidates.pop(0))
            if sitemap_url in visited_sitemaps:
                continue
            visited_sitemaps.add(sitemap_url)
            if not self._same_site(sitemap_url, domain):
                continue
            try:
                response = self.session.get(sitemap_url, timeout=settings.timeout, allow_redirects=True)
                if response.status_code >= 400:
                    failures.append({"url": sitemap_url, "status_code": response.status_code})
                    continue
                if len(response.content) > 10_000_000:
                    failures.append({"url": sitemap_url, "error": "sitemap_too_large"})
                    continue
                root = ET.fromstring(response.content)
                for element in root.iter():
                    if not element.tag.lower().endswith("loc") or not element.text:
                        continue
                    target = self._normalize_url(element.text.strip())
                    if not self._same_site(target, domain):
                        continue
                    parent_tag = element.getparent().tag if hasattr(element, "getparent") else ""
                    # ElementTree has no parent pointer. Infer sitemap vs URL entry from root.
                    root_kind = root.tag.lower().split("}")[-1]
                    if root_kind == "sitemapindex" or urlparse(target).path.lower().endswith(".xml"):
                        if target not in visited_sitemaps:
                            candidates.append(target)
                    else:
                        found.append(target)
                        metadata[target] = {"sitemap": sitemap_url}
                        if len(found) >= settings.max_sitemap_urls:
                            break
            except (requests.RequestException, ET.ParseError, ValueError) as exc:
                failures.append({"url": sitemap_url, "error": str(exc)[:200]})
        # Stable de-duplication while preserving sitemap order.
        unique = list(dict.fromkeys(found))
        return unique, len(visited_sitemaps), metadata, failures

    def crawl_domain(self, domain, cap, max_depth=None):
        domain = self._canonical_host(domain)
        max_depth = settings.max_crawl_depth if max_depth is None else max_depth
        allowed_concepts = self.ontology.domain_concepts(domain) if self.ontology else []
        start_url = f"https://{domain}/"

        robots, sitemap_seeds, robots_errors = self._robots(domain)
        if not robots:
            return {
                "domain": domain, "status": "robots_unavailable",
                "pages_discovered": 0, "pages_crawled": 0, "pages_skipped": 0,
                "errors": len(robots_errors), "error_samples": robots_errors[:5],
            }

        sitemap_urls, sitemap_count, sitemap_metadata, sitemap_failures = self._sitemap_urls(domain, sitemap_seeds)
        # Sitemap URLs are queued first. Homepage is always included as the root.
        queue = deque([(start_url, 0)])
        queued = {self._normalize_url(start_url)}
        for url in sitemap_urls:
            normalized = self._normalize_url(url)
            if normalized not in queued:
                queue.append((normalized, 1))
                queued.add(normalized)

        visited = set()
        pages_crawled = pages_skipped = errors = links_discovered = 0
        max_depth_reached = 0
        skip_reasons = {}
        error_samples = []
        sitemap_pages_crawled = 0
        chunks_tagged = 0
        chunks_without_concepts = 0

        while queue and pages_crawled < cap:
            url, depth = queue.popleft()
            url = self._normalize_url(url)
            if url in visited:
                continue
            visited.add(url)
            if not self._same_site(url, domain):
                pages_skipped += 1
                skip_reasons["off_site"] = skip_reasons.get("off_site", 0) + 1
                continue
            if depth > max_depth:
                pages_skipped += 1
                skip_reasons["depth_limit"] = skip_reasons.get("depth_limit", 0) + 1
                continue
            if not self._allowed_by_robots(robots, url):
                pages_skipped += 1
                skip_reasons["robots_disallowed_or_unavailable"] = skip_reasons.get("robots_disallowed_or_unavailable", 0) + 1
                continue

            try:
                response = self.session.get(url, timeout=settings.timeout, allow_redirects=True)
                response.raise_for_status()
                final_url = self._normalize_url(response.url)
                if not self._same_site(final_url, domain):
                    pages_skipped += 1
                    skip_reasons["redirected_off_site"] = skip_reasons.get("redirected_off_site", 0) + 1
                    continue
                content_type = response.headers.get("content-type", "").lower()
                if "text/html" not in content_type or len(response.content) > 3_000_000:
                    pages_skipped += 1
                    reason = "non_html" if "text/html" not in content_type else "page_too_large"
                    skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                    continue

                soup = BeautifulSoup(response.text, "html.parser")
                discovered = []
                for anchor in soup.find_all("a", href=True):
                    candidate = self._normalize_url(urljoin(final_url, anchor["href"]))
                    if self._same_site(candidate, domain) and candidate not in queued and candidate not in visited:
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
                    skip_reasons["insufficient_text"] = skip_reasons.get("insufficient_text", 0) + 1
                else:
                    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                    page_id = hashlib.sha256(final_url.encode("utf-8")).hexdigest()
                    for index, start in enumerate(range(0, len(text), 1800)):
                        chunk_text = text[start:start + 1800]
                        chunk_concepts = self.concept_resolver.tag_document(title, chunk_text, allowed_concepts) if self.concept_resolver else []
                        if chunk_concepts:
                            chunks_tagged += 1
                        else:
                            chunks_without_concepts += 1
                        self.search.upsert({
                            "id": f"{page_id}-{index}",
                            "title": title,
                            "content": chunk_text,
                            "url": final_url,
                            "domain": domain,
                            "concepts": chunk_concepts,
                            "content_hash": digest,
                            "crawl_depth": depth,
                            "sitemap_lastmod": sitemap_metadata.get(url, {}).get("lastmod"),
                        })
                    pages_crawled += 1
                    if url in sitemap_metadata:
                        sitemap_pages_crawled += 1
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
            "sitemap_urls_discovered": len(sitemap_urls),
            "sitemap_pages_crawled": sitemap_pages_crawled,
            "chunks_tagged": chunks_tagged,
            "chunks_without_concepts": chunks_without_concepts,
            "domain_allowed_concepts": allowed_concepts,
            "sitemaps_found": sitemap_count,
            "sitemap_failures": sitemap_failures[:10],
            "robots_errors": robots_errors[:5],
            "links_discovered": links_discovered,
            "pages_skipped": pages_skipped,
            "skip_reasons": skip_reasons,
            "errors": errors,
            "error_samples": error_samples,
            "max_depth_reached": max_depth_reached,
            "max_depth_configured": max_depth,
            "page_limit": cap,
            "queue_remaining": len(queue),
        }
