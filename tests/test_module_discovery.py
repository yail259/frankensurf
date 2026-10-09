"""Drafting a site module from a page's own JSON feed (synthetic fixtures only)."""
import json

from frankensurf.module_discovery import candidates, discover
from frankensurf.runtime import _parse_builtin_content
from frankensurf.site_modules import SiteModule

URL = "https://shop.example.com/search?q=lamp"
NAV = [{"label": "Home", "href": "/"}, {"label": "Home", "href": "/a"}, {"label": "Home", "href": "/b"}]


def page(scripts):
    body = "".join(scripts)
    return f"<html><head><title>Lamps</title>{body}</head><body><p>Lamps</p></body></html>"


def result_for(content, **extra):
    parsed = _parse_builtin_content(content, "text/html", URL, None)
    return {"url": URL, "content": content, "content_type": "text/html", **parsed, **extra}


def next_data(products):
    data = {"props": {"pageProps": {"nav": NAV, "search": {"total": len(products), "results": products}}}}
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'


PRODUCTS = [{"id": n, "title": f"Brass lamp {n}", "seoUrl": f"/p/brass-lamp-{n}",
             "priceInfo": {"currentPrice": 40 + n}, "images": [{"url": f"https://img.example/{n}.jpg"}]}
            for n in range(8)]


def test_next_data_listing_becomes_a_module_that_extracts_items():
    found = discover(result_for(page([next_data(PRODUCTS)])), URL)
    assert found["found"] and found["drafts"]
    best = found["drafts"][0]
    module = best["module"]
    assert module["sources"]["listing"] == {"kind": "embedded_json",
                                            "selector": 'script#__NEXT_DATA__[type="application/json"]',
                                            "path": "props.pageProps.search.results"}
    assert module["items"]["fields"]["name"]["path"] == "title"
    assert module["items"]["fields"]["price"]["path"] == "priceInfo.currentPrice"
    assert module["items"]["fields"]["image"]["path"] == "images.0.url"
    assert module["match"] == {"origin": "https://shop.example.com", "path_pattern": "/search",
                               "query_keys": ["q"]}
    assert best["count"] == 8
    assert best["sample"][0] == {"name": "Brass lamp 0", "url": "https://shop.example.com/p/brass-lamp-0",
                                 "price": 40, "image": "https://img.example/0.jpg"}
    SiteModule.from_record(module)


def test_jsonld_item_list_is_found():
    itemlist = {"@context": "https://schema.org", "@type": "ItemList", "itemListElement": [
        {"@type": "ListItem", "position": n, "item": {"@type": "Product", "name": f"Desk lamp {n}",
                                                      "url": f"https://shop.example.com/p/{n}",
                                                      "offers": {"price": "19.9" + str(n)}}}
        for n in range(5)]}
    content = page([f'<script type="application/ld+json">{json.dumps(itemlist)}</script>'])
    found = discover(result_for(content), URL)
    module = found["drafts"][0]["module"]
    assert module["sources"]["listing"]["kind"] == "jsonld"
    assert module["sources"]["listing"]["path"] == "itemListElement"
    assert module["items"]["fields"]["name"]["path"] == "item.name"
    assert module["items"]["fields"]["price"]["path"] == "item.offers.price"
    assert found["drafts"][0]["sample"][1]["price"] == 19.91


def test_captured_json_feed_is_found():
    feed = {"data": {"hits": [{"name": f"Lamp {n}", "link": f"/p/{n}", "amount": n} for n in range(6)]}}
    result = result_for(page([]), captured_json={"items": [
        {"url": "https://shop.example.com/api/v2/search?q=lamp", "data": feed}]})
    module = discover(result, URL)["drafts"][0]["module"]
    assert module["sources"]["listing"] == {"kind": "captured_json", "url_contains": "/api/v2/search",
                                            "path": "data.hits"}


def test_navigation_and_short_lists_are_not_listings():
    content = page([next_data(PRODUCTS[:2])])
    assert not [c for c in candidates(result_for(content)) if c["source"]["path"].endswith("nav")]
    found = discover(result_for(content), URL)
    assert found["drafts"] == [] and found["hint"]


