"""Structural completeness: a page without its content escalates to a stronger provider."""
import asyncio
import pytest

from frankensurf import providers
from frankensurf.completeness import assess, item_links, page_kind
from frankensurf.providers import ProviderManifest, ProviderRegistry
from frankensurf.runtime import Runtime, WebFailure, WebPolicy

SEARCH = "https://shop.example.com/search?q=lamp"
CHROME = "<p>Home Shop Help Sign in Cart Stores Gift cards Delivery Returns Contact us</p>" * 8
SHELL = "<html><title>Search</title><body>" + CHROME + "<div id='results'></div></body></html>"
RESULTS = ("<html><title>Search</title><body>" + CHROME
           + "".join(f'<a href="/product/brass-desk-lamp-{n}/SKU{n:06d}">Lamp {n} $49</a>' for n in range(12))
           + "</body></html>")


def test_page_kinds_and_item_links():
    assert page_kind(SEARCH) == "search"
    assert page_kind("https://www.ebay.com.au/itm/287624232775") == "item"
    assert page_kind("https://docs.python.org/3/library/asyncio.html") == "page"
    assert item_links(RESULTS, SEARCH) == 12
    assert item_links('<a href="/help">x</a><a href="/about">y</a><a href="https://other.example/p/1">z</a>', SEARCH) == 0
    assert item_links('<link href="/assets/app-123456.css"><a href="/legal/modern-slavery-statement-2025">x</a>'
                      '<a href="/rooms/">y</a>', SEARCH) == 0


def test_assess_uses_structure_not_the_query():
    shell = assess(SEARCH, {"content": SHELL, "text": "Search " + "Home Shop Help " * 60 + "lamp lamp lamp",
                            "content_type": "text/html"})
    assert shell["complete"] is False and shell["item_links"] == 0
    full = assess(SEARCH, {"content": RESULTS, "text": "Lamp $49 " * 12, "content_type": "text/html"})
    assert full["complete"] and full["score"] > shell["score"]
    assert assess(SEARCH, {"content": "{}", "text": "", "content_type": "application/json"})["complete"]


def plugin(identifier, content, failure=None, **manifest):
    class Plugin:
        calls = 0

        def __init__(self):
            self.manifest = ProviderManifest(identifier, "1", **manifest)

        def available(self, configured):
            return True

        async def acquire(self, request, services):
            type(self).calls += 1
            if failure:
                raise WebFailure(failure, "fixture")
            return {"url": request.url, "content": content, "content_type": "text/html", "http_status": 200}
    return Plugin()


@pytest.fixture
def escalating(monkeypatch):
    monkeypatch.setattr(Runtime, "completeness_enabled", True)

    def install(*items):
        registry = ProviderRegistry()
        for item in items:
            registry.register(item)
        monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    return install


POLICY = {"origin_route_hint_ttl_seconds": 0, "origin_min_interval_seconds": 0,
          "second_opinion_text_chars": 0}


async def test_incomplete_page_escalates_along_the_ladder_and_keeps_the_best(tmp_path, escalating):
    cheap = plugin("cheap", SHELL)
    blocked = plugin("strong_blocked", "", failure="BLOCKED", rendering=True)
    strong = plugin("strong", RESULTS, rendering=True)
    escalating(cheap, blocked, strong)
    async with Runtime(tmp_path) as web:
        result = await web.read(SEARCH, policy_overrides={
            **POLICY, "completeness_ladder": ["strong_blocked", "strong"]})
    record = result["receipt"]["completeness"]
    assert result["receipt"]["method"] == "strong" and record["complete"] is True
    assert [step["provider"] for step in record["escalations"]] == ["strong_blocked", "strong"]
    assert record["escalations"][0]["failure"] == "BLOCKED" and record["kept"] == "strong"
    assert "Lamp 3" in result["text"]


async def test_escalation_reads_wait_longer_for_rendered_content(tmp_path, escalating):
    seen = []

    class Recorder:
        manifest = providers.ProviderManifest("slow_render", "1", rendering=True)

        def available(self, configured):
            return True

        async def acquire(self, request, services):
            seen.append(request.policy.settle_ms)
            return {"url": request.url, "content": RESULTS, "content_type": "text/html", "http_status": 200}
    escalating(plugin("cheap", SHELL), Recorder())
    async with Runtime(tmp_path) as web:
        await web.read(SEARCH, policy_overrides={**POLICY, "completeness_ladder": ["slow_render"]})
    assert seen == [5000]


