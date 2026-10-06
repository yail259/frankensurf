"""A script-built plain-HTTP page gets a rendered second opinion."""
import httpx
import pytest

from frankensurf import experimental, providers
from frankensurf.runtime import Runtime, WebFailure, WebPolicy

URL = "https://shop.example.com/search?q=lamp"
CHROME = "<p>Home Shop Help Sign in Cart Stores Gift cards Delivery Returns Contact us</p>" * 6
HTTP_PAGE = "<html><title>Search</title><body>" + CHROME + '<div id="results"></div><script src="/app.js"></script></body></html>'
RESULTS = "<html><title>Search</title><body>" + CHROME + "<p>Desk lamp, brass, $49. In stock.</p>" * 60 + "</body></html>"


def transport():
    return httpx.MockTransport(lambda request: httpx.Response(200, text=HTTP_PAGE, headers={"content-type": "text/html"}))


def browser(content=RESULTS, failure=None):
    calls = []

    async def rendered(url, policy, provider):
        calls.append(provider)
        if failure:
            raise WebFailure(failure, "fixture")
        return {"url": url, "content": content, "raw": content.encode(), "content_type": "text/html",
                "http_status": 200, "headers": {}}
    return rendered, calls


POLICY = {"origin_route_hint_ttl_seconds": 0, "origin_min_interval_seconds": 0}


@pytest.fixture(autouse=True)
def no_isolated_browsers(monkeypatch):
    # Only the patched local browser renders; stealth workers stay out of it.
    monkeypatch.setattr(experimental, "installed", lambda provider: False)
    registry = providers.DEFAULT_PROVIDERS.clone()
    for identifier in ("browser_use", "crawl4ai"):
        registry.enable(identifier, False)
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)


async def test_rendered_page_with_clearly_more_content_wins(tmp_path):
    async with Runtime(tmp_path, transport=transport()) as web:
        web._get_browser, calls = browser()
        result = await web.read(URL, policy_overrides=POLICY)
    opinion = result["receipt"]["second_opinion"]
    assert result["receipt"]["method"] == "local" and calls == ["local"]
    assert opinion["kept"] == "local" and opinion["other"] == "http"
    assert opinion["text_chars"]["rendered"] > 1.5 * opinion["text_chars"]["http"]
    assert "Desk lamp" in result["text"]


async def test_http_page_stands_when_the_render_adds_little_or_fails(tmp_path):
    async with Runtime(tmp_path, transport=transport()) as web:
        web._get_browser, _ = browser(content=HTTP_PAGE.replace('<script src="/app.js"></script>', ""))
        same = await web.read(URL, policy_overrides=POLICY)
        web._get_browser, _ = browser(failure="BLOCKED")
        blocked = await web.read(URL, policy_overrides=POLICY)
    for result in (same, blocked):
        assert result["receipt"]["status"] == "observed" and result["receipt"]["method"] == "http"
        assert result["receipt"]["second_opinion"]["kept"] == "http"
    assert blocked["receipt"]["second_opinion"]["other_status"] == "failed"


async def test_no_second_opinion_when_disabled_explicit_or_long(tmp_path):
    async with Runtime(tmp_path, transport=transport()) as web:
        web._get_browser, calls = browser()
        off = await web.read(URL, policy_overrides={**POLICY, "second_opinion_text_chars": 0})
        explicit = await web.read(URL, provider="http", policy_overrides=POLICY)
        whole = await web.read(URL, WebPolicy(second_opinion_text_chars=100, origin_route_hint_ttl_seconds=0))
    assert calls == []
    assert all("second_opinion" not in r["receipt"] for r in (off, explicit, whole))
