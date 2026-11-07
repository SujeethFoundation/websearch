"""Path rules for crawling: an optional allow-list and an optional block-list per domain.

A domain in the ontology may set either or both under crawl_policy:

    "include_paths": ["/career-advice"]     only these sections are crawled (allow-list)
    "exclude_paths": ["/job", "/jobs"]      these sections are never crawled (block-list)

A rule matches that path and everything beneath it, compared on whole path segments: "/job"
matches /job and /job/anything, but not /jobs or /jobsite. Matching ignores letter case, a
trailing slash, and percent-encoding.

A "*" inside a rule matches any characters within one path segment (never a "/"). For example
"/career-paths/*-certifications" matches /career-paths/snowflake-developer-certifications and
anything beneath it, but not /career-paths/certifications-guide.

A domain may also set "include_sitemaps", an allow-list of child sitemap files. When a sitemap index lists
many sitemaps (jobs, companies, articles), only the ones whose URL path matches a rule are opened:

    "include_sitemaps": ["/sitemaps/advice.xml"]

Rules use the same matching as the two lists above. The top-level sitemap (robots.txt "Sitemap:" lines,
/sitemap.xml) is always opened; only the sitemap files it lists are filtered. Without the key, nothing is filtered.

How the two lists combine:
  - With no include_paths, every path is allowed except those matching exclude_paths (the old behaviour).
  - With include_paths, only paths matching one of them are allowed.
  - exclude_paths always wins: a path matching both lists is not crawled. That lets you allow
    "/career-advice" and still block "/career-advice/news".
  - The bare homepage (the domain root with no query string) is always allowed, even with
    include_paths, because the crawl starts there to find sitemaps and links. Its query-string
    variants (for example /?from=nav) are not.
  - A bare "/" is never a rule, in either list: it would mean "the whole site", so it is ignored.
"""
import functools
import logging
import re
import threading
import time
from urllib.parse import unquote, urlparse

log = logging.getLogger("sujeeth-web-search.path-rules")


class PathRulesUnavailable(RuntimeError):
    """The crawl path rules have never been read from the ontology.

    Answering "is this path allowed?" without them would mean crawling with no rules at all, so callers
    get this error instead. In the page worker it makes the queue message retry until the ontology answers.
    """


def normalize_rule(rule):
    text = "/" + str(rule or "").strip().lower().strip("/")
    # A bare "/" would exclude (or include) the whole site, so it is treated as no rule at all.
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
    """True if the path matches any rule. (Used for both lists: it simply means 'matches'.)"""
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


def is_outside_includes(url, includes):
    """True if an allow-list exists and the URL is not inside it. With no allow-list, never."""
    if not includes:
        return False
    parsed = urlparse(str(url or ""))
    if not unquote(parsed.path or "/").strip("/") and not parsed.query:
        return False  # the bare homepage: the crawl starts here
    return not is_excluded_path(parsed.path, includes)


def is_blocked_url(url, includes=(), excludes=()):
    """The combined decision: blocked by the block-list, or outside the allow-list."""
    return is_excluded_url(url, excludes) or is_outside_includes(url, includes)


class PathPolicy:
    """Reads each domain's include_paths and exclude_paths from the ontology crawl manifest.

    The rules are cached for a few minutes. If the ontology cannot be read, or answers with a manifest that
    lists no crawl domains, the previous rules stay in force and the read is retried after a minute. If the
    rules have never been read, every lookup raises PathRulesUnavailable (fail closed) instead of allowing
    every path.
    """

    def __init__(self, ontology, ttl_seconds=600):
        self.ontology = ontology
        self.ttl = ttl_seconds
        self._rules = {}
        self._includes = {}
        self._sitemaps = {}
        self._ever_loaded = False
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
                rows = manifest.get("crawl_domains") if isinstance(manifest, dict) else None
                if not isinstance(rows, list) or not rows:
                    # An answer with no domains is a broken ontology, not "no rules": do not replace good rules with it.
                    raise ValueError("the ontology crawl manifest lists no crawl domains")
                rules, includes, sitemaps = {}, {}, {}
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    policy = row.get("crawl_policy") if isinstance(row.get("crawl_policy"), dict) else {}
                    domain = str(row.get("domain") or "").lower()
                    found = normalize_rules(policy.get("exclude_paths"))
                    if found:
                        rules[domain] = found
                    allowed = normalize_rules(policy.get("include_paths"))
                    if allowed:
                        includes[domain] = allowed
                    child_sitemaps = normalize_rules(policy.get("include_sitemaps"))
                    if child_sitemaps:
                        sitemaps[domain] = child_sitemaps
                self._rules = rules
                self._includes = includes
                self._sitemaps = sitemaps
                self._ever_loaded = True
                self._loaded_at = now
            except Exception:
                log.exception("Could not read crawl path rules from the ontology; keeping the previous rules")
                self._loaded_at = now - self.ttl + 60

    def _require(self):
        """Refresh the rules if due, and refuse to go on if they have never been read."""
        self._refresh()
        if not self._ever_loaded:
            raise PathRulesUnavailable(
                "Crawl path rules could not be read from the ontology; refusing to crawl without them.")

    def rules_for(self, domain):
        """The block-list (exclude_paths) for a domain."""
        self._require()
        return self._rules.get(str(domain or "").lower(), ())

    def includes_for(self, domain):
        """The allow-list (include_paths) for a domain; empty means no allow-list."""
        self._require()
        return self._includes.get(str(domain or "").lower(), ())

    def sitemaps_for(self, domain):
        """The allow-list of child sitemap files (include_sitemaps); empty means open every sitemap."""
        self._require()
        return self._sitemaps.get(str(domain or "").lower(), ())

    def is_sitemap_allowed(self, domain, url):
        """True if the crawler may open this child sitemap file. With no include_sitemaps, always True."""
        rules = self.sitemaps_for(domain)
        if not rules:
            return True
        return is_excluded_path(urlparse(str(url or "")).path, rules)

    def all_rules(self):
        self._require()
        return dict(self._rules)

    def all_includes(self):
        self._require()
        return dict(self._includes)

    def is_excluded(self, domain, url):
        """True if the crawler must not fetch this URL: blocked, or outside the allow-list."""
        key = str(domain or "").lower()
        self._require()
        return is_blocked_url(url, self._includes.get(key, ()), self._rules.get(key, ()))
