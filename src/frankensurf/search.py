"""Validated free search adapters. Search observations are indexed discovery only."""
from __future__ import annotations
import xml.etree.ElementTree as ET
from urllib.parse import urlencode, urlparse, urljoin, parse_qs
from bs4 import BeautifulSoup

REGISTRY = {
    "searxng": {"kind": "json", "base_url": "http://127.0.0.1:8088", "path": "/search", "endpoint_scope": "loopback self-hosted"},
    "duckduckgo_html": {"kind": "html", "base_url": "https://html.duckduckgo.com", "path": "/html/", "endpoint_scope": "public search"},
    "bing_rss": {"kind": "rss", "base_url": "https://www.bing.com", "path": "/search", "endpoint_scope": "public search"},
}


def build_search(query: str, source: str, config: dict | None = None):
    if source not in REGISTRY: raise ValueError("Unknown search adapter")
    if not isinstance(query, str) or not query.strip() or len(query) > 2000: raise ValueError("Invalid search query")
    config = config or {}
    if set(config) - {"base_url", "engines", "language"}: raise ValueError("Unknown engine configuration")
    definition = REGISTRY[source]
    base = config.get("base_url") or definition["base_url"]
    parsed = urlparse(base)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password: raise ValueError("Invalid search endpoint")
    if source == "searxng" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}: raise ValueError("This release uses self-hosted loopback SearXNG only")
    if source != "searxng" and base != definition["base_url"]: raise ValueError("Public search endpoints are fixed by validated adapter")
    params = {"q": query}
    if source == "searxng":
        params.update(format="json", language=config.get("language", "en-AU"))
        if config.get("engines"): params["engines"] = config["engines"]
    elif source == "bing_rss": params["format"] = "rss"
    return base.rstrip("/") + definition["path"] + "?" + urlencode(params), definition


def normalize_results(content: str, structured: object, source: str, limit: int):
    upstream_failures = []
    found = []
    if source == "searxng":
        if not isinstance(structured, dict) or not isinstance(structured.get("results"), list):
            raise ValueError("Expected SearXNG search results array")
        upstream_failures = structured.get("unresponsive_engines", [])
        for item in structured["results"]:
            if not isinstance(item, dict): continue
            found.append({"url": item.get("url"), "title": item.get("title") or "", "snippet": item.get("content") or "", "engines": item.get("engines") or ([item["engine"]] if item.get("engine") else []), "indexed_date": item.get("publishedDate")})
    elif source == "bing_rss":
        try: root = ET.fromstring(content)
        except ET.ParseError as exc: raise ValueError("Expected search RSS") from exc
        if root.tag != "rss": raise ValueError("Expected RSS root")
        for item in root.findall("./channel/item"):
            found.append({"url": item.findtext("link"), "title": item.findtext("title") or "", "snippet": item.findtext("description") or "", "engines": ["bing"], "indexed_date": None})
    else:
        soup = BeautifulSoup(content, "html.parser")
        for item in soup.select(".result"):
            link = item.select_one(".result__a")
            if not link: continue
            url = urljoin("https://html.duckduckgo.com", link.get("href") or "")
            redirected = parse_qs(urlparse(url).query).get("uddg")
            if redirected: url = redirected[0]
            snippet = item.select_one(".result__snippet")
            found.append({"url": url, "title": link.get_text(" ", strip=True), "snippet": snippet.get_text(" ", strip=True) if snippet else "", "engines": ["duckduckgo"], "indexed_date": None})
        if not found and "no results" not in soup.get_text(" ", strip=True).lower():
            raise ValueError("Search selectors absent; empty result unverified")
    results, seen = [], set()
    for item in found:
        url = item["url"]
        if not isinstance(url, str): continue
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or url in seen: continue
        seen.add(url)
        results.append(item)
        if len(results) >= limit: break
    return results, upstream_failures
