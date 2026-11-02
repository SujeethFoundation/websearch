"""Path exclusion rules for crawling.

A domain in the ontology may list crawl_policy.exclude_paths, for example ["/job", "/jobs"].
A rule matches that path and everything beneath it, compared on whole path segments: "/job"
matches /job and /job/anything, but not /jobs or /jobsite. Matching ignores letter case, a
trailing slash, and percent-encoding.

A "*" inside a rule matches any characters within one path segment (never a "/"). For example
"/career-paths/*-certifications" matches /career-paths/snowflake-developer-certifications and
anything beneath it, but not /career-paths/certifications-guide.
"""
import functools
import logging
import re
import threading
import time
from urllib.parse import unquote, urlparse

log = logging.getLogger("sujeeth-web-search.path-rules")


def normalize_rule(rule):
    text = "/" + str(rule or "").strip().lower().strip("/")
    # A bare "/" would exclude the whole site, so it is treated as no rule at all.
    return None if text == "/" else text


def normalize_rules(rules):
    if isinstance(rules, str):
        rules = [rules]
    cleaned = []
    for rule in rules or []:
        item = normalize_rule(rule)
        if item and item not in cleaned:
            cleaned.append(item)
    return tuple(cleaned)


@functools.lru_cache(maxsize=512)
def _wildcard_pattern(rule):
    return re.compile("^" + re.escape(rule).replace(r"\*", "[^/]*") + "(?:/|$)")


def is_excluded_path(path, rules):
    if not rules:
        return False
    trimmed = unquote(path or "/").lower().strip("/")
    candidate = "/" + trimmed if trimmed else "/"
    for rule in rules:
        if "*" in rule:
            if _wildcard_pattern(rule).match(candidate):
                return True
        elif candidate == rule or candidate.startswith(rule + "/"):
            return True
    return False


def is_excluded_url(url, rules):
    return is_excluded_path(urlparse(str(url or "")).path, rules)


class PathPolicy:
    """Reads each domain's exclude_paths from the ontology crawl manifest.

    The rules are cached for a few minutes. If the ontology cannot be read, the previous rules
    stay in force and the read is retried after a minute.
    """

    def __init__(self, ontology, ttl_seconds=600):
        self.ontology = ontology
        self.ttl = ttl_seconds
        self._rules = {}
        self._loaded_at = None
        self._lock = threading.Lock()

    def _refresh(self):
        now = time.monotonic()
        if self._loaded_at is not None and now - self._loaded_at < self.ttl:
            return
        with self._lock:
            now = time.monotonic()
            if self._loaded_at is not None and now - self._loaded_at < self.ttl:
                return
            try:
                manifest = self.ontology.crawl_manifest()
                rules = {}
                for row in manifest.get("crawl_domains", []):
                    if not isinstance(row, dict):
                        continue
                    policy = row.get("crawl_policy") if isinstance(row.get("crawl_policy"), dict) else {}
                    found = normalize_rules(policy.get("exclude_paths"))
                    if found:
                        rules[str(row.get("domain") or "").lower()] = found
                self._rules = rules
                self._loaded_at = now
            except Exception:
                log.exception("Could not read exclude_paths from the ontology; keeping the previous rules")
                self._loaded_at = now - self.ttl + 60

    def rules_for(self, domain):
        self._refresh()
        return self._rules.get(str(domain or "").lower(), ())

    def all_rules(self):
        self._refresh()
        return dict(self._rules)

    def is_excluded(self, domain, url):
        return is_excluded_url(url, self.rules_for(domain))
