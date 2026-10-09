"""Draft a site module from one observed page by finding the page's own data.

Most listing pages ship their rows as JSON for their own frontend: JSON-LD
(ItemList, Product), JSON in a script tag (__NEXT_DATA__ and friends), or a
JSON response the page fetched while rendering. discover() scores every list
of objects it finds there, picks the one that looks most like items (a name,
a link, a price, an image), and turns it into a frankensurf.site-module/v1
draft. The draft is data, like any module: it is validated, run against the
same page, and only saved when the caller saves it.

Nothing here knows a site. The field names it looks for are web-wide
conventions (schema.org and common frontend JSON), not per-site rules.
"""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl, quote_plus, urljoin, urlparse

from .site_modules import SiteModule, _jsonld_blocks, _types

# Field roles and the keys that commonly carry them, best first.
_ROLES = {
    "name": ("name", "title", "productName", "product_name", "displayName", "display_name", "jobTitle",
             "headline", "label", "itemName"),
    "url": ("url", "href", "link", "permalink", "canonicalUrl", "canonical_url", "productUrl",
            "product_url", "pdpUrl", "seoUrl", "uri", "path"),
    "price": ("price", "salePrice", "sale_price", "currentPrice", "current_price", "finalPrice",
              "lowPrice", "amount", "value", "formattedPrice", "displayPrice", "priceValue"),
    "image": ("image", "imageUrl", "image_url", "thumbnail", "thumbnailUrl", "img", "picture",
              "primaryImage", "mainImage", "images"),
}
_WEIGHTS = {"name": 3, "url": 2, "price": 2, "image": 1}
# Containers a role's value often sits one or two levels inside.
_NESTS = ("item", "product", "offers", "offer", "price", "prices", "pricing", "priceInfo", "media",
          "images", "image", "attributes", "node", "listing", "data", "current", "0")
_SEGMENT = re.compile(r"[^.]{1,128}")
_MIN_ROWS = 3
_MAX_DEPTH = 10
_MAX_LISTS = 400


def _scalar(value):
    return isinstance(value, (str, int, float)) and not isinstance(value, bool)


def _looks(role, value):
    if role == "price":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value >= 0
        return isinstance(value, str) and bool(re.search(r"\d", value)) and len(value) < 40
    if not isinstance(value, str) or not value.strip():
        return False
    if role in ("url", "image"):
        text = value.strip()
        return text.startswith(("http://", "https://", "/")) and " " not in text
    return 2 <= len(value.strip()) <= 400


def _field_path(row, role):
    """The dotted path inside row that carries role, or None."""
    def search(node, prefix, depth):
        if not isinstance(node, dict):
            return None
        for key in _ROLES[role]:
            if key in node and _SEGMENT.fullmatch(key):
                value = node[key]
                if isinstance(value, list) and value and role == "image":
                    value, key = value[0], key + ".0"
                if isinstance(value, dict) and role in ("image", "url"):
                    for inner in ("url", "src", "href"):
                        if _looks(role, value.get(inner)):
                            return prefix + key + "." + inner
                if _looks(role, value):
                    return prefix + key
        if depth >= 2:
            return None
        for nest in _NESTS:
            child = node.get(nest)
            if isinstance(child, list) and child and nest != "0":
                child, nest = child[0], nest + ".0"
            found = search(child, prefix + nest + ".", depth + 1)
            if found:
                return found
        return None
    return search(row, "", 0)


def _walk_value(row, path):
    for segment in path.split("."):
        if isinstance(row, dict):
            row = row.get(segment)
        elif isinstance(row, list) and segment.isdigit() and int(segment) < len(row):
            row = row[int(segment)]
        else:
            return None
    return row


