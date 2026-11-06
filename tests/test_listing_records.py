"""Repeated records with typed fields on listing pages (synthetic fixtures only:
made-up shops, products and jobs)."""
from frankensurf.module_discovery import discover, items_quality
from frankensurf.records import detect, markdown_records, offer, parse_price
from frankensurf.runtime import _parse_builtin_content
from frankensurf.site_modules import SiteModule

SHOP = "https://www.gadgetbarn.example/clearance"


def html_result(content, url=SHOP):
    parsed = _parse_builtin_content(content, "text/html", url, None)
    return {"url": url, "content": content, "content_type": "text/html", **parsed}


def text_result(text, url=SHOP):
    return {"url": url, "content": text, "text": text, "content_type": "text/markdown; charset=utf-8"}


def extract(result, url=SHOP):
    found = discover(result, url, limit=1)
    assert found["drafts"], found
    module = found["drafts"][0]["module"]
    SiteModule.from_record(module)  # The draft is a valid, saveable module.
    return module, SiteModule.from_record(module).extract(result, url)["items"]


# ---- prices ----

def test_prices_need_a_currency_marker_and_read_split_markup():
    assert parse_price("Zapper M3PRO Wireless Mouse 4.3 out of 5") is None
    assert parse_price("$ 69 . 99") == 69.99
    assert parse_price("$\n69\n.\n99") == 69.99
    assert parse_price("39.99") == 39.99 and parse_price(12) == 12
    assert offer("A$1,249.50") == {"price": 1249.5, "currency": "AUD"}
    assert offer("US$34.51 (~A$49.62)") == {"price": 34.51, "currency": "USD"}
    assert offer("~A$49.62") == {"price": 49.62, "currency": "AUD"}
    assert offer("from $19") == {"price": 19}
    assert offer("$10 - $25") == {"price": 10, "price_max": 25}
    assert offer("no price here, model X200") == {}


def test_was_and_now_prices():
    assert offer("$129\nSave $30!\n$99") == {"price": 99, "was_price": 129}
    assert offer("$79 Save $10") == {"price": 79, "was_price": 89}
    assert offer("Now $45 was $60") == {"price": 45, "was_price": 60}
    assert offer("$39.99 RRP: $49.99") == {"price": 39.99, "was_price": 49.99}
    assert offer("Made-up Mouse $142.50 ($299.95 RRP) Delivered") == {"price": 142.5, "was_price": 299.95}
    # A card promotion is not a was price, and its amounts are not the price.
    assert offer("$39.99 Get $10.00 off $200.00 with Store Card") == {"price": 39.99}
    assert offer("$15 + $9.95 shipping") == {"price": 15}


def test_availability_and_stock():
    assert detect("in stock\n2 Unit(s)", ("availability", "stock_count")) == {"availability": "in_stock",
                                                                             "stock_count": 2}
    assert detect("SOLD OUT", ("availability",)) == {"availability": "out_of_stock"}
    assert detect("Only 3 left in stock.", ("availability", "stock_count")) == {"availability": "limited",
                                                                               "stock_count": 3}
    assert detect("expired\nWidget deal", ("availability",)) == {"availability": "expired"}


# ---- markdown cards (a text-only reader's view of a shop page) ----

def shop_markdown():
    menu = "\n".join(f"- [Category {n}](https://www.gadgetbarn.example/c/{n})" for n in range(10))
    cards = []
    stock = ["in stock", "2 Unit(s)", "SOLD OUT", "in stock", "in stock"]
    for n in range(5):
        url = f"https://www.gadgetbarn.example/product/{100 + n}-flux-widget-{n}"
        cards.append(
            f"[![Flux Widget Model {n}](https://img.gadgetbarn.example/{n}.jpg)]({url})\n\n---\n\n"
            f"[Flux Widget Model {n}]({url})\n\n"
            f"[Flux Widget Model {n}, a long blurb about the made-up widget and its features]({url})\n\n---\n\n"
            + (f"${120 + n}\n\nSave $20!\n\n${100 + n}\n\n" if n % 2 == 0 else f"${60 + n}\n\n")
            + f"{stock[n]}\n\nAdd to cart\n\n")
    footer = "[Privacy](https://www.gadgetbarn.example/privacy) [Contact](https://www.gadgetbarn.example/contact)"
    return f"# Gadget Barn\n\n[Cart $0](https://www.gadgetbarn.example/cart)\n\n{menu}\n\n" + "".join(cards) + footer


