"""Site modules: agent-authored per-site knowledge, as data. Synthetic fixtures only."""
import copy
import json
import os
import stat

import pytest

from frankensurf import providers
from frankensurf.cli import parse_args
from frankensurf.providers import ProviderManifest, ProviderRegistry
from frankensurf.site_modules import SiteModule, SiteModuleError, SiteModuleRegistry
from frankensurf.runtime import Runtime

ORIGIN = "https://auction.example.com"
# Results embedded as JSON after a marker (like a search index's hits), cards
# rendered without links, and listing URLs only in a JSON-LD ItemList.
HITS = [{"title": f"Mirrorless camera body {n}", "lot": f"LOT{n:04d}", "ends": f"2026-10-1{n}T10:00:00Z",
         "bids": n, "price": f"A${900 + n},000"} for n in range(6)]
PAGE = ("<html><head><title>Search</title>"
        '<script type="application/ld+json">' + json.dumps({"@type": "ItemList", "itemListElement": [
            {"@type": "ListItem", "position": n + 1, "url": f"{ORIGIN}/lot/{n:08d}"} for n in range(6)]})
        + "</script></head><body><p>Home Auctions Help</p>"
        + "".join(f'<div class="card"><img src="/t/{n}.jpg"><h3>Mirrorless camera body {n}</h3></div>'
                  for n in range(6))
        + "<script>window.__RESULTS__ = " + json.dumps({"hits": HITS, "page": 1}) + ";</script>"
        + "</body></html>")
EMPTY_FEED = "<html><title>Search</title><body><p>Showing our featured lots</p></body></html>"

MODULE = {
    "schema": "frankensurf.site-module/v1",
    "id": "example.auction.search", "version": "1",
    "match": {"origin": ORIGIN, "path_pattern": "/search"},
    "templates": {"search": {"url": ORIGIN + "/search?q={query}&page={page}",
                             "params": {"query": "required", "page": {"default": 1}}}},
    "readiness": {"selector": ".card", "settle_ms": 1500},
    "sources": {"hits": {"kind": "embedded_json", "selector": "script", "marker": "window.__RESULTS__ =",
                         "path": "hits"},
                "list": {"kind": "jsonld", "type": "ItemList", "path": "itemListElement"},
                "cards": {"kind": "html", "item_selector": ".card",
                          "fields": {"image": {"selector": "img", "attribute": "src"},
                                     "title": {"selector": "h3"}}}},
    "items": {"from": "hits",
              "fields": {"title": "title", "lot": "lot", "ends_at": "ends", "bids": {"path": "bids", "type": "number"},
                         "price": {"path": "price", "type": "number"}},
              "join": {"from": "list", "on": "position", "fields": {"url": {"path": "url", "type": "url"}}}},
    "pagination": {"param": "page", "start": 1, "max_pages": 3},
    "invalid": {"text": ["showing our featured lots"]},
    "assertions": {"schema": "frankensurf.workload-assertions/v1",
                   "checks": [{"path": "items", "operator": "minimum_count", "value": 3},
                              {"path": "first.url", "operator": "nonempty"}]},
    "notes": "Search takes q; results are embedded JSON; URLs are in the ItemList.",
}


def module(**changes):
    raw = copy.deepcopy(MODULE)
    raw.update(changes)
    return raw


def test_a_module_validates_and_fingerprints():
    parsed = SiteModule.from_record(MODULE)
    assert parsed.matches(ORIGIN + "/search?q=camera") and not parsed.matches(ORIGIN + "/lot/1")
    assert parsed.fingerprint == SiteModule.from_record(parsed.record()).fingerprint
    assert parsed.policy_overrides() == {"content_ready_selector": ".card", "content_ready_timeout_seconds": 10,
                                         "settle_ms": 1500}


@pytest.mark.parametrize("bad", [
    {"policy_defaults": {"identity": "me"}},
    {"policy_defaults": {"allow_paid_fallbacks": True}},
    {"policy_defaults": {"provider": "firecrawl"}},
    {"templates": {"search": {"url": "https://elsewhere.example/search?q={query}", "params": {"query": "required"}}}},
    {"templates": {"search": {"url": ORIGIN + "/search?q={query}", "params": {}}}},
    {"match": {"origin": ORIGIN, "path_pattern": "/(a+)+$"}},
    {"match": {"origin": "http://auction.example.com", "path_pattern": "/search"}},
    {"sources": {"hits": {"kind": "python", "code": "print(1)"}}},
    {"surprise": True},
    {"notes": "x" * 70_000},
])
def test_modules_hold_data_never_authority_or_code(bad):
    with pytest.raises(SiteModuleError):
        SiteModule.from_record(module(**bad))