async def test_first_page_stands_when_nothing_better_and_paid_needs_a_grant(tmp_path, escalating):
    cheap = plugin("cheap", SHELL)
    paid = plugin("paid_strong", RESULTS, rendering=True, paid=True)
    escalating(cheap, paid)
    async with Runtime(tmp_path) as web:
        free = await web.read(SEARCH, policy_overrides={**POLICY, "completeness_ladder": ["paid_strong"]})
        granted = await web.read(SEARCH, policy_overrides={
            **POLICY, "completeness_ladder": ["paid_strong"], "allow_paid_fallbacks": True})
    assert free["receipt"]["method"] == "cheap" and free["receipt"]["completeness"]["escalations"] == []
    assert free["receipt"]["completeness"]["complete"] is False
    assert granted["receipt"]["method"] == "paid_strong"


async def test_explicit_provider_and_complete_pages_are_left_alone(tmp_path, escalating):
    cheap = plugin("cheap", SHELL)
    strong = plugin("strong", RESULTS, rendering=True)
    escalating(cheap, strong)
    async with Runtime(tmp_path) as web:
        explicit = await web.read(SEARCH, provider="cheap", policy_overrides=POLICY)
        off = await web.read(SEARCH, policy_overrides={**POLICY, "completeness_escalation": False})
    assert "completeness" not in explicit["receipt"] and "completeness" not in off["receipt"]
    assert type(strong).calls == 0
    with pytest.raises(ValueError):
        WebPolicy(completeness_max_extra_reads=-1)


def slow_plugin(identifier, content, delay, log, **manifest):
    class Slow:
        def __init__(self):
            self.manifest = ProviderManifest(identifier, "1", **manifest)

        def available(self, configured):
            return True

        async def acquire(self, request, services):
            log.append(("start", identifier))
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                log.append(("cancelled", identifier))
                raise
            log.append(("done", identifier))
            return {"url": request.url, "content": content, "content_type": "text/html", "http_status": 200}
    return Slow()


async def test_free_tools_race_and_the_first_complete_page_wins(tmp_path, escalating):
    log = []
    escalating(plugin("cheap", SHELL),
               slow_plugin("slow_free", RESULTS, 30, log, rendering=True),
               slow_plugin("fast_free", RESULTS, 0, log, rendering=True))
    async with Runtime(tmp_path) as web:
        result = await web.read(SEARCH, policy_overrides={
            **POLICY, "completeness_ladder": ["slow_free", "fast_free"]})
    record = result["receipt"]["completeness"]
    assert result["receipt"]["method"] == "fast_free" and record["complete"] is True
    assert ("start", "slow_free") in log and ("cancelled", "slow_free") in log
    assert ("done", "slow_free") not in log


async def test_paid_tools_never_race(tmp_path, escalating):
    log = []
    escalating(plugin("cheap", SHELL),
               slow_plugin("free_shell", SHELL, 0, log, rendering=True),
               slow_plugin("paid_one", RESULTS, 0, log, rendering=True, paid=True),
               slow_plugin("paid_two", RESULTS, 0, log, rendering=True, paid=True))
    async with Runtime(tmp_path) as web:
        result = await web.read(SEARCH, policy_overrides={
            **POLICY, "allow_paid_fallbacks": True,
            "completeness_ladder": ["free_shell", "paid_one", "paid_two"]})
    assert result["receipt"]["method"] == "paid_one"
    assert ("start", "paid_two") not in log
    with pytest.raises(ValueError):
        WebPolicy(completeness_parallel=0)


async def test_slow_read_is_hedged_with_the_first_free_ladder_tool(tmp_path, escalating):
    log = []
    escalating(slow_plugin("slow_main", RESULTS, 30, log),
               slow_plugin("fast_free", RESULTS, 0, log, rendering=True))
    async with Runtime(tmp_path) as web:
        result = await web.read(SEARCH, policy_overrides={
            **POLICY, "hedge_after_seconds": 0.2, "completeness_ladder": ["fast_free"]})
    receipt = result["receipt"]
    assert receipt["method"] == "fast_free" and receipt["hedge"]["won"] is True
    assert ("cancelled", "slow_main") in log


