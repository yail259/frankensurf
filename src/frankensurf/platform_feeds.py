"""A shop platform's own public listing JSON, found from the page itself.

Many shops run on a hosted platform that publishes every collection as
public JSON at a fixed address beside the page. Shopify serves
/collections/<handle>/products.json on every storefront. A theme often
renders the product grid with a third-party script (search and
merchandising apps) that a plain read never sees, or that shows "no
products" to a browser it does not trust, while the platform's JSON lists
the collection as the store holds it.

Nothing here knows a site: the platform is recognised from markers its
storefront puts in every page, and the JSON is the platform's documented
public endpoint, read once and politely like any other page.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

# Markers Shopify's storefront renderer writes into every page it serves, and
# the asset paths it serves a store's images from (which survive in the
# markdown a text-only reader returns).
_SHOPIFY = re.compile(r"\bShopify\.shop\s*=|\bwindow\.ShopifyAnalytics\b|<meta[^>]+name=[\"']shopify-", re.I)
_SHOPIFY_ASSETS = re.compile(r"//cdn\.shopify\.com/s/files/|/cdn/shop/(?:files|products)/")
# /collections/<handle>, optionally under a locale prefix (/en-au/...). A
# filtered or tagged collection (/collections/<handle>/<tag>) is not the same
# list as the JSON, so it is left to the page.
_COLLECTION = re.compile(r"^(?P<prefix>(?:/[a-z]{2}(?:-[a-z0-9]{2,4})?)?)/collections/(?P<handle>[^/?#]+)/?$", re.I)
_CURRENCY = re.compile(r"\bShopify\.currency\s*=\s*\{[^}]*\"active\"\s*:\s*\"([A-Z]{3})\"")
LIMIT = 250


def platform(content: str) -> str | None:
    """The shop platform the page's own markup names, if any."""
    head = (content or "")[:3_000_000]
    if _SHOPIFY.search(head) or len(_SHOPIFY_ASSETS.findall(head)) >= 3:
        return "shopify"
    return None


def listing_json_url(content: str, url: str) -> str | None:
    """The platform's public JSON for the collection this page lists, or None."""
    if platform(content) != "shopify":
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    match = _COLLECTION.match(parsed.path or "")
    if not match or parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    page = (parse_qs(parsed.query).get("page") or ["1"])[0]
    if page not in ("", "1"):
        # The page lists a later slice than the JSON's first one.
        return None
    return (f"{parsed.scheme}://{parsed.netloc}{match['prefix']}/collections/"
            f"{match['handle']}/products.json?limit={LIMIT}")


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def items(data, page_url: str, content: str = "") -> list[dict] | None:
    """Items (name, url, price, was_price, currency, availability, image) from
    the platform's JSON, or None when it is not that JSON."""
    if not isinstance(data, dict) or not isinstance(data.get("products"), list):
        return None
    parsed = urlparse(page_url)
    match = _COLLECTION.match(parsed.path or "")
    base = f"{parsed.scheme}://{parsed.netloc}{match['prefix'] if match else ''}"
    currency = _CURRENCY.search(content or "")
    found = []
    for product in data["products"]:
        if not isinstance(product, dict) or not product.get("handle") or not product.get("title"):
            continue
        variants = [variant for variant in product.get("variants") or () if isinstance(variant, dict)]
        available = [variant for variant in variants if variant.get("available")]
        priced = [(price, variant) for variant in (available or variants)
                  if (price := _number(variant.get("price"))) is not None]
        item = {"name": " ".join(str(product["title"]).split()),
                "url": f"{base}/products/{product['handle']}"}
        if priced:
            price, variant = min(priced, key=lambda pair: pair[0])
            item["price"] = price
            was = _number(variant.get("compare_at_price"))
            if was is not None and was > price:
                item["was_price"] = was
            if currency:
                item["currency"] = currency.group(1)
        if variants and any("available" in variant for variant in variants):
            item["availability"] = "in_stock" if available else "out_of_stock"
        images = [image for image in product.get("images") or () if isinstance(image, dict) and image.get("src")]
        if images:
            item["image"] = images[0]["src"]
        found.append(item)
    return found