def test_extraction_joins_sources_and_reports_pagination():
    parsed = SiteModule.from_record(MODULE)
    output = parsed.extract({"url": ORIGIN + "/search?q=camera&page=1", "content": PAGE,
                             "structured": {"jsonld": [json.loads(PAGE.split('ld+json">')[1].split("</script>")[0])]}},
                            ORIGIN + "/search?q=camera&page=1", query_terms=["camera"])
    assert output["count"] == 6
    assert output["first"] == {"title": "Mirrorless camera body 0", "lot": "LOT0000",
                               "ends_at": "2026-10-10T10:00:00Z", "bids": 0, "price": 900000,
                               "url": ORIGIN + "/lot/00000000"}
    assert len(output["matching_query"]) == 6
    assert output["next_url"] == ORIGIN + "/search?q=camera&page=2"
    last = parsed.extract({"url": ORIGIN + "/search?q=camera&page=3", "content": ""}, ORIGIN + "/search?q=camera&page=3")
    assert "next_url" not in last


def test_next_flight_payloads_and_attribute_fallbacks():
    # Next.js app-router pages stream their data as JSON inside JS string chunks.
    payload = json.dumps({"results": [{"hits": [{"Title": "Camera one", "Url": "/lot/1"}]}]})
    half = len(payload) // 2
    page = "".join('<script>self.__next_f.push([1,%s])</script>' % json.dumps(part)
                   for part in ("5:" + payload[:half], payload[half:]))
    page += ('<div class="card"><img src="data:image/gif;base64,R0" data-src="/t/lazy.jpg"></div>'
             '<div class="card"><img src="/t/eager.jpg"></div>')
    parsed = SiteModule.from_record({
        "id": "flight", "version": "1", "match": {"origin": ORIGIN, "path_pattern": "/s"},
        "sources": {"hits": {"kind": "embedded_json", "decode": "next_flight", "marker": '"hits":'},
                    "cards": {"kind": "html", "item_selector": ".card",
                              "fields": {"image": {"selector": "img", "attribute": ["src", "data-src"]}}}},
        "items": {"from": "hits", "fields": {"title": "Title", "url": {"path": "Url", "type": "url"}},
                  "join": {"from": "cards", "on": "position", "fields": {"image": {"path": "image", "type": "url"}}}}})
    output = parsed.extract({"url": ORIGIN + "/s?q=camera", "content": page}, ORIGIN + "/s?q=camera")
    assert output["items"] == [{"title": "Camera one", "url": ORIGIN + "/lot/1", "image": ORIGIN + "/t/lazy.jpg"}]
    with pytest.raises(SiteModuleError):
        SiteModule.from_record({**MODULE, "sources": {"hits": {"kind": "embedded_json", "decode": "eval"}}})


def test_templates_encode_and_stay_on_origin():
    parsed = SiteModule.from_record(MODULE)
    assert parsed.build_url("search", {"query": "desk lamp & shade"}) == ORIGIN + "/search?q=desk+lamp+%26+shade&page=1"
    with pytest.raises(SiteModuleError):
        parsed.build_url("search", {})
    with pytest.raises(SiteModuleError):
        parsed.build_url("search", {"query": "x", "extra": "y"})


def test_the_registry_is_private_versioned_and_editable(tmp_path):
    registry = SiteModuleRegistry(tmp_path / "routes" / "site-modules.json")
    saved = registry.put(MODULE)
    assert saved["id"] == "example.auction.search" and saved["version"] == "1"
    assert stat.S_IMODE(os.stat(registry.path).st_mode) == 0o600
    registry.put(module(version="2", notes="v2"))
    assert [item["version"] for item in registry.inspect()] == ["2"]
    assert registry.match(ORIGIN + "/search?q=x").version == "2"
    registry.enable("example.auction.search", False)
    assert registry.match(ORIGIN + "/search?q=x") is None
    registry.delete("example.auction.search")
    assert registry.inspect() == []
    with pytest.raises(SiteModuleError):
        registry.get("example.auction.search")


