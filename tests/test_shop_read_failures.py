"""Shop pages that read wrong: a 404 under shop chrome, a 404 confirmed past a
wall, a wrong category URL the home page links correctly, a product grid drawn
by client-side templates, and a platform collection whose grid a script draws."""
import json

import pytest

from frankensurf import platform_feeds, providers
from frankensurf.providers import ProviderManifest, ProviderRegistry
from frankensurf.runtime import (Runtime, WebFailure, WebPolicy, _client_templates, _connection_failure_message,
                                 _home_link_for, _site_error, _wants_second_opinion)

QUIET = {"origin_route_hint_ttl_seconds": 0, "origin_min_interval_seconds": 0, "second_opinion_text_chars": 0,
         "hedge_after_seconds": 0, "warm_up_on_wall": False}
CHROME = "".join(f'<a href="/c/{n}">Department {n}</a> ' for n in range(40)) + "<p>Cart Help Stores</p>" * 30


def registry_of(monkeypatch, *plugins):
    registry = ProviderRegistry()
    for plugin in plugins:
        registry.register(plugin)
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)


def fixed(identifier, outcome=None, pages=None, content_type="text/html"):
    calls = []

    class Plugin:
        manifest = ProviderManifest(identifier, "1")

        def available(self, configured):
            return True

        async def acquire(self, request, services):
            calls.append(request.url)
            if outcome:
                raise WebFailure(outcome, "fixture outcome", 404 if outcome == "NOT_FOUND" else None)
            body = (pages or {}).get(request.url)
            if body is None:
                raise WebFailure("NOT_FOUND", "fixture 404", 404)
            kind = "application/json" if request.url.endswith(("products.json?limit=250",)) else content_type
            return {"url": request.url, "content": body, "content_type": kind, "http_status": 200}
    Plugin.__name__ = identifier
    plugin = Plugin()
    plugin.calls = calls
    return plugin


# ---- a 404 page is a 404 however much chrome surrounds it ----

def test_a_404_title_is_the_sites_not_found_page_under_long_shop_chrome():
    chrome = "Shop all departments, your cart is empty, you may also like ... " * 2000
    assert _site_error("404 Not Found", chrome, "https://shop.example/gaming-mice", None)
    assert _site_error("Page not found | Shop", chrome, "https://shop.example/gaming-mice", None)
    # A long page that merely names an error word in its title still needs short text.
    assert not _site_error("Error | Shop", chrome, "https://shop.example/gaming-mice", None)
    assert not _site_error("404 Not Found T-Shirt | Shop", chrome, "https://shop.example/tee", None)


# ---- a 404 two tools met past a wall ends the read ----

async def test_a_not_found_confirmed_past_a_wall_ends_the_read(tmp_path, monkeypatch):
    walled, first, second, never = (fixed("walled", "CAPTCHA"), fixed("first", "NOT_FOUND"),
                                    fixed("second", "NOT_FOUND"), fixed("never", "VISUAL_REQUIRED"))
    registry_of(monkeypatch, walled, first, second, never)
    async with Runtime(tmp_path) as web:
        result = await web.read("https://shop.example/gaming-mice", WebPolicy(
            origin_route_hint_ttl_seconds=0, escalate_after_walls=0, rescue_not_found=False))
    assert [a["provider"] for a in result["receipt"]["attempts"]] == ["walled", "first", "second"]
    assert result["receipt"]["failure"]["code"] == "NOT_FOUND"


# ---- the home page links the page a wrong URL meant ----

HOME = ("<html><title>PC shop</title><body>" + CHROME
        + '<a href="/clearance-sale">Clearance Sale!</a> <a href="/hot-deals?promotion=1">Hot deals</a>'
        + '<a href="/product/clearance-cable-adaptor-usb-c-12345">Cable</a>'
        + "</body></html>")
LISTING = ("<html><title>Clearance Sale</title><body><h1>Clearance Sale</h1>"
           + "".join(f'<div class="card"><a href="/product/thing-{n}-{n:06d}">Clearance thing {n}</a> '
                     f'<span>${n + 10}.00</span></div>' for n in range(24))
           + "<p>" + "Cleared stock at lower prices with full warranty. " * 40 + "</p></body></html>")


def test_the_home_link_is_the_one_whose_path_names_the_words():
    home = {"content": HOME, "content_type": "text/html", "url": "https://shop.example/"}
    url = "https://shop.example/clearance"
    assert _home_link_for(home, "https://shop.example/", url, "clearance") == "https://shop.example/clearance-sale"
    # Nothing that names the words: no link.
    assert _home_link_for(home, "https://shop.example/", "https://shop.example/gaming-mice", "gaming mice") is None
    # Two equally good links: no guess.
    tied = {**home, "content": HOME.replace("</body>", '<a href="/clearance-deals">More</a></body>')}
    assert _home_link_for(tied, "https://shop.example/", url, "clearance") is None