def _score_list(rows, path="", host=None, terms=()):
    """Field paths for this list of rows and how item-like it is."""
    objects = [row for row in rows if isinstance(row, dict)]
    if len(objects) < _MIN_ROWS or len(objects) < 0.8 * len(rows):
        return None
    sample = objects[:30]
    fields = _fields(sample)
    # A listing row carries at least two of name, link, price and image; a
    # schema.org ItemList may carry links alone.
    if fields is None or (len(fields) < 2 and not (path.endswith("itemListElement") and "url" in fields)):
        return None
    quality = sum(_WEIGHTS[role] for role in fields)
    # Results rows are rich objects; facets, menus and crumbs are thin.
    keys = sum(len(row) for row in sample) / len(sample)
    quality *= 1.5 if keys >= 6 else 1.0 if keys >= 4 else 0.6
    tail = [part for part in path.split(".") if not part.isdigit()][-2:]
    if any(_CHROME_PATH.search(part) for part in tail):
        return None
    if "url" in fields and host:
        links = [urlparse(str(_walk_value(row, fields["url"]) or "")).hostname for row in sample]
        away = sum(1 for link in links if link and not _same_site(link, host))
        if away > 0.5 * len(sample):
            return None
    if terms:
        # Names when there are names: a filter option's URL repeats the query too.
        blob = [str(_walk_value(row, fields["name"])).lower() if "name" in fields
                else json.dumps(row, ensure_ascii=False).lower() for row in sample]
        hits = sum(1 for text in blob if any(term in text for term in terms))
        # Results for a query mention it somewhere; a list that never does is page furniture.
        if not hits:
            return None
        quality *= 1 + 2 * hits / len(sample)
    return fields, quality * min(len(objects), 60)


_CHROME_PATH = re.compile(r"(?i)(facet|filter|refine|nav|menu|breadcrumb|crumb|footer|header|consent|"
                          r"cookie|categor|brand|sort|banner|promo|suggest|related|recent|option|recommend)")


_SECOND_LEVEL = {"co", "com", "net", "org", "gov", "edu", "ac", "ne", "or", "go"}


def _same_site(host, other):
    def tail(name):
        parts = name.lower().split(".")
        keep = 3 if len(parts) >= 3 and parts[-2] in _SECOND_LEVEL else 2
        return ".".join(parts[-keep:])
    return tail(host) == tail(other)


# Words that name the page type rather than what was searched for.
_GENERIC = {"search", "results", "result", "html", "htm", "php", "aspx", "query", "catalogsearch",
            "buscar", "zoeken", "recherche", "suche", "annonser", "annunci", "anuncios", "jobs", "job",
            "shop", "browse", "catalog", "products", "product", "list", "category", "directory", "www"}


def _terms(url):
    """Words the page was asked for: query values, else the path's last segment."""
    parsed = urlparse(url)
    text = " ".join(value for _, value in parse_qsl(parsed.query))
    if not text.strip():
        segments = [segment for segment in parsed.path.split("/") if segment]
        text = segments[-1] if segments else ""
    words = [word for word in re.findall(r"[^\W_]{3,}", text.lower()) if word not in _GENERIC]
    # Stems, so gitarre finds E-Gitarren and jacket finds jackets.
    return tuple(dict.fromkeys(word[:5] for word in words))[:5]


def _fields(sample):
    fields = {}
    for role in _ROLES:
        counts = {}
        for row in sample:
            path = _field_path(row, role)
            if path:
                counts[path] = counts.get(path, 0) + 1
        if counts:
            path, hits = max(counts.items(), key=lambda pair: pair[1])
            if hits >= 0.6 * len(sample):
                fields[role] = path
    if "name" not in fields and "url" not in fields:
        return None
    # Distinct names: a list of nav links or repeated labels is not a listing.
    if "name" in fields:
        names = {str(_walk_value(row, fields["name"])).strip().lower() for row in sample}
        if len(names) < 0.7 * len(sample):
            return None
    return fields


def _lists(value, path="", depth=0, found=None):
    """Every list of objects under value, with its dotted path."""
    found = [] if found is None else found
    if depth > _MAX_DEPTH or len(found) >= _MAX_LISTS:
        return found
    if isinstance(value, list):
        if sum(isinstance(row, dict) for row in value) >= _MIN_ROWS:
            found.append((path, value))
        for index, child in enumerate(value[:5]):
            _lists(child, f"{path}.{index}" if path else str(index), depth + 1, found)
    elif isinstance(value, dict):
        for key, child in value.items():
            if isinstance(child, (dict, list)) and isinstance(key, str) and _SEGMENT.fullmatch(key):
                _lists(child, f"{path}.{key}" if path else key, depth + 1, found)
    return found