def test_markdown_cards_become_items():
    module, items = extract(text_result(shop_markdown()))
    assert module["sources"]["listing"] == {"kind": "markdown"}
    assert len(items) == 5
    assert items[0] == {"name": "Flux Widget Model 0",
                        "url": "https://www.gadgetbarn.example/product/100-flux-widget-0",
                        "price": 100, "image": "https://img.gadgetbarn.example/0.jpg", "was_price": 120,
                        "availability": "in_stock"}
    assert items[1]["price"] == 61 and "was_price" not in items[1]
    assert items[1]["availability"] == "in_stock" and items[1]["stock_count"] == 2
    assert items[2]["availability"] == "out_of_stock"


def test_menus_and_link_lists_are_not_records():
    menu = "\n".join(f"- [Category {n} with a longer label](https://www.gadgetbarn.example/c/{n})"
                     for n in range(12))
    assert markdown_records(menu, SHOP) == []


# ---- html rows ----

def test_split_price_markup_and_sponsored_rows():
    rows = []
    for n in range(6):
        rows.append(
            f'<div class="result-row"><div class="row-section"><h2><a href="/dp/ZX{n}">Zapper M{n}PRO Mouse</a></h2>'
            f'<span>4.{n} out of 5 stars</span></div>'
            f'<div class="row-section offer-price-block"><span class="item-price"><span class="sym">$</span>'
            f'<span class="whole">6{n}</span><span class="point">.</span><span class="frac">99</span></span>'
            f'<span class="item-price list-price">List: <s>$7{n}.99</s></span></div></div>')
    ad = ('<div class="result-row"><div class="row-section"><span>Sponsored</span>'
          '<h2><a href="https://ads.gadgetbarn.example/clk/?id=1">Shop Brand X now</a></h2></div>'
          '<div class="row-section"><span class="item-price">$5</span></div></div>')
    content = f"<html><body><main>{ad}{''.join(rows)}</main></body></html>"
    module, items = extract(html_result(content, "https://www.gadgetbarn.example/s?k=zapper"),
                            "https://www.gadgetbarn.example/s?k=zapper")
    assert module["items"]["fields"]["price"]["type"] == "price"
    assert len(items) == 6 and not any("Brand X" in item["name"] for item in items)
    assert items[3]["name"] == "Zapper M3PRO Mouse"
    assert items[3]["price"] == 63.99 and items[3]["was_price"] == 73.99 and items[3]["rating"] == 4.3


def test_deal_titles_carry_their_price_and_status():
    rows = "".join(
        f'<h2 class="deal-title">{"<span class=tag>expired</span> " if n == 1 else ""}'
        f'{"<span class=tag>out of stock</span> " if n == 2 else ""}'
        f'<a href="/node/{n}">Made-up Mouse {n} US<em>$3{n}.50</em> (~A<em>$5{n}.10</em>) Delivered @ Fake Store</a>'
        f'</h2>' for n in range(6))
    _, items = extract(html_result(f"<html><body>{rows}</body></html>", "https://deals.example/tag/mouse"),
                       "https://deals.example/tag/mouse")
    assert items[0]["price"] == 30.5 and items[0]["currency"] == "USD"
    assert items[1]["availability"] == "expired" and items[2]["availability"] == "out_of_stock"
    assert "availability" not in items[0]


# ---- not only shops ----

def test_a_job_board_yields_generic_records():
    companies = ["Acme Imaginary Co", "Bolt Fictional Ltd", "Cog Pretend Pty", "Dynamo Example Inc"]
    cards = "\n\n".join(
        f"### [Senior Widget Engineer {n}](https://jobs.example/job/{n})\n\n{companies[n]}\n\n"
        f"Springfield, NSW\n\nPosted {n + 1} days ago\n\nFull time"
        for n in range(4))
    text = "# Jobs\n\n- [Home](https://jobs.example/)\n- [Post a job](https://jobs.example/post)\n\n" + cards
    records = markdown_records(text, "https://jobs.example/search?q=widget")
    assert len(records) == 4
    assert records[1] == {"name": "Senior Widget Engineer 1", "url": "https://jobs.example/job/1",
                          "date": "2 days ago", "location": "Springfield, NSW",
                          "subtitle": "Bolt Fictional Ltd"}
    assert not any("price" in record for record in records)


# ---- quality ----

def test_items_quality_says_why():
    text = shop_markdown()
    result = text_result(text)
    _, items = extract(result)
    assert items_quality(items, result, SHOP)["grade"] == "good"
    unpriced = [{key: value for key, value in item.items() if key != "price"} for item in items]
    poor = items_quality(unpriced, result, SHOP)
    assert poor["grade"] == "poor" and "no price" in poor["reason"]
    empty = items_quality([], text_result("$10 $20 $30 $40 $50 $60 $70"), SHOP)
    assert empty["grade"] == "poor" and "looks like a listing" in empty["reason"]


# ---- several offers in one title ----

