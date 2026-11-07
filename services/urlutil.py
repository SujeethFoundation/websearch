"""URL rules shared by the page worker, sitemap collection and manual page fetch.

An apex domain and its www host count as one approved site. Only https URLs without credentials qualify.
"""
import ipaddress
import re
import socket
from urllib.parse import urlparse, urlunparse


def canonical_host(domain):
    """The host without a leading www."""
    host = (urlparse(domain).hostname or domain).lower().strip(".")
    return host[4:] if host.startswith("www.") else host


def host_variants(domain):
    """The two hosts approved for a domain: apex and www."""
    apex = canonical_host(domain)
    return {apex, "www." + apex}


def same_site(url, domain):
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname in host_variants(domain)
        and not parsed.username
        and not parsed.password
    )


def normalize_url(url):
    """Lower-case scheme and host, drop the fragment and tracking parameters, collapse repeated slashes."""
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


def is_public_host(host):
    """True only if the host resolves, and every address it resolves to is public."""
    try:
        addresses = socket.getaddrinfo(host, None)
        return bool(addresses) and all(ipaddress.ip_address(item[4][0]).is_global for item in addresses)
    except Exception:
        return False