def _sources(result):
    """(source spec, JSON root) pairs: where a module could read rows from."""
    structured = result.get("structured") if isinstance(result.get("structured"), dict) else {}
    for block in _jsonld_blocks(structured):
        kinds = sorted(_types(block))
        spec = {"kind": "jsonld", "path": ""}
        if kinds:
            spec["type"] = kinds[0]
        yield spec, block
    for entry in structured.get("embedded_json") or ():
        if not isinstance(entry, dict):
            continue
        ident = entry.get("id")
        selector = (f'script#{ident}[type="application/json"]'
                    if isinstance(ident, str) and re.fullmatch(r"[A-Za-z_][\w-]{0,99}", ident)
                    else 'script[type="application/json"]')
        yield {"kind": "embedded_json", "selector": selector, "path": ""}, entry.get("data")
    captured = result.get("captured_json") or {}
    entries = captured.get("items") if isinstance(captured, dict) else captured
    for entry in entries or ():
        if not isinstance(entry, dict) or not entry.get("url"):
            continue
        parsed = urlparse(str(entry["url"]))
        marker = parsed.path if len(parsed.path) > 1 else parsed.netloc + parsed.path
        yield {"kind": "captured_json", "url_contains": marker[:300], "path": ""}, entry.get("data")


def candidates(result, url=None):
    """Item-like lists the page carries, best first."""
    found = []
    url = url or result.get("url") or ""
    host, terms = urlparse(url).hostname, _terms(url)
    for spec, root in _sources(result):
        for path, rows in _lists(root):
            if spec["kind"] == "jsonld" and not path:
                continue
            scored = _score_list(rows, path, host, terms)
            if scored is None:
                continue
            fields, score = scored
            found.append({"source": {**spec, "path": path}, "fields": fields, "rows": len(rows),
                          "score": score})
    # A selector shared by several script tags reads only the first; prefer unique ones.
    found.sort(key=lambda item: (-item["score"], item["source"]["kind"] != "jsonld"))
    return found


_CLASS = re.compile(r"-?[A-Za-z_][\w-]{0,80}")
_PRICE_TEXT = re.compile(r"(?:[$€£¥₹]|\b(?:USD|AUD|EUR|GBP|NOK|SEK|DKK|CHF|kr|zł|R\$))\s?\d|\d[\d.,\s]*\s?(?:€|kr|zł|Kč)")
_ACCOUNT_LINK = re.compile(r"(?i)login|log-in|signin|sign-in|sign_in|oauth|account|register|wishlist|cart")
_IMAGE_ATTRIBUTES =["data-src", "data-original", "data-lazy-src", "src"]


