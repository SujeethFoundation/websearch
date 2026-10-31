
import hashlib
import re
import time
from collections import deque
from urllib.parse import urlparse, urljoin, urldefrag
import requests
from bs4 import BeautifulSoup
from services.config import settings


class Crawler:
    def __init__(self, ontology, storage, search):
        self.ontology = ontology
        self.storage = storage
        self.search = search
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "SujeethFoundationKnowledgeCrawler/0.2 (+contact site owner)"

    def refresh(self, domains):
        results = []
        for item in domains:
            domain = item.get("domain") if isinstance(item, dict) else str(item)
            configured_cap = item.get("max_pages", settings.max_pages) if isinstance(item, dict) else settings.max_pages
            cap = min(int(configured_cap), settings.max_pages)
            results.append(self.crawl_domain(domain, cap))
        return {"status": "completed", "domains": results}

    def crawl_domain(self, domain, cap):
        domain = domain.lower().strip()
        start_url = "https://" + domain + "/"
        try:
            robots = self.session.get("https://" + domain + "/robots.txt", timeout=settings.timeout)
            if robots.status_code == 200:
                rules = [line.strip().lower() for line in robots.text.splitlines()]
                if "disallow: /" in rules:
                    return {"domain": domain, "status": "blocked_by_robots_txt", "pages": 0}
        except requests.RequestException:
            return {"domain": domain, "status": "robots_unavailable", "pages": 0}

        queue = deque([start_url])
        seen = set()
        pages = errors = 0
        while queue and pages < cap:
            url = urldefrag(queue.popleft())[0]
            if url in seen:
                continue
            seen.add(url)
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.hostname != domain:
                continue
            try:
                response = self.session.get(url, timeout=settings.timeout)
                response.raise_for_status()
                if "text/html" not in response.headers.get("content-type", "").lower():
                    continue
                if len(response.content) > 3_000_000:
                    continue
                soup = BeautifulSoup(response.text, "html.parser")
                for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
                    tag.decompose()
                title = soup.title.get_text(" ", strip=True) if soup.title else url
                text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
                if len(text) >= 80:
                    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                    page_id = hashlib.sha256(url.encode("utf-8")).hexdigest()
                    for index, start in enumerate(range(0, len(text), 1800)):
                        self.search.upsert({
                            "id": f"{page_id}-{index}",
                            "title": title,
                            "content": text[start:start + 1800],
                            "url": url,
                            "domain": domain,
                            "concepts": [],
                            "content_hash": digest,
                        })
                    pages += 1

                for anchor in soup.find_all("a", href=True):
                    next_url = urlparse(urljoin(url, anchor["href"]))
                    if next_url.scheme == "https" and next_url.hostname == domain:
                        queue.append(urldefrag(next_url.geturl())[0])
                time.sleep(0.25)
            except requests.RequestException:
                errors += 1

        return {"domain": domain, "status": "completed", "pages": pages, "errors": errors}