def install(monkeypatch, *pages):
    """One provider per page, tried in order."""
    monkeypatch.setattr(Runtime, "completeness_enabled", True)
    registry = ProviderRegistry()
    for index, content in enumerate(pages):
        identifier = "p%d" % index

        class Plugin:
            def __init__(self, identifier=identifier, content=content):
                self.manifest = ProviderManifest(identifier, "1", rendering=index > 0)
                self.content = content

            def available(self, configured):
                return True

            async def acquire(self, request, services):
                page = {"url": request.url, "content": self.content, "content_type": "text/html",
                        "http_status": 200}
                if request.policy.content_ready_selector:
                    # Like real browsers: report on the module's readiness selector.
                    page["content_readiness"] = {
                        "status": "satisfied" if "card" in self.content else "timed_out",
                        "timeout_seconds": request.policy.content_ready_timeout_seconds}
                return page
        registry.register(Plugin())
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)


POLICY = {"origin_route_hint_ttl_seconds": 0, "origin_min_interval_seconds": 0, "second_opinion_text_chars": 0,
          "completeness_ladder": ["p1"]}


async def test_a_saved_module_shapes_matching_reads(tmp_path, monkeypatch):
    install(monkeypatch, PAGE, PAGE)
    async with Runtime(tmp_path) as web:
        web.site_modules.put(MODULE)
        result = await web.read(ORIGIN + "/search?q=camera", policy_overrides=POLICY)
        plain = await web.read(ORIGIN + "/search?q=camera", policy_overrides={**POLICY, "freshness": "now"},
                               module=False)
    receipt = result["receipt"]
    assert receipt["status"] == "observed" and len(result["items"]) == 6
    assert result["items"][2]["url"] == ORIGIN + "/lot/00000002"
    assert receipt["module"]["id"] == "example.auction.search" and receipt["module"]["status"] == "passed"
    assert receipt["module"]["source"] == "matched" and result["next_url"].endswith("page=2")
    # The page has few item links, but the module's assertions pass: no escalation.
    assert receipt["completeness"]["module"] == "passed" and "escalations" not in receipt["completeness"]
    assert "items" not in plain and "module" not in plain["receipt"]


async def test_module_invalid_markers_stop_the_read(tmp_path, monkeypatch):
    install(monkeypatch, EMPTY_FEED, EMPTY_FEED)
    async with Runtime(tmp_path) as web:
        web.site_modules.put(MODULE)
        result = await web.read(ORIGIN + "/search?q=camera", policy_overrides=POLICY)
    receipt = result["receipt"]
    assert receipt["status"] == "failed" and receipt["failure"]["code"] == "NOT_FOUND"
    assert receipt["module"]["status"] == "invalid"


async def test_named_and_override_modules(tmp_path, monkeypatch):
    install(monkeypatch, PAGE, PAGE)
    async with Runtime(tmp_path) as web:
        web.site_modules.put(MODULE)
        wrong = await web.read("https://other.example.com/search?q=x", policy_overrides=POLICY,
                               module="example.auction.search")
        once = await web.read(ORIGIN + "/search?q=camera", policy_overrides={**POLICY, "freshness": "now"},
                              module_override=module(id="trial", items={"from": "hits", "fields": {"lot": "lot"}}))
        templated = await web.read_template("example.auction.search", "search", {"query": "camera"},
                                            policy_overrides={**POLICY, "freshness": "now"})
    assert wrong["receipt"]["status"] == "failed" and wrong["receipt"]["failure"]["code"] == "POLICY_DENIED"
    assert once["items"][0] == {"lot": "LOT0000"}
    assert once["receipt"]["module"]["source"] == "override"
    assert templated["receipt"]["requested_url"] == ORIGIN + "/search?q=camera&page=1"
    assert templated["receipt"]["module"]["source"] == "named"


async def test_failing_assertions_are_reported_not_hidden(tmp_path, monkeypatch):
    moved = PAGE.replace("window.__RESULTS__", "window.__OTHER__")
    install(monkeypatch, moved, moved)
    async with Runtime(tmp_path) as web:
        web.site_modules.put(MODULE)
        result = await web.read(ORIGIN + "/search?q=camera",
                                policy_overrides={**POLICY, "completeness_ladder": []})
    assert result["receipt"]["module"]["status"] == "failed" and result["items"] == []


def test_cli_module_commands_parse():
    assert parse_args(["module", "list"]).urls == ["list"]
    assert parse_args(["module", "add", "m.json"]).urls == ["add", "m.json"]
    args = parse_args(["read-template", "example.auction.search", "search", "--param", "query=lamp"])
    assert args.param == ["query=lamp"]
    with pytest.raises(SystemExit):
        parse_args(["module", "explode", "x"])