def _html_candidates(content, url, terms):
    """Repeated elements that each hold one item: a same-site link and some text.
    Rows are found by shape (a tag and class repeated many times), not by name."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup((content or "")[:4_000_000], "html.parser")
    for dead in soup.select("script,style,noscript,svg,header,footer,nav"):
        dead.decompose()
    host = urlparse(url).hostname or ""
    groups = {}
    for element in soup.find_all(True, class_=True, limit=60_000):
        for name in element.get("class") or ():
            if _CLASS.fullmatch(name) and not name[0].isdigit():
                groups.setdefault(f"{element.name}.{name}", []).append(element)
    found = []
    for selector, elements in groups.items():
        if not 4 <= len(elements) <= 400 or _CHROME_PATH.search(selector.split(".", 1)[1]):
            continue
        sample = elements[:40]
        rows = []
        for element in sample:
            links = {link.get("href").split("#")[0] for link in element.select("a[href]")
                     if link.get("href") and not link.get("href").startswith(("#", "javascript:", "mailto:"))}
            links = {link for link in links
                     if not urlparse(link).hostname or _same_site(urlparse(link).hostname, host)}
            text = element.get_text(" ", strip=True)
            if not 1 <= len(links) <= 3 or not 8 <= len(text) <= 1500:
                continue
            rows.append((element, text))
        if len(rows) < max(4, 0.7 * len(sample)):
            continue
        texts = [text for _, text in rows]
        if len({text.lower() for text in texts}) < 0.8 * len(texts):
            continue
        # The row's link (the first one, as the module will read it) differs per
        # row: one shared link (sign in, a category) means these are not items.
        firsts = [urljoin(url, element.select_one("a[href]").get("href")).split("#")[0] for element, _ in rows]
        # Links back to this listing with other parameters are filters or pages.
        own = urlparse(url).path.rstrip("/")
        if (len(set(firsts)) < 0.8 * len(rows) or url.split("#")[0] in firsts
                or sum(urlparse(href).path.rstrip("/") == own for href in firsts) > 0.5 * len(rows)
                or sum(bool(_ACCOUNT_LINK.search(href)) for href in firsts) > 1):
            continue
        priced = sum(1 for text in texts if _PRICE_TEXT.search(text))
        imaged = sum(1 for element, _ in rows if element.find("img"))
        quality = 3 + 2 + (2 if priced >= 0.6 * len(rows) else 0) + (1 if imaged >= 0.6 * len(rows) else 0)
        if terms:
            # A bonus, not a gate: results need not repeat the query (brands, other languages).
            hits = sum(1 for text in texts if any(term in text.lower() for term in terms))
            quality *= 1 + hits / len(texts)
        mean = sum(len(text) for text in texts) / len(texts)
        found.append({"selector": selector, "rows": len(elements), "priced": priced >= 0.6 * len(rows),
                      "imaged": imaged >= 0.6 * len(rows), "elements": [element for element, _ in rows],
                      "score": quality * min(len(elements), 60) * (1.2 if 40 <= mean <= 600 else 1.0)})
    found.sort(key=lambda item: -item["score"])
    return found


def _sub_selector(elements, test):
    """The tag.class inside each row whose element passes test, most common first."""
    counts = {}
    for element in elements[:30]:
        seen = set()
        for inner in element.find_all(True, class_=True):
            if not test(inner):
                continue
            for name in inner.get("class") or ():
                key = f"{inner.name}.{name}"
                if _CLASS.fullmatch(name) and key not in seen:
                    seen.add(key)
                    counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    best, hits = max(counts.items(), key=lambda pair: pair[1])
    return best if hits >= 0.6 * min(len(elements), 30) else None


def _html_source(candidate):
    elements = candidate["elements"]
    fields = {"url": {"selector": "a[href]", "attribute": ["href"]}}
    title = _sub_selector(elements, lambda node: (node.name in ("h2", "h3", "h4", "h5")
                          or re.search(r"(?i)title|name", " ".join(node.get("class") or ())))
                          and len(node.get_text(" ", strip=True)) >= 3)
    sample = elements[:30]
    if title:
        fields["name"] = {"selector": title}
    elif sum(len(row.select_one("a[href]").get_text(" ", strip=True)) >= 3 for row in sample) >= 0.6 * len(sample):
        fields["name"] = {"selector": "a[href]"}
    elif sum(bool((row.find("img") or {}).get("alt")) for row in sample) >= 0.6 * len(sample):
        fields["name"] = {"selector": "img", "attribute": ["alt"]}
    if candidate["priced"]:
        price = _sub_selector(elements, lambda node: re.search(r"(?i)price", " ".join(node.get("class") or ()))
                              and _PRICE_TEXT.search(node.get_text(" ", strip=True)))
        if price:
            fields["price"] = {"selector": price}
    if candidate["imaged"]:
        fields["image"] = {"selector": "img", "attribute": _IMAGE_ATTRIBUTES}
    return {"kind": "html", "item_selector": candidate["selector"], "fields": fields}


def _module_id(url):
    host = urlparse(url).hostname or "site"
    host = host[4:] if host.startswith("www.") else host
    return ("auto-" + re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-"))[:64]


def draft(url, candidate, *, module_id=None, notes=None, search=None):
    """A site module record for one candidate list on the page at url."""
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    types = {"name": "text", "url": "url", "price": "number", "image": "url"}
    record = {
        "schema": "frankensurf.site-module/v1",
        "id": module_id or _module_id(url),
        "version": "1",
        "match": {"origin": origin, "path_pattern": re.escape(parsed.path or "/")},
        "sources": {"listing": candidate["source"]},
        "items": {"from": "listing",
                  "fields": {role: {"path": path, "type": types[role]}
                             for role, path in candidate["fields"].items()}},
        "notes": notes or f"Drafted by frankensurf module discover from {url}",
    }
    if parsed.query:
        record["match"]["query_keys"] = sorted({key for key in re.findall(r"(?:^|&)([^=&]+)", parsed.query)
                                                if re.fullmatch(r"[A-Za-z0-9_.\-\[\]]{1,128}", key)})
        if not record["match"]["query_keys"]:
            del record["match"]["query_keys"]
    search = search_template(url) or search
    if search:
        template, encoding, path_pattern = search
        record["templates"] = {"search": {"url": template,
                                          "params": {"query": {"required": True, "encoding": encoding}}}}
        if path_pattern:
            record["match"]["path_pattern"] = path_pattern
    return record


_SEARCH_WORDS = re.compile(r"(?i)search|/sch/|find|query|s[öø]k|zoek|such|busca|recherch|cerca|haku|szuk|hled|"
                           r"検索|搜索|검색")
_HELPER_INPUT = re.compile(r"(?i)suggest|hidden|autocomplete|typeahead|csrf|token")


def find_search(result, url):
    """How a site searches, from one of its pages (usually the home page):
    schema.org SearchAction first, then the page's own search form. Returns
    {"template": URL with {query}, "encoding", "path_pattern", "from"} or None.
    Standards and HTML forms only; nothing here knows a site."""
    origin = "{0.scheme}://{0.netloc}".format(urlparse(url))
    structured = result.get("structured") if isinstance(result.get("structured"), dict) else {}
    for block in _jsonld_blocks(structured):
        actions = block.get("potentialAction")
        for action in actions if isinstance(actions, list) else [actions]:
            if not isinstance(action, dict) or "SearchAction" not in _types(action):
                continue
            target = action.get("target")
            target = target.get("urlTemplate") if isinstance(target, dict) else target
            target = target[0] if isinstance(target, list) and target else target
            if not isinstance(target, str):
                continue
            names = re.findall(r"\{([A-Za-z_][\w-]{0,63})\}", target)
            if len(set(names)) != 1:
                continue
            template = urljoin(url, target.replace("{" + names[0] + "}", "{query}"))
            if not template.startswith(origin + "/"):
                continue
            built = template.split("?", 1)
            encoding = "query" if len(built) == 2 and "{query}" in built[1] else "path"
            return {"template": template, "encoding": encoding, "path_pattern": None, "from": "jsonld"}
    content_type = str(result.get("content_type") or "")
    if "html" not in content_type and content_type:
        return None
    from bs4 import BeautifulSoup
    soup = BeautifulSoup((result.get("content") or "")[:4_000_000], "html.parser")
    best = None
    for form in soup.find_all("form", limit=50):
        if (form.get("method") or "get").lower() != "get":
            continue
        fields = form.find_all(["input", "select"])
        # The box a person types into: a conventional name first, then any
        # search-typed input that is not a suggestion or hidden helper field.
        typed = [field for field in fields if field.name == "input" and field.get("name")
                 and (field.get("type") or "text").lower() in ("text", "search")
                 and not _HELPER_INPUT.search(field.get("name"))]
        query = next((field for field in typed if _QUERY_KEYS.match(field.get("name"))), None)
        query = query or next((field for field in typed if (field.get("type") or "").lower() == "search"), None)
        if query is None and len(typed) == 1:
            # One box in a form that says it searches (its own attributes or the box's
            # placeholder or label), whatever the box is named (eBay's _nkw, say).
            words = " ".join(str(value) for node in (form, typed[0]) for key, value in node.attrs.items()
                             if key in ("role", "id", "class", "action", "placeholder", "aria-label", "title"))
            if _SEARCH_WORDS.search(words):
                query = typed[0]
        if query is None:
            continue
        action = urljoin(url, form.get("action") or url).split("#")[0].split("?")[0]
        if not action.startswith(origin + "/") and action != origin:
            continue
        parts = []
        for field in fields:
            name = field.get("name")
            if not name or field.name != "input":
                continue
            if field is query:
                parts.append(f"{quote_plus(name)}={{query}}")
            elif (field.get("type") or "").lower() == "hidden" and field.get("value") is not None:
                parts.append(f"{quote_plus(name)}={quote_plus(field.get('value'))}")
        score = 2 if (query.get("type") or "").lower() == "search" else 1
        score += 1 if re.search(r"(?i)search", " ".join([form.get("role") or "", form.get("id") or "",
                                                          " ".join(form.get("class") or []),
                                                          form.get("action") or ""])) else 0
        if best is None or score > best[0]:
            best = (score, action + "?" + "&".join(parts))
    if best:
        return {"template": best[1], "encoding": "query", "path_pattern": None, "from": "form"}
    return None


# Parameter names sites commonly use for the search box: a web convention, not a site list.
_QUERY_KEYS = re.compile(r"(?i)^(q|qs|query|search|searchterm|search_term|searchtext|search_query|keyword|"
                         r"keywords|kw|k|term|terms|text|words|freetext|sw|s|tr|st|ntt|w|p)$")


# A path segment that introduces a search word: /q/lamp, /search/lamp, /tag/lamp.
_PATH_SEARCH = re.compile(r"(?i)^(q|s|k|search|suche|zoeken|buscar|busca|recherche|cerca|haku|sok|soeg|szukaj|"
                          r"tag|tags|topic|topics|keyword|keywords)$")


def search_template(url):
    """(template URL with {query}, encoding, widened path pattern or None) for a
    search URL, or None. The query is a parameter (?q=lamp) or one path segment
    (/q/lamp/); every other part of the URL is kept as it was."""
    parsed = urlparse(url)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    texty = [(index, key) for index, (key, value) in enumerate(pairs)
             if re.search(r"[^\W\d_]", value) and value.lower() not in ("true", "false", "on", "off")]
    chosen = next((index for index, key in texty if _QUERY_KEYS.match(key)), None)
    if chosen is None and len(texty) == 1:
        chosen = texty[0][0]
    if chosen is not None:
        parts = [f"{quote_plus(key)}={'{query}' if index == chosen else quote_plus(value)}"
                 for index, (key, value) in enumerate(pairs)]
        base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        return base + "?" + "&".join(parts), "query", None
    if parsed.query:
        return None
    segments = parsed.path.split("/")
    for index in range(len(segments) - 1, -1, -1):
        segment = segments[index]
        # One plain word only: a slug like python-jobs cannot be rebuilt from a query.
        previous = next((part for part in reversed(segments[:index]) if part), "")
        if (re.fullmatch(r"[^\W\d_]{2,64}", segment) and segment.lower() not in _GENERIC
                and _PATH_SEARCH.match(previous)):
            before, after = "/".join(segments[:index]) + "/", "/".join(segments[index + 1:])
            after = ("/" + after) if index + 1 < len(segments) else ""
            template = f"{parsed.scheme}://{parsed.netloc}{before}{{query}}{after}"
            return template, "path", re.escape(before) + r"[^/]+" + re.escape(after)
        if segment:
            break
    return None


def discover(result, url, *, module_id=None, limit=3, search=None):
    """Draft site modules from one read result. Each draft is validated and run
    against the same page; drafts that extract nothing are dropped. JSON feeds
    come first (they survive redesigns better); repeated HTML rows after."""
    drafts = []
    seen = set()
    content_type = str(result.get("content_type") or "")
    html = (_html_candidates(result.get("content"), url, _terms(url))[:4]
            if "html" in content_type or not content_type else [])
    pool = candidates(result, url) + [
        {"source": _html_source(item), "fields": None, "rows": item["rows"], "score": item["score"]}
        for item in html]
    for candidate in pool:
        if candidate["fields"] is None:
            candidate["fields"] = {role: role for role in candidate["source"]["fields"]}
        key = json.dumps(candidate["source"], sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        record = draft(url, candidate, module_id=module_id, search=search)
        try:
            module = SiteModule.from_record(record)
        except ValueError:
            continue
        output = module.extract(result, url)
        if output["count"] < _MIN_ROWS:
            continue
        # Items have names (or are bare links from an ItemList); rows whose
        # names are mostly empty are tiles or banners.
        if "name" in candidate["fields"]:
            named = sum(1 for item in output["items"] if isinstance(item.get("name"), str)
                        and len(item["name"].strip()) >= 3)
            if named < 0.6 * output["count"]:
                continue
        if "url" in candidate["fields"]:
            linked = sum(1 for item in output["items"] if item.get("url"))
            if linked < 0.6 * output["count"]:
                continue
        drafts.append({"module": module.record(), "count": output["count"],
                       "fields": sorted(candidate["fields"]), "sample": output["items"][:3]})
        if len(drafts) >= limit:
            break
    return {"url": url, "drafts": drafts,
            "found": bool(drafts),
            "hint": None if drafts else
            "No item feed or repeated item rows found; try a rendered read (capture_json_responses)"
            " or write the module by hand."}