def test_each_offer_clause_pairs_its_own_prices():
    title = "Zapper Mouse from $129 Delivered, Glide Edition $159 (Was $259) @ Fake Store"
    # The title names both offers: the first leads, and the other's was price is not its own.
    assert offer(title, title=title) == {
        "price": 129, "offers": [{"label": "Zapper Mouse", "price": 129},
                                 {"label": "Glide Edition @ Fake Store", "price": 159, "was_price": 259}]}
    # A name that matches one clause picks it, with that clause's was price.
    picked = offer(title, title="Glide Edition Mouse")
    assert picked["price"] == 159 and picked["was_price"] == 259 and len(picked["offers"]) == 2
    assert offer("Zapper Mouse $49; Zapper Pad $19 (RRP $39)") == {
        "price": 49, "offers": [{"label": "Zapper Mouse", "price": 49},
                                {"label": "Zapper Pad", "price": 19, "was_price": 39}]}


def test_one_offer_stays_one_offer():
    # A was price after a comma, a price in another currency, a threshold and a unit price.
    assert offer("Flux Keyboard $1,299, was $1,599") == {"price": 1299, "was_price": 1599}
    assert offer("Flux Suite US $236 / A $360 (Was US $295), 20% off panels") == {
        "price": 236, "currency": "USD", "was_price": 295}
    assert offer("Flux Pad $9 + Delivery ($0 with Membership/ $59 Spend)") == {"price": 9}
    assert offer("Flux Beans $25 ($2.50/100g)") == {"price": 25}
    assert offer("Made-up Mouse $142.50 ($299.95 RRP) Delivered") == {"price": 142.5, "was_price": 299.95}


def test_multi_offer_deal_cards_keep_was_prices_apart():
    cards = "\n\n".join(
        f"## [Flux Widget {n} from ${100 + n} Delivered, Pro Kit ${150 + n} (Was ${250 + n}) @ Fake Store]"
        f"(https://deals.example/node/{n})\n\nPosted {n + 1} days ago" for n in range(4))
    records = markdown_records(cards, "https://deals.example/tag/widget")
    assert len(records) == 4
    assert records[0]["price"] == 100 and "was_price" not in records[0]
    assert records[0]["offers"][1] == {"label": "Pro Kit @ Fake Store", "price": 150, "was_price": 250}


def test_multi_offer_html_titles_split_over_markup():
    rows = "".join(
        f'<h2 class="deal-title"><a href="/node/{n}">Flux Widget {n} from <em>${100 + n}</em> Delivered, '
        f'Pro Kit <em>${150 + n}</em> (Was <em>${250 + n}</em>) @ Fake Store</a></h2>' for n in range(5))
    _, items = extract(html_result(f"<html><body>{rows}</body></html>", "https://deals.example/tag/widget"),
                       "https://deals.example/tag/widget")
    assert items[0]["price"] == 100 and "was_price" not in items[0]
    assert [offer["price"] for offer in items[0]["offers"]] == [100, 150]


def test_html_rows_that_are_only_links_are_navigation():
    tiles = "".join(f'<li class="range-tile"><a href="/range/{n}-flux-gadgets">Flux Gadgets Range {n}</a></li>'
                    for n in range(30))
    page = f"<html><body><ul>{tiles}</ul><p>{'Flux gadgets for every room. ' * 100}</p></body></html>"
    assert discover(html_result(page), SHOP)["drafts"] == []


def test_an_items_own_price_picks_its_offer():
    from frankensurf.records import enrich
    item = enrich({"name": "Pro Kit", "price": 150}, "Flux Widget from $100, Pro Kit $150 (Was $250)")
    assert item["price"] == 150 and item["was_price"] == 250
    item = enrich({"name": "Flux Widget", "price": 100}, "Flux Widget from $100, Pro Kit $150 (Was $250)")
    assert item["price"] == 100 and "was_price" not in item


# ---- confidence: menus and page text are not records ----

SERVICES = ("- [Gift cards](https://www.gadgetbarn.example/gift-cards)\n  from $10\n"
            "- [Warranty plans](https://www.gadgetbarn.example/warranty)\n  from $29\n"
            "- [Setup service](https://www.gadgetbarn.example/setup)\n  from $49\n"
            "- [Trade-in](https://www.gadgetbarn.example/trade-in)\n  from $5\n")


def test_prices_in_running_prose_are_not_card_fields():
    blurbs = [("repairs", "Book a repair online and we charge a flat rate of $80 for the health check of your computer."),
              ("pickup", "Our drivers collect your computer and we charge $110 for diagnostics and the new parts."),
              ("upgrades", "We upgrade your computer with new parts and charge $60 for the labour on top of parts.")]
    text = "# Gadget Barn\n\n" + "\n\n".join(
        f"- [{name.title()}](https://www.gadgetbarn.example/{name})\n\n{blurb}" for name, blurb in blurbs)
    assert markdown_records(text, SHOP) == []