async def test_a_hedge_page_stands_in_when_the_main_read_fails(tmp_path, escalating):
    class Walled:
        def __init__(self, identifier, delay):
            self.manifest = ProviderManifest(identifier, "1", rendering=True)
            self.delay = delay

        def available(self, configured):
            return True

        async def acquire(self, request, services):
            await asyncio.sleep(self.delay)
            raise WebFailure("BLOCKED", "fixture wall")

    class Flaky:
        """A remote reader that gets the page once, then meets the wall."""
        manifest = ProviderManifest("flaky_free", "1", remote=True)
        calls = 0

        def available(self, configured):
            return True

        async def acquire(self, request, services):
            type(self).calls += 1
            if type(self).calls > 1:
                raise WebFailure("CAPTCHA", "fixture wall")
            return {"url": request.url, "content": SHELL, "content_type": "text/html", "http_status": 200}

    escalating(Walled("walled_a", 0.4), Walled("walled_b", 0.4), Flaky())
    async with Runtime(tmp_path) as web:
        result = await web.read(SEARCH, policy_overrides={
            **POLICY, "hedge_after_seconds": 0.2, "completeness_ladder": ["flaky_free", "walled_b"],
            "warm_up_on_wall": False})
    receipt = result["receipt"]
    assert receipt["status"] == "observed" and receipt["method"] == "flaky_free"
    assert receipt["hedge"]["won"] is True and receipt["hedge"]["after_failure"] == "CAPTCHA"
    assert receipt["hedge"]["walled"] == ["flaky_free", "walled_a", "walled_b"]
    # The tools the main read was walled on are not tried again while escalating.
    assert [step["provider"] for step in receipt["completeness"].get("escalations", [])] == []


async def test_an_incomplete_hedge_never_replaces_the_main_read(tmp_path, escalating):
    log = []
    escalating(slow_plugin("slow_main", RESULTS, 0.6, log),
               slow_plugin("shell_free", SHELL, 0, log, rendering=True))
    async with Runtime(tmp_path) as web:
        result = await web.read(SEARCH, policy_overrides={
            **POLICY, "hedge_after_seconds": 0.2, "completeness_ladder": ["shell_free"]})
        quick = await web.read(SEARCH + "&page=2", policy_overrides={
            **POLICY, "hedge_after_seconds": 0, "completeness_ladder": ["shell_free"]})
    assert result["receipt"]["method"] == "slow_main"
    assert result["receipt"]["hedge"] == {**result["receipt"]["hedge"], "provider": "shell_free", "won": False}
    assert "hedge" not in quick["receipt"]
    with pytest.raises(ValueError):
        WebPolicy(hedge_after_seconds=-1)


def test_zero_prices_and_unrendered_values_are_placeholders():
    item = "https://cruise.example.com/itinerary/10-day-islands/sydney/pp1"
    text = "Home Cruises Deals " * 30 + "From $ 0 * average per person, 0 person room. " + "Details " * 200
    page = {"content": "<html><body><p>" + text + "</p></body></html>", "text": text, "content_type": "text/html"}
    verdict = assess(item, page)
    assert verdict["complete"] is False and verdict["placeholder"] and verdict["prices"] == 0
    real = text.replace("From $ 0", "From $1,299")
    assert assess(item, {**page, "text": real, "content": "<p>" + real + "</p>"})["complete"]
    leaked = "Sailing {{ sailing.name }} departs {{ sailing.date }} for {{ price }} NaN " + "x " * 900
    assert assess(item, {**page, "text": leaked, "content": "<p>" + leaked + "</p>"})["placeholder"]


def test_a_page_of_menus_is_not_the_page():
    item = "https://cruise.example.com/cruise/cl27-aq09jan27"
    links = "".join(f'<li><a href="/ships/ship-{n}">Ship number {n} cruises and deals</a></li>' for n in range(300))
    content = f"<html><body><nav><ul>{links}</ul></nav><p>Loading</p></body></html>"
    text = " ".join(f"Ship number {n} cruises and deals" for n in range(300)) + " Loading"
    verdict = assess(item, {"content": content, "text": text, "content_type": "text/html"})
    assert verdict["complete"] is False and verdict["link_text_share"] > 0.9
    article = "<p>" + "A long article paragraph about the voyage. " * 80 + "</p>" + links[:3000]
    text = "A long article paragraph about the voyage. " * 80 + " Ship number 1 cruises and deals" * 10
    assert assess(item, {"content": article, "text": text, "content_type": "text/html"})["complete"]


