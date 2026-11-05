"""Memory guards for the crawler: capped downloads, streamed sitemap parsing, chunk grouping.

Measured on a laptop, parsing HTML with BeautifulSoup's html.parser takes about 35 times the page
size in memory (a 5 MB page peaks near 200 MB), and parsing a 10 MB sitemap into a tree takes about
60 MB. These helpers keep one page or sitemap from pushing a small instance over its limit.
"""
import io
import xml.etree.ElementTree as ET


class TooLarge(Exception):
    """A response body went over its byte cap."""


def declared_length(response):
    """The Content-Length the server announced, or None when it sent none."""
    try:
        return int(response.headers.get("Content-Length", ""))
    except (TypeError, ValueError):
        return None


def read_capped(response, cap, chunk_size=65536):
    """Read a streamed response body and raise TooLarge as soon as it passes cap bytes.

    A Content-Length over the cap is refused before any of the body is read. Returns a bytearray.
    """
    declared = declared_length(response)
    if declared is not None and declared > cap:
        response.close()
        raise TooLarge(f"declared {declared} bytes, cap {cap}")
    raw = bytearray()
    for block in response.iter_content(chunk_size=chunk_size):
        if not block:
            continue
        raw.extend(block)
        if len(raw) > cap:
            response.close()
            raise TooLarge(f"body passed {cap} bytes")
    return raw


def iter_sitemap_locs(data):
    """Yield (root_kind, loc_text) for every <loc> in a sitemap or sitemap index.

    Parses as a stream and discards each entry once it is read, so memory stays flat however many
    URLs the file lists. root_kind is the root tag without its namespace ("urlset" or "sitemapindex").
    """
    root = None
    root_kind = ""
    for event, element in ET.iterparse(io.BytesIO(data), events=("start", "end")):
        if event == "start":
            if root is None:
                root = element
                root_kind = element.tag.lower().split("}")[-1]
            continue
        tag = element.tag.lower()
        if tag.endswith("loc") and element.text:
            yield root_kind, element.text.strip()
        if tag.endswith(("url", "sitemap")):
            root.clear()


def chunk_groups(text, chunk_chars, max_chunks=0, group_size=50):
    """Yield lists of (index, chunk_text), group_size chunks at a time.

    Stops after max_chunks chunks (0 means no limit). Groups let the caller embed and store a few
    chunks, release them, and move on, instead of holding every chunk and vector of a long page.
    """
    group = []
    for index, start in enumerate(range(0, len(text), chunk_chars)):
        if max_chunks and index >= max_chunks:
            break
        group.append((index, text[start:start + chunk_chars]))
        if len(group) >= group_size:
            yield group
            group = []
    if group:
        yield group