async def test_runtime_discovers_saves_and_then_reads_items(tmp_path, monkeypatch):
    from frankensurf import providers
    from frankensurf.providers import ProviderManifest, ProviderRegistry
    from frankensurf.runtime import Runtime

    content = page([next_data(PRODUCTS)])

    class Fixture:
        manifest = ProviderManifest("fixture", "1")

        def available(self, configured):
            return True

        async def acquire(self, request, services):
            return {"url": request.url, "content": content, "content_type": "text/html", "http_status": 200}
    registry = ProviderRegistry()
    registry.register(Fixture())
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    overrides = {"origin_route_hint_ttl_seconds": 0, "origin_min_interval_seconds": 0,
                 "second_opinion_text_chars": 0, "completeness_escalation": False}
    async with Runtime(tmp_path) as web:
        found = await web.discover_module(URL, save=True, policy_overrides=overrides)
        assert found["saved"]["id"] == "auto-shop-example-com" and len(found["reads"]) == 1
        again = await web.read("https://shop.example.com/search?q=desk", policy_overrides=overrides)
    assert len(again["items"]) == 8 and again["items"][0]["name"] == "Brass lamp 0"


def test_server_rendered_rows_become_an_html_module():
    cards = "".join(
        f'<div class="product-tile"><a href="/p/{n}"><img data-src="/img/{n}.jpg" src="data:,">'
        f'<h3 class="tile-title">Steel lamp {n}</h3></a><span class="tile-price">$ {n}9.00</span></div>'
        for n in range(1, 9))
    menu = "".join(f'<li class="menu-item"><a href="/c/{n}">Lamps {n}</a></li>' for n in range(9))
    content = (f"<html><head><title>Lamps</title></head><body><nav><ul>{menu}</ul></nav>"
               f"<main>{cards}</main></body></html>")
    found = discover(result_for(content), URL)
    module = found["drafts"][0]["module"]
    assert module["sources"]["listing"]["kind"] == "html"
    assert module["sources"]["listing"]["item_selector"] == "div.product-tile"
    assert found["drafts"][0]["sample"][0] == {"url": "https://shop.example.com/p/1", "name": "Steel lamp 1",
                                               "price": 19, "image": "https://shop.example.com/img/1.jpg"}


def test_drafts_carry_a_search_template_built_from_the_url():
    from frankensurf.module_discovery import search_template
    assert search_template("https://x.example/s?cat=5&searchTerm=red+boots&page=2") == (
        "https://x.example/s?cat=5&searchTerm={query}&page=2", "query", None)
    assert search_template("https://x.example/q/fiets/") == ("https://x.example/q/{query}/", "path", "/q/[^/]+/")
    assert search_template("https://x.example/about/team") is None
    assert search_template("https://x.example/item/123") is None
    module = SiteModule.from_record(discover(result_for(page([next_data(PRODUCTS)])), URL)["drafts"][0]["module"])
    assert module.build_url("search", {"query": "desk lamp"}) == "https://shop.example.com/search?q=desk+lamp"


async def test_batch_template_reads_many_searches_with_the_saved_module(tmp_path, monkeypatch):
    from frankensurf import providers
    from frankensurf.providers import ProviderManifest, ProviderRegistry
    from frankensurf.runtime import Runtime
    seen = []

    class Fixture:
        manifest = ProviderManifest("fixture", "1")

        def available(self, configured):
            return True

        async def acquire(self, request, services):
            seen.append(request.url)
            word = request.url.rsplit("=", 1)[-1]
            products = [{**row, "title": f"{word} {row['title']}"} for row in PRODUCTS]
            return {"url": request.url, "content": page([next_data(products)]), "content_type": "text/html",
                    "http_status": 200}
    registry = ProviderRegistry()
    registry.register(Fixture())
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    overrides = {"origin_route_hint_ttl_seconds": 0, "origin_min_interval_seconds": 0,
                 "second_opinion_text_chars": 0, "completeness_escalation": False}
    async with Runtime(tmp_path) as web:
        web._domain_delay = 0
        await web.discover_module(URL, save=True, policy_overrides=overrides)
        results = await web.batch_template("auto-shop-example-com", "search",
                                           [{"query": "desk"}, {"query": "floor lamp"}],
                                           policy_overrides=overrides)
    assert [result["params"]["query"] for result in results] == ["desk", "floor lamp"]
    assert results[1]["url"] == "https://shop.example.com/search?q=floor+lamp"
    assert results[1]["items"][0]["name"] == "floor+lamp Brass lamp 0" and len(results[0]["items"]) == 8


