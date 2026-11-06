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
