"""Is this the page, or only its frame?

A read can succeed with HTTP 200 and still miss its content: a search page
whose results load later, a listing page that rendered only its header. Core
checks a page's structure, never what the caller asked for, and escalates to a
stronger provider when the structure is missing.

- Search and category pages (a query parameter, or a path such as /search or
  /s/) need at least 10 distinct same-site item links (product, listing or
  detail URLs; never assets or site-wide service pages) or 4 prices. Menus
  carry a handful of such links; result lists carry many.
- Item pages (a path with a long id or a long slug) need a price, or enough text.
- Other pages need enough text.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urljoin, urlparse

QUERY_KEYS = frozenset({"q", "query", "k", "s", "st", "ss", "search", "searchterm", "keyword",
                        "keywords", "text", "term", "searchtext", "find_desc", "d", "field-keywords"})
_SEARCH_PATH = re.compile(r"/(search|s|shop|catalogsearch|browse|category|categories|c|list|"
                          r"jobs|homes|for_sale|sale|buy|rent|pdsearch|keyword\.php|w|p/pl)(/|$|\.|\?)", re.I)
# A marker segment followed by the item itself (/p/<slug>, /rooms/<id>).
_ITEM_MARKER = re.compile(r"/(p|dp|ip|item|itm|items|product|products|listing|listings|rooms|"
                          r"property|properties|job|jobs|viewjob|questions|ad|ads|deal)/[^/]+", re.I)
# Links that are never results: assets, and site-wide service and legal pages.
_NOT_ITEM = re.compile(r"\.(css|js|mjs|json|xml|pdf|png|jpe?g|gif|svg|webp|ico|woff2?|ttf)$|"
                       r"(privacy|policy|policies|terms|conditions|statement|legal|cookie|"
                       r"customer-service|customer-care|help|faq|support|careers|about|ethics|"
                       r"disclosure|accessibility|sitemap|store-locator|stores|gift-card|login|"
                       r"sign-?in|register|account|contact|returns|shipping|delivery)", re.I)
_LONG_ID = re.compile(r"\d{5,}|[A-Z0-9]{8,}")
_PRICE = re.compile(r"(?:A\$|AU\$|US\$|C\$|NZ\$|\$|€|£)\s?\d[\d,]*(?:\.\d{2})?")
_HREF = re.compile(r"""<a\b[^>]*?\bhref\s*=\s*["']([^"'#]+)""", re.I)
_MARKDOWN_LINK = re.compile(r"\]\((https?://[^)\s#]+|/[^)\s#]*)")


def page_kind(url: str) -> str:
    parsed = urlparse(url)
    params = {key.lower() for key in parse_qs(parsed.query)}
    if params & QUERY_KEYS or _SEARCH_PATH.search(parsed.path or "/"):
        return "search"
    if _ITEM_MARKER.search(parsed.path) or any(
            _LONG_ID.search(segment) or segment.count("-") >= 3
            for segment in parsed.path.split("/") if segment):
        return "item"
    return "page"


def _item_like(path: str) -> bool:
    if _NOT_ITEM.search(path):
        return False
    if _ITEM_MARKER.search(path):
        return True
    return any(_LONG_ID.search(segment) or (segment.count("-") >= 3 and len(segment) >= 20)
               for segment in path.split("/") if segment)


def item_links(html: str, base: str) -> int:
    """Distinct same-site links that look like items (products, listings, detail pages)."""
    host = (urlparse(base).hostname or "").removeprefix("www.")
    base_path = urlparse(base).path
    found = set()
    for href in _HREF.findall(html[:4_000_000]) + _MARKDOWN_LINK.findall(html[:4_000_000]):
        absolute = urljoin(base, href.replace("&amp;", "&"))
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        if (parsed.hostname or "").removeprefix("www.") != host or parsed.path == base_path:
            continue
        if _item_like(parsed.path):
            found.add(parsed.path.rstrip("/"))
    return len(found)


def assess(url: str, result: dict, *, min_items: int = 10, min_prices: int = 4,
           min_text_chars: int = 1500) -> dict:
    """Score a page and say whether it looks complete for its kind of URL."""
    content = result.get("content") or ""
    text = (result.get("text") or "").strip()
    content_type = result.get("content_type") or ""
    kind = page_kind(url)
    if "html" not in content_type and "markdown" not in content_type:
        return {"kind": kind, "complete": True, "score": len(text), "reason": "not a document page"}
    items = item_links(content, result.get("url") or url)
    prices = len(_PRICE.findall(text))
    score = items * 100 + prices * 20 + min(len(text), 50_000) / 10
    if kind == "search":
        complete = items >= min_items or prices >= min_prices
        reason = None if complete else f"search page with {items} item links and {prices} prices"
    elif kind == "item":
        complete = prices >= 1 or len(text) >= min_text_chars
        reason = None if complete else f"item page with no price and {len(text)} characters"
    else:
        complete = len(text) >= min_text_chars or (len(text) >= 400 and "<script" not in content)
        reason = None if complete else f"page with {len(text)} characters of text"
    return {"kind": kind, "complete": complete, "score": score, "item_links": items,
            "prices": prices, "text_chars": len(text), "reason": reason}