def test_find_search_reads_search_action_then_the_search_form():
    from frankensurf.module_discovery import find_search
    home = "https://shop.example.com/"
    action = {"@context": "https://schema.org", "@type": "WebSite", "url": home, "potentialAction": {
        "@type": "SearchAction", "target": {"@type": "EntryPoint",
                                            "urlTemplate": "https://shop.example.com/find?text={term}"},
        "query-input": "required name=term"}}
    found = find_search(result_for(page([f'<script type="application/ld+json">{json.dumps(action)}</script>'])),
                        home)
    assert found == {"template": "https://shop.example.com/find?text={query}", "encoding": "query",
                     "path_pattern": None, "from": "jsonld"}
    form = ('<html><body><form action="/login" method="post"><input name="q"></form>'
            '<form role="search" action="/s"><input type="hidden" name="cat" value="all">'
            '<input type="search" name="kw"><button>Go</button></form></body></html>')
    found = find_search(result_for(form), home)
    assert found["template"] == "https://shop.example.com/s?cat=all&kw={query}" and found["from"] == "form"
    offsite = '<form action="https://other.example/search"><input type="search" name="q"></form>'
    assert find_search(result_for(offsite), home) is None


async def test_discover_from_a_home_page_with_a_query(tmp_path, monkeypatch):
    from frankensurf import providers
    from frankensurf.providers import ProviderManifest, ProviderRegistry
    from frankensurf.runtime import Runtime
    home_page = '<html><body><form action="/search"><input type="search" name="q"></form></body></html>'

    class Fixture:
        manifest = ProviderManifest("fixture", "1")

        def available(self, configured):
            return True

        async def acquire(self, request, services):
            content = page([next_data(PRODUCTS)]) if "/search" in request.url else home_page
            return {"url": request.url, "content": content, "content_type": "text/html", "http_status": 200}
    registry = ProviderRegistry()
    registry.register(Fixture())
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    overrides = {"origin_route_hint_ttl_seconds": 0, "origin_min_interval_seconds": 0,
                 "second_opinion_text_chars": 0, "completeness_escalation": False}
    async with Runtime(tmp_path) as web:
        found = await web.discover_module("https://shop.example.com/", query="brass lamp",
                                          policy_overrides=overrides)
    assert found["search"]["from"] == "form" and len(found["reads"]) == 2
    module = found["drafts"][0]["module"]
    assert module["templates"]["search"]["url"] == "https://shop.example.com/search?q={query}"
    assert found["drafts"][0]["count"] == 8


def test_navigation_and_seo_link_farms_are_not_items():
    from frankensurf.module_discovery import _link_farm
    assert _link_farm([f"Python jobs in {city}" for city in ("London", "Leeds", "Bath", "York", "Hull")])
    assert not _link_farm([f"Apple iPhone 15 {size}GB" for size in (128, 256, 512, 1024)])
    nav = {"@context": "https://schema.org", "@type": "SiteNavigationElement", "hasPart": [
        {"@type": "WebPage", "name": f"Watches {n}", "url": f"https://shop.example.com/c/{n}"}
        for n in range(8)]}
    content = page([f'<script type="application/ld+json">{json.dumps(nav)}</script>'])
    assert discover(result_for(content), "https://shop.example.com/search?q=watch")["drafts"] == []
