"""Thin pages move on to the next provider instead of counting as success."""
import httpx

from frankensurf import hosted_providers, providers
from frankensurf.providers import ProviderManifest, ProviderRegistry
from frankensurf.runtime import Runtime, WebPolicy

URL = "https://shop.example.com/search?q=camera"
REAL = "<html><title>Results</title><body>" + "<p>Camera listing with price $900.</p>" * 10 + "</body></html>"


def plugin(identifier, content, rendering=False):
    class Plugin:
        manifest = ProviderManifest(identifier, "1", rendering=rendering)
        calls = 0

        async def acquire(self, request, services):
            Plugin.calls += 1
            return {"url": request.url, "content": content, "content_type": "text/html", "http_status": 200}
    return Plugin()


def install(monkeypatch, *plugins):
    registry = ProviderRegistry()
    for item in plugins:
        registry.register(item)
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)


def tried(result):
    return [(a["provider"], a.get("failure")) for a in result["receipt"]["attempts"]]


async def test_empty_render_moves_on_to_the_next_provider(tmp_path, monkeypatch):
    empty = plugin("empty_render", "<html><title>CeX</title><body>CeX</body></html>", rendering=True)
    good = plugin("good_render", REAL, rendering=True)
    install(monkeypatch, empty, good)
    async with Runtime(tmp_path) as web:
        result = await web.read(URL, WebPolicy(origin_route_hint_ttl_seconds=0))
    assert tried(result) == [("empty_render", "EMPTY_PAGE"), ("good_render", None)]
    assert result["receipt"]["status"] == "observed"


async def test_empty_render_from_a_forced_provider_is_returned_as_is(tmp_path, monkeypatch):
    install(monkeypatch, plugin("empty_render", "<html><body>CeX</body></html>", rendering=True))
    async with Runtime(tmp_path) as web:
        result = await web.read(URL, WebPolicy(provider="empty_render"))
    assert result["receipt"]["status"] == "observed"


async def test_short_static_page_without_scripts_is_still_a_success(tmp_path):
    page = "<html><title>Example Domain</title><body><p>This domain is for use in documentation examples.</p></body></html>"
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=page, headers={"content-type": "text/html"}))
    async with Runtime(tmp_path, transport=transport) as web:
        result = await web.read("https://example.com/")
    assert result["receipt"]["status"] == "observed" and len(result["receipt"]["attempts"]) == 1


async def test_script_shell_under_200_characters_escalates(tmp_path):
    shell = '<html><title>Search</title><body><div id="app">Loading search results</div><script src="/app.js"></script></body></html>'
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=shell, headers={"content-type": "text/html"}))
    async with Runtime(tmp_path, transport=transport) as web:
        calls = []

        async def rendered(url, policy, provider):
            calls.append(provider)
            return {"url": url, "content": REAL, "raw": REAL.encode(), "content_type": "text/html",
                    "http_status": 200, "headers": {}}
        web._get_browser = rendered
        result = await web.read(URL, WebPolicy(origin_route_hint_ttl_seconds=0))
    assert result["receipt"]["attempts"][0]["failure"] == "VISUAL_REQUIRED"
    assert calls == ["local"] and result["receipt"]["status"] == "observed"


async def test_large_script_page_with_little_text_escalates(tmp_path):
    # Airbnb room pages: hundreds of KB of embedded script state, a few
    # hundred characters of visible text until the page renders.
    state = '<script>window.__STATE__=' + '{"k":"v"},' * 30_000 + '</script>'
    shell = "<html><title>Room</title><body>" + "<p>Header link</p>" * 40 + state + "</body></html>"
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=shell, headers={"content-type": "text/html"}))
    async with Runtime(tmp_path, transport=transport) as web:
        async def rendered(url, policy, provider):
            return {"url": url, "content": REAL, "raw": REAL.encode(), "content_type": "text/html",
                    "http_status": 200, "headers": {}}
        web._get_browser = rendered
        result = await web.read(URL, WebPolicy(origin_route_hint_ttl_seconds=0))
        assert result["receipt"]["attempts"][0]["failure"] == "VISUAL_REQUIRED"
        assert result["receipt"]["status"] == "observed"
        small = await web.read(URL, WebPolicy(origin_route_hint_ttl_seconds=0, html_shell_large_bytes=10**9))
    assert small["receipt"]["attempts"][0]["provider"] == "http" and len(small["receipt"]["attempts"]) == 1


async def test_unblockers_get_their_own_longer_deadline(tmp_path, monkeypatch):
    seen = []

    async def slow_but_fine(method, url, policy, **kwargs):
        seen.append(policy.timeout_seconds)
        return 200, {"content-type": "text/html", "x-request-cost": "0.025"}, REAL.encode()

    monkeypatch.setenv("FRANKENSURF_ZENROWS_API_KEY", "k")
    monkeypatch.setattr(hosted_providers, "_call", slow_but_fine)
    async with Runtime(tmp_path) as web:
        result = await web.read(URL, WebPolicy(provider="zenrows", allow_paid_fallbacks=True, timeout_seconds=25))
    assert result["receipt"]["status"] == "observed"
    assert seen == [90.0]
    assert WebPolicy().unblocker_timeout_seconds == 90.0
