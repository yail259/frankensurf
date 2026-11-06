"""A caller's own fields on listing items (synthetic fixtures only: a made-up
job board, events page and shop)."""
import pytest

from frankensurf.module_discovery import discover
from frankensurf.record_schema import fields, fill, is_hint_schema, to_json_schema
from frankensurf.runtime import WebPolicy, _parse_builtin_content
from frankensurf.site_modules import SiteModule

JOBS = "https://jobs.example/search?q=widget"
EVENTS = "https://events.example/whats-on"
SHOP = "https://www.gadgetbarn.example/clearance"


def html_result(content, url):
    parsed = _parse_builtin_content(content, "text/html", url, None)
    return {"url": url, "content": content, "content_type": "text/html", **parsed}


def text_result(text, url):
    return {"url": url, "content": text, "text": text, "content_type": "text/markdown; charset=utf-8"}


def items_for(result, url, schema):
    found = discover(result, url, limit=1)
    assert found["drafts"], found
    return SiteModule.from_record(found["drafts"][0]["module"]).extract(result, url, schema=schema)["items"]


def job_board():
    companies = ["Acme Imaginary Co", "Bolt Fictional Ltd", "Cog Pretend Pty", "Dynamo Example Inc"]
    return "# Jobs\n\n" + "\n\n".join(
        f"### [Senior Widget Engineer {n}](https://jobs.example/job/{n})\n\n"
        f"Company: {companies[n]}\n\nSalary: ${120 + n},000 - ${140 + n},000\n\nLocation: Springfield, NSW\n\n"
        + ("Remote\n\n" if n % 2 == 0 else "")
        + f"Posted {n + 1} days ago" for n in range(4))


def test_hint_schema_on_a_job_board():
    schema = {"role": "title", "salary": "price", "location": "location", "remote": "boolean", "posted": "date"}
    items = items_for(text_result(job_board(), JOBS), JOBS, schema)
    assert len(items) == 4
    first = items[0]["extracted"]
    assert first["data"] == {"role": "Senior Widget Engineer 0", "salary": 120000, "location": "Springfield, NSW",
                             "remote": True, "posted": "1 days ago"}
    assert first["sources"] == {"role": "item.name", "salary": "item.price", "location": "item.location",
                                "remote": "text:Remote", "posted": "item.date"}
    assert first["missing"] == []
    # Nothing says the second job is remote: the field is missing, never guessed false.
    assert items[1]["extracted"]["missing"] == ["remote"]


def test_labels_fill_fields_the_detectors_do_not_know():
    schema = {"type": "object", "properties": {
        "employer": {"type": "string"},
        "pay": {"type": "number", "description": "yearly salary"},
        "headline": {"type": "string"}}}
    items = items_for(text_result(job_board(), JOBS), JOBS, schema)
    extracted = items[2]["extracted"]
    assert extracted["data"]["employer"] == "Cog Pretend Pty"
    assert extracted["sources"]["employer"] == "label:Company"
    assert extracted["data"]["pay"] == 122000 and extracted["data"]["headline"] == "Senior Widget Engineer 2"


def events_page():
    rows = []
    for n in range(5):
        free = n % 2 == 1
        rows.append(
            f'<article class="event-card"><h3><a href="/event/{n}-made-up-fair">Made-up Fair {n}</a></h3>'
            f'<p class="when">{12 + n} March 2027</p><p>Venue: Town Hall {n}, Springfield</p>'
            + ('<p class="tag">Free entry</p>' if free else f'<p>Tickets ${20 + n}</p>')
            + '</article>')
    return f"<html><body><main>{''.join(rows)}</main></body></html>"


def test_json_schema_subset_on_an_events_page():
    schema = {"type": "object", "properties": {
        "event": {"type": "string", "description": "the event's name"},
        "when": {"type": "string", "format": "date"},
        "venue": {"type": "string"},
        "ticket_price": {"type": "number"},
        "free": {"type": "boolean"}}}
    items = items_for(html_result(events_page(), EVENTS), EVENTS, schema)
    assert len(items) == 5
    paid, free = items[0]["extracted"], items[1]["extracted"]
    assert paid["data"] == {"event": "Made-up Fair 0", "when": "12 March 2027", "venue": "Town Hall 0, Springfield",
                            "ticket_price": 20}
    assert paid["missing"] == ["free"] and paid["sources"]["venue"] == "label:Venue"
    assert free["data"]["free"] is True and free["sources"]["free"] == "text:Free entry"
    assert "ticket_price" in free["missing"]