def test_template_subtraction_drops_what_every_page_of_the_site_repeats():
    from frankensurf.records import SIBLINGS
    page = f"# Gadget Barn\n\n{SERVICES}\n## Clearance\n\nNothing on clearance today, check back soon.\n"
    sibling = f"# Gadget Barn\n\n{SERVICES}\n## About\n\nA made-up shop.\n"
    assert len(markdown_records(page, SHOP)) == 4  # Without a sibling page the menu looks like a listing.
    token = SIBLINGS.set(lambda url: [sibling])
    try:
        assert markdown_records(page, SHOP) == []
        # Real cards stay, even when the sibling page shows some of the same products.
        shop = shop_markdown()
        overlap = sibling + shop[shop.index("[![Flux Widget Model 0]"):shop.index("[![Flux Widget Model 2]")]
        SIBLINGS.set(lambda url: [overlap])
        assert len(markdown_records(shop, SHOP)) == 5
        SIBLINGS.set(lambda url: [])  # No sibling cached: the check is skipped.
        assert len(markdown_records(page, SHOP)) == 4
    finally:
        SIBLINGS.reset(token)


def test_short_link_lists_at_the_page_edge_with_no_numbers_are_navigation():
    offices = "\n".join(f"- [{city} office](https://jobs.example/offices/{city.lower()})\n\n  {city}, NSW\n"
                        for city in ("Springfield", "Shelbyville", "Ogdenville", "Brockway"))
    cards = "\n\n".join(
        f"### [Senior Widget Engineer {n}](https://jobs.example/job/{n})\n\nAcme Imaginary Co\n\n"
        f"Capital City, NSW\n\nPosted {n + 1} days ago\n\nFull time\n\nApply by email" for n in range(6))
    records = markdown_records(f"# Jobs\n\n## Our offices\n\n{offices}\n\n## Openings\n\n{cards}",
                               "https://jobs.example/search?q=widget")
    assert [record["url"] for record in records] == [f"https://jobs.example/job/{n}" for n in range(6)]


# ---- the item cap ----

def test_capped_items_say_how_many_the_page_holds():
    module, items = extract(text_result(shop_markdown()))
    capped = {**module, "items": {**module["items"], "limit": 2}}
    output = SiteModule.from_record(capped).extract(text_result(shop_markdown()), SHOP)
    assert output["count"] == 2 and output["truncated"] is True and output["total"] == 5
    assert "truncated" not in SiteModule.from_record(module).extract(text_result(shop_markdown()), SHOP)


async def test_read_max_items_and_cached_siblings(tmp_path, monkeypatch):
    import pytest
    from frankensurf import providers
    from frankensurf.providers import ProviderManifest, ProviderRegistry
    from frankensurf.runtime import Runtime, WebPolicy

    about, empty = "https://www.gadgetbarn.example/about", "https://www.gadgetbarn.example/empty"
    pages = {SHOP: shop_markdown(), about: f"# Gadget Barn\n\n{SERVICES}\nAbout us.\n",
             empty: f"# Gadget Barn\n\n{SERVICES}\nNothing here today.\n"}

    class Fixture:
        manifest = ProviderManifest("fixture", "1")

        def available(self, configured):
            return True

        async def acquire(self, request, services):
            return {"url": request.url, "content": pages[request.url], "content_type": "text/markdown",
                    "http_status": 200}
    registry = ProviderRegistry()
    registry.register(Fixture())
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    overrides = {"origin_route_hint_ttl_seconds": 0, "origin_min_interval_seconds": 0,
                 "second_opinion_text_chars": 0, "completeness_escalation": False, "auto_items": True}
    async with Runtime(tmp_path) as web:
        capped = await web.read(SHOP, policy_overrides={**overrides, "max_items": 3})
        assert web._sibling_texts(empty) == [pages[SHOP]]  # Only the cache: nothing is read for it.
        alone = await web.read(empty, policy_overrides=overrides)
        await web.read(about, policy_overrides=overrides)
        subtracted = await web.read(empty, policy_overrides=overrides)
    assert len(capped["items"]) == 3 and capped["items_truncated"] is True and capped["items_total"] == 5
    assert capped["receipt"]["auto_items"]["truncated"] is True
    assert len(alone["items"]) == 4  # The shop page shares no menu with it: the menu passes as items.
    assert "items" not in subtracted and subtracted["items_quality"]["count"] == 0
    with pytest.raises(ValueError):
        WebPolicy(max_items=0)