def test_prices_behind_a_choice_are_named_not_escalated():
    item = "https://cruise.example.com/cruise/cl27-aq09jan27"
    text = ("Day 1 Sydney. Day 2 At Sea. " * 80 + "How many guests in this cabin? "
            "Select 1-4 guests for your first cabin to see prices.")
    verdict = assess(item, {"content": "<p>" + text + "</p>", "text": text, "content_type": "text/html"})
    assert verdict["complete"] is True
    assert verdict["needs_interaction"].startswith("Select 1-4 guests")


def test_query_keys_are_matched_however_they_are_spelled_and_echo_free_pages_are_off_query():
    from frankensurf.completeness import query_terms
    assert query_terms("https://x.example/search?search_term=sofa") == ["sofa"]
    assert query_terms("https://x.example/results.html?words=lamp") == ["lamp"]
    url = "https://x.example/search?search-term=sunscreen"
    links = "".join(f'<a href="/p/item-{n}/SKU{n:06d}">Body lotion {n} $9</a>' for n in range(12))
    links += '<a href="/p/sunscreen-guide/SKU999999">Guide</a>'
    page = {"content": "<html><body>" + links + "</body></html>",
            "text": " ".join(f"Body lotion {n} $9" for n in range(12)) + " Guide", "content_type": "text/html"}
    verdict = assess(url, page)
    assert verdict["off_query"] and verdict["complete"] is False


def test_prices_with_the_currency_after_the_amount_count():
    url = "https://shop.example.de/search_dir.html?sw=gitarre"
    text = " ".join(f"Gitarre {n} {n},99 €" for n in range(1, 6)) + " " + "Menü " * 300
    verdict = assess(url, {"content": "<p>" + text + "</p>", "text": text, "content_type": "text/html"})
    assert verdict["kind"] == "search" and verdict["prices"] == 5 and verdict["complete"]


def test_walls_are_named_in_other_languages():
    from frankensurf.runtime import _challenge_text
    assert _challenge_text("Zugriff verweigert / Access denied", "Aus Sicherheitsgründen")
    assert _challenge_text("Доступ ограничен: проблема с IP", "")
    assert _challenge_text("Vercel Security Checkpoint", "")
    assert not _challenge_text("Lamps - Shop", "Lamps for every room")


async def test_off_query_from_plain_http_gets_one_rendered_read(tmp_path, escalating):
    default_feed = ("<html><title>Search</title><body>" + CHROME
                    + "".join(f'<a href="/product/red-wine-{n}/SKU{n:06d}">Red wine {n} $20</a>' for n in range(12))
                    + "</body></html>")
    escalating(plugin("http", default_feed), plugin("local", RESULTS, rendering=True))
    async with Runtime(tmp_path) as web:
        result = await web.read(SEARCH, policy_overrides=POLICY)
    receipt = result["receipt"]
    assert receipt["method"] == "local"
    assert receipt["completeness"]["rendered_after_off_query"] == "http"
    assert "Lamp 3" in result["text"]


async def test_escalation_stops_when_two_browsers_read_the_same_page(tmp_path, escalating):
    log = []
    escalating(plugin("cheap", SHELL),
               slow_plugin("browser_a", SHELL, 0, log, rendering=True),
               slow_plugin("browser_b", SHELL, 0, log, rendering=True),
               slow_plugin("browser_c", RESULTS, 0, log, rendering=True))
    async with Runtime(tmp_path) as web:
        result = await web.read(SEARCH, policy_overrides={
            **POLICY, "completeness_ladder": ["browser_a", "browser_b", "browser_c"]})
    record = result["receipt"]["completeness"]
    assert record["agreed"] is True and ("start", "browser_c") not in log


def test_results_are_recognised_by_shape_when_their_links_mention_the_query():
    from frankensurf.completeness import result_group
    url = "https://pkgs.example.org/packages?q=json"
    links = "".join(f'<a href="/packages/json-tool-{n}">json-tool-{n}</a>' for n in range(10))
    menu = "".join(f'<a href="/docs/topic{n}">Topic {n}</a>' for n in range(12))
    assert result_group(links + menu, url, ["json"]) == 10
    assert result_group(menu, url, ["json"]) == 0
    verdict = assess(url, {"content": "<html><body>" + links + menu + "</body></html>",
                           "text": "json tool " * 200, "content_type": "text/html"})
    assert verdict["complete"] and verdict["item_links"] >= 10