def shop_page():
    rows = []
    for n in range(5):
        rows.append(
            f'<div class="product-card"><a href="/product/{n}-flux-lamp"><h2>Flux Lamp {n}</h2></a>'
            f'<span class="product-price">${40 + n}.00</span><s>${60 + n}.00</s>'
            f'<p>Brand: Lumen Imaginary</p><p>{"SOLD OUT" if n == 3 else "In stock"}</p></div>')
    return f"<html><body><main>{''.join(rows)}</main></body></html>"


def test_a_shop_with_was_prices_brands_and_stock():
    schema = {"product": "title", "cost": "price", "was": "price", "brand": "text", "in_stock": "boolean"}
    items = items_for(html_result(shop_page(), SHOP), SHOP, schema)
    data = items[0]["extracted"]["data"]
    assert data == {"product": "Flux Lamp 0", "cost": 40, "was": 60, "brand": "Lumen Imaginary", "in_stock": True}
    assert items[0]["extracted"]["sources"]["was"] == "item.was_price"
    assert items[3]["extracted"]["data"]["in_stock"] is False


def test_schema_forms_and_hints():
    hints = {"role": "title", "salary": "price"}
    assert is_hint_schema(hints) and not is_hint_schema(to_json_schema(hints))
    assert to_json_schema(hints)["properties"]["salary"] == {"type": "number", "x-hint": "price"}
    with pytest.raises(ValueError):
        to_json_schema({"role": "job-title"})
    inferred = {field["name"]: field["hint"] for field in fields({"properties": {
        "listed": {"type": "string"}, "city": {"type": "string"}, "fee": {"type": "number"},
        "notes": {"type": "string"}, "open": {"type": "boolean"}}})}
    assert inferred == {"listed": "date", "city": "location", "fee": "price", "notes": "text", "open": "boolean"}
    # The policy takes the small form and keeps a JSON Schema, so page-level extraction reads it too.
    assert WebPolicy(extract_schema=hints).extract_schema["properties"]["role"]["x-hint"] == "title"


def test_the_model_fallback_hook_is_off_unless_given():
    schema_fields = fields({"mood": "text"})
    item = {"name": "Flux Lamp", "url": "https://www.gadgetbarn.example/product/1"}
    assert fill(schema_fields, item, "Flux Lamp")["missing"] == ["mood"]
    assert fill(fields({"remote": "boolean"}), item, "Flux Lamp\nNot remote")["missing"] == ["remote"]
    asked = []

    def fallback(missing, record, text):
        asked.append([field["name"] for field in missing])
        return {"mood": "calm"}
    filled = fill(schema_fields, item, "Flux Lamp", model_fallback=fallback)
    assert asked == [["mood"]] and filled["data"] == {"mood": "calm"} and filled["sources"] == {"mood": "model"}


async def test_read_items_with_a_schema(tmp_path, monkeypatch):
    from frankensurf import providers
    from frankensurf.providers import ProviderManifest, ProviderRegistry
    from frankensurf.runtime import Runtime

    class Fixture:
        manifest = ProviderManifest("fixture", "1")

        def available(self, configured):
            return True

        async def acquire(self, request, services):
            return {"url": request.url, "content": job_board(), "content_type": "text/markdown", "http_status": 200}
    registry = ProviderRegistry()
    registry.register(Fixture())
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    overrides = {"origin_route_hint_ttl_seconds": 0, "origin_min_interval_seconds": 0,
                 "second_opinion_text_chars": 0, "completeness_escalation": False, "auto_items": True,
                 "extract_schema": {"role": "title", "salary": "price", "remote": "boolean"}}
    async with Runtime(tmp_path) as web:
        result = await web.read(JOBS, policy_overrides=overrides)
    assert result["items"][0]["extracted"]["data"]["salary"] == 120000
    assert result["items"][1]["extracted"]["missing"] == ["remote"]
    assert result["receipt"]["auto_items"]["schema"] == {"fields": 3, "filled": 10, "of": 12}