async def test_a_wrong_category_url_is_rescued_through_the_home_page_link(tmp_path, monkeypatch):
    monkeypatch.setattr(Runtime, "completeness_enabled", True)
    site = fixed("http", pages={"https://shop.example/": HOME, "https://shop.example/clearance-sale": LISTING})
    registry_of(monkeypatch, site)
    async with Runtime(tmp_path) as web:
        result = await web.read("https://shop.example/clearance", policy_overrides=QUIET)
    receipt = result["receipt"]
    assert receipt["status"] == "observed" and result["url"] == "https://shop.example/clearance-sale"
    assert receipt["rescued"]["via"] == "home page link"
    assert receipt["rescued"]["link_url"] == "https://shop.example/clearance-sale"
    assert "rescued" in receipt["quality"]["flags"]
    # No search was needed.
    assert not any("search" in url for url in site.calls)


# ---- a grid drawn from client-side templates gets a rendered read ----

TEMPLATED = ("<html><title>Clearance</title><body>" + CHROME + "<div id='hits'></div>"
             '<script type="text/template" id="hit-template">'
             + "<div class='hit'><a href='{{url}}'>{{name}}</a> {{price.AUD.default}} {{#sale}}{{/sale}}</div>" * 6
             + "</script><script>render()</script></body></html>")


def test_client_side_templates_ask_for_a_rendered_read_of_a_long_page():
    policy = WebPolicy()
    result = {"content": TEMPLATED, "text": "x" * 14000, "content_type": "text/html",
              "receipt": {"status": "observed", "method": "http"}}
    assert _client_templates(TEMPLATED) >= 20
    assert _wants_second_opinion(result, policy, None)
    small = TEMPLATED.replace("{{", "").replace('<script type="text/template"',
                                                '<script type="text/template" data-x="{{a}}"')
    assert not _wants_second_opinion({**result, "content": small}, policy, None)
    assert not _wants_second_opinion({**result, "content": TEMPLATED.replace("text/template", "text/javascript")},
                                     policy, None)


# ---- a platform collection: its public JSON lists what the grid script draws ----

SHOPIFY_PAGE = ("<html><head><title>Deals</title><script>var Shopify = Shopify || {}; "
                'Shopify.shop = "demo.myshopify.com"; Shopify.currency = {"active":"AUD","rate":"1.0"};</script>'
                "</head><body>" + CHROME + "<h1>Deals</h1><div data-no-results><p>No products available.</p></div>"
                "</body></html>")


def products(n):
    return json.dumps({"products": [
        {"title": f"Mouse {i}", "handle": f"mouse-{i}", "vendor": "Brand",
         "variants": [{"price": f"{50 + i}.00", "compare_at_price": f"{80 + i}.00", "available": i % 2 == 0},
                      {"price": f"{60 + i}.00", "compare_at_price": None, "available": False}],
         "images": [{"src": f"https://cdn.example/m{i}.jpg"}]} for i in range(n)]})


def test_platform_listing_json_is_found_from_the_page_markup_only():
    assert (platform_feeds.listing_json_url(SHOPIFY_PAGE, "https://shop.example/collections/deals")
            == "https://shop.example/collections/deals/products.json?limit=250")
    assert (platform_feeds.listing_json_url(SHOPIFY_PAGE, "https://shop.example/en-au/collections/deals/")
            == "https://shop.example/en-au/collections/deals/products.json?limit=250")
    # Not the platform, not a collection, a tag-filtered or later page: nothing.
    assert platform_feeds.listing_json_url("<html>collections</html>", "https://shop.example/collections/deals") is None
    assert platform_feeds.listing_json_url(SHOPIFY_PAGE, "https://shop.example/products/mouse-1") is None
    assert platform_feeds.listing_json_url(SHOPIFY_PAGE, "https://shop.example/collections/deals/red") is None
    assert platform_feeds.listing_json_url(SHOPIFY_PAGE, "https://shop.example/collections/deals?page=2") is None


def test_platform_items_carry_price_was_price_and_availability():
    items = platform_feeds.items(json.loads(products(2)), "https://shop.example/collections/deals", SHOPIFY_PAGE)
    assert items[0] == {"name": "Mouse 0", "url": "https://shop.example/products/mouse-0", "price": 50,
                        "was_price": 80, "currency": "AUD", "availability": "in_stock",
                        "image": "https://cdn.example/m0.jpg"}
    # No variant available: the cheapest is still the price, and it is out of stock.
    assert items[1]["price"] == 51 and items[1]["availability"] == "out_of_stock"
    assert platform_feeds.items({"product": {}}, "https://shop.example/collections/deals") is None


@pytest.mark.parametrize("count", [12, 0])
async def test_a_platform_collection_reads_its_items_from_the_platform_json(tmp_path, monkeypatch, count):
    page_url = "https://shop.example/collections/deals"
    site = fixed("http", pages={page_url: SHOPIFY_PAGE,
                                page_url + "/products.json?limit=250": products(count)})
    registry_of(monkeypatch, site)
    async with Runtime(tmp_path) as web:
        result = await web.read(page_url, policy_overrides={**QUIET, "auto_items": True})
        plain = await web.read(page_url, policy_overrides=QUIET)
    receipt = result["receipt"]
    assert receipt["platform_items"]["used"] and receipt["platform_items"]["platform"] == "shopify"
    assert len(result["items"]) == count and result["items_quality"]["source"] == "platform_json"
    assert "auto_module" not in result
    if count:
        assert result["items"][3]["url"] == "https://shop.example/products/mouse-3"
        assert "no_items" not in receipt["quality"]["flags"]
    else:
        # The store lists nothing here: right page, but not a listing to trust as good.
        assert "empty_listing" in receipt["quality"]["flags"] and receipt["quality"]["grade"] != "good"
    # Without items asked for, no extra read.
    assert "platform_items" not in plain["receipt"]
    assert sum(url.endswith("products.json?limit=250") for url in site.calls) == 1


