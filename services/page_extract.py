"""Pull a title, a description and the readable text out of a fetched page.

The description is inferred from the page itself: the page's own summary tags when it has them,
otherwise the opening sentences of its text. A description sent in a request is never used.
"""
import re
from bs4 import BeautifulSoup

DESCRIPTION_MAX = 240
_META_KEYS = (("property", "og:description"), ("name", "description"), ("name", "twitter:description"))
_BOILERPLATE = re.compile(r"^(skip to|menu|home\b|log ?in|sign ?in|cookie|we use cookies|accept all)", re.I)


def _clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _trim(text, limit=DESCRIPTION_MAX):
    """Cut at a sentence end if one falls in the second half of the limit, else at a word."""
    text = _clean(text)
    if len(text) <= limit:
        return text
    window = text[:limit]
    ends = [m.end() for m in re.finditer(r"[.!?](?=\s|$)", window)]
    if ends and ends[-1] >= limit // 2:
        return window[:ends[-1]].strip()
    return window.rsplit(" ", 1)[0].rstrip(",;:- ") + "…"


def infer_description(text, limit=DESCRIPTION_MAX):
    """Describe a page from its opening sentences. Skips menu and cookie fragments."""
    sentences = re.split(r"(?<=[.!?])\s+", _clean(text))
    picked, size = [], 0
    for sentence in sentences:
        if len(sentence) < 40 or _BOILERPLATE.match(sentence):
            continue
        picked.append(sentence)
        size += len(sentence) + 1
        if size >= limit * 0.6:
            break
    return _trim(" ".join(picked), limit) if picked else ""


def extract_html(html):
    """Return {"title", "description", "description_source", "text"} for an HTML page."""
    soup = BeautifulSoup(html, "html.parser")
    title = _clean(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if not title:
        og = soup.find("meta", attrs={"property": "og:title"})
        title = _clean(og.get("content")) if og else ""
    meta = ""
    for attr, key in _META_KEYS:
        tag = soup.find("meta", attrs={attr: key})
        if tag and len(_clean(tag.get("content"))) >= 40:
            meta = _clean(tag.get("content"))
            break
    for tag in soup(["head", "title", "script", "style", "nav", "footer", "header", "noscript", "svg", "form"]):
        tag.decompose()
    for node in soup.select("[role=navigation],.cookie,.cookie-banner,.cookie-consent,.advertisement,.ads"):
        node.decompose()
    text = _clean(soup.get_text(" ", strip=True))
    if meta:
        description, source = _trim(meta), "page_meta"
    else:
        description, source = infer_description(text), "page_text"
    return {"title": title, "description": description, "description_source": source if description else None,
            "text": text}