async def test_a_platform_json_that_fails_leaves_the_page_items(tmp_path, monkeypatch):
    page_url = "https://shop.example/collections/deals"
    registry_of(monkeypatch, fixed("http", pages={page_url: SHOPIFY_PAGE}))
    async with Runtime(tmp_path) as web:
        result = await web.read(page_url, policy_overrides={**QUIET, "auto_items": True})
    assert result["receipt"]["platform_items"]["used"] is False
    assert result["receipt"]["platform_items"]["failure"] == "NOT_FOUND"
    assert result["receipt"]["status"] == "observed"


# ---- connection failures say whose they are ----

def test_dns_and_tls_failures_name_the_site_not_the_tool():
    assert "DNS" in _connection_failure_message(OSError("[Errno -2] Name or service not known"), "x")
    assert "TLS" in _connection_failure_message(
        OSError("[SSL: TLSV1_ALERT_INTERNAL_ERROR] tlsv1 alert internal error (_ssl.c:1010)"), "x")
    assert "TLS" in _connection_failure_message(OSError("net::ERR_SSL_PROTOCOL_ERROR at https://a/"), "x")
    assert _connection_failure_message(OSError("connection reset"), "fallback") == "fallback"


def test_a_text_reader_markdown_still_shows_the_platform():
    markdown = "".join(f"![p{n}](https://shop.example/cdn/shop/files/p{n}.jpg) Mouse {n}\n" for n in range(4))
    assert platform_feeds.platform(markdown) == "shopify"
    assert platform_feeds.platform("![logo](https://cdn.shopify.com/s/files/1/logo.png)") is None


# ---- a listing that says it is empty is not a good read of a listing ----

def test_a_listing_that_says_it_has_no_products_is_flagged():
    from frankensurf.completeness import assess, quality
    page = ("<html><title>Deals</title><body>" + CHROME + "<h1>Deals</h1><p>No products available.</p>"
            + "".join(f'<a href="/products/top-seller-{n}">Top seller {n} $1{n}9.00</a>' for n in range(8))
            + "</body></html>")
    result = {"content": page, "text": "Deals " + "Department Cart Help Stores " * 80
              + " No products available. " + " ".join(f"Top seller {n} $1{n}9.00" for n in range(8)),
              "content_type": "text/html", "url": "https://shop.example/collections/deals"}
    graded = quality(assess("https://shop.example/collections/deals", result))
    assert "empty_listing" in graded["flags"] and graded["grade"] == "partial"
    assert graded["empty_listing"] == "No products available"
    full = {**result, "text": result["text"].replace("No products available.", "24 products")}
    assert "empty_listing" not in quality(assess("https://shop.example/collections/deals", full))["flags"]


async def test_a_templated_grid_page_is_kept_rendered_when_the_render_shows_the_prices(tmp_path, monkeypatch):
    import httpx
    from frankensurf import experimental
    monkeypatch.setattr(experimental, "installed", lambda provider: False)
    registry = providers.DEFAULT_PROVIDERS.clone()
    for identifier in ("browser_use", "crawl4ai"):
        registry.enable(identifier, False)
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    menus = "<p>" + "Laptops Monitors Gaming mice Keyboards Headsets Storage Cables Networking deals " * 120 + "</p>"
    http_page = TEMPLATED.replace(CHROME, menus)
    # The grid adds less than half again as much text, but dozens of prices.
    grid = "".join(f"<div class='hit'><a href='/p/{n}'>Monitor {n}</a> ${n + 100}.00 ${n + 200}.00</div>"
                   for n in range(30))
    rendered = http_page.replace("<div id='hits'></div>", f"<div id='hits'>{grid}</div>")

    async def browser(url, policy, provider):
        return {"url": url, "content": rendered, "raw": rendered.encode(), "content_type": "text/html",
                "http_status": 200, "headers": {}}
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text=http_page, headers={"content-type": "text/html"}))
    async with Runtime(tmp_path, transport=transport) as web:
        web._get_browser = browser
        result = await web.read("https://shop.example.com/clearance",
                                policy_overrides={"origin_route_hint_ttl_seconds": 0,
                                                  "origin_min_interval_seconds": 0})
    opinion = result["receipt"]["second_opinion"]
    assert opinion["text_chars"]["rendered"] < 1.5 * opinion["text_chars"]["http"]
    assert opinion["kept"] == "local" and opinion["prices"]["rendered"] >= 60
    assert "Monitor 7" in result["text"]
