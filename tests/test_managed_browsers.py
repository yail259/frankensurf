"""T2 managed browsers: session lifecycle, cost, key hygiene and fallback order."""
import json

import pytest

from frankensurf import managed_browsers
from frankensurf.managed_browsers import (
    BrowserbaseProvider,
    BrowserlessProvider,
    HyperbrowserProvider,
    SteelCloudProvider,
)
from frankensurf.providers import DEFAULT_PROVIDERS, ProviderRequest
from frankensurf.runtime import WebFailure, WebPolicy, _navigation_failure

URL = "https://shop.example.com/item/1"
HTML = "<html><body>" + "<p>Item with price $90</p>" * 20 + "</body></html>"


@pytest.fixture
def calls(monkeypatch):
    seen = []

    async def call(method, url, policy, *, headers=None, params=None, json_body=None):
        seen.append((method, url, json_body))
        if url.endswith("/v1/sessions") or url.endswith("/api/session") or url.endswith("browserbase.com/v1/sessions"):
            body = {"id": "s1", "connectUrl": "wss://connect.browserbase.example/s1",
                    "wsEndpoint": "wss://hb.example/s1"}
            return 200, {}, json.dumps(body).encode()
        return 200, {}, b"{}"

    async def render(endpoint, url, policy, service):
        seen.append(("RENDER", endpoint, None))
        return {"url": url, "content": HTML, "raw": HTML.encode(),
                "content_type": "text/html; rendered=1", "http_status": 200, "headers": {}}

    monkeypatch.setattr(managed_browsers, "_call", call)
    monkeypatch.setattr(managed_browsers, "_render", render)
    return seen


def request(**policy):
    return ProviderRequest(URL, WebPolicy(allow_paid_fallbacks=True, **policy))


def test_unavailable_until_the_key_exists(monkeypatch):
    monkeypatch.delenv("FRANKENSURF_BROWSERBASE_API_KEY", raising=False)
    monkeypatch.setattr("frankensurf.hosted_providers._env_file", lambda: {})
    monkeypatch.setattr("frankensurf.hosted_providers._services_file", lambda: {})
    assert BrowserbaseProvider().available(()) is False
    monkeypatch.setenv("FRANKENSURF_BROWSERBASE_API_KEY", "bb-key")
    assert BrowserbaseProvider().available(()) is True


async def test_browserbase_session_renders_releases_and_bills_the_minimum(calls, monkeypatch):
    monkeypatch.setenv("FRANKENSURF_BROWSERBASE_API_KEY", "bb-key")
    monkeypatch.setenv("FRANKENSURF_BROWSERBASE_PROJECT_ID", "proj")
    page = await BrowserbaseProvider().acquire(request(), None)
    assert page["content"] == HTML and page["http_status"] == 200
    assert calls[0] == ("POST", BrowserbaseProvider.API, {"projectId": "proj"})
    assert calls[1] == ("RENDER", "wss://connect.browserbase.example/s1", None)
    assert calls[2][1].endswith("/v1/sessions/s1") and calls[2][2]["status"] == "REQUEST_RELEASE"
    assert page["cost_usd"] == round(60 / 3600 * 0.12, 6)


async def test_session_is_released_when_the_render_fails(calls, monkeypatch):
    monkeypatch.setenv("FRANKENSURF_HYPERBROWSER_API_KEY", "hb-key")

    async def broken(endpoint, url, policy, service):
        raise WebFailure("PROVIDER_DOWN", service + " browser connection failed")
    monkeypatch.setattr(managed_browsers, "_render", broken)
    with pytest.raises(WebFailure) as failure:
        await HyperbrowserProvider().acquire(request(), None)
    assert failure.value.code == "PROVIDER_DOWN" and "hb-key" not in str(failure.value)
    assert calls[-1][0] == "PUT" and calls[-1][1].endswith("/api/session/s1/stop")


async def test_cost_cap_refuses_before_opening_a_session(calls, monkeypatch):
    monkeypatch.setenv("FRANKENSURF_BROWSERBASE_API_KEY", "bb-key")
    with pytest.raises(WebFailure) as failure:
        await BrowserbaseProvider().acquire(request(max_cost_usd=0.0001), None)
    assert failure.value.code == "BUDGET_EXHAUSTED" and calls == []


async def test_target_status_from_the_rendered_page_is_a_typed_failure(calls, monkeypatch):
    monkeypatch.setenv("FRANKENSURF_BROWSERLESS_API_KEY", "bl-key")

    async def forbidden(endpoint, url, policy, service):
        assert endpoint == BrowserlessProvider.CONNECT + "?token=bl-key"
        return {"url": url, "content": "denied", "raw": b"denied",
                "content_type": "text/html", "http_status": 403, "headers": {}}
    monkeypatch.setattr(managed_browsers, "_render", forbidden)
    with pytest.raises(WebFailure) as failure:
        await BrowserlessProvider().acquire(request(), None)
    assert failure.value.code == "BLOCKED"


async def test_steel_cloud_adds_the_key_to_the_connect_url_and_supports_self_hosted(calls, monkeypatch):
    monkeypatch.setenv("FRANKENSURF_STEEL_API_KEY", "st-key")
    await SteelCloudProvider().acquire(request(), None)
    assert calls[1][1] == "wss://connect.steel.dev?sessionId=s1&apiKey=st-key"
    assert calls[2][1] == "https://api.steel.dev/v1/sessions/s1/release"
    calls.clear()
    monkeypatch.setenv("FRANKENSURF_STEEL_CLOUD_URL", "http://127.0.0.1:3300")
    await SteelCloudProvider().acquire(request(), None)
    assert calls[0][1] == "http://127.0.0.1:3300/v1/sessions"
    assert calls[1][1] == "ws://127.0.0.1:3300/?sessionId=s1"


def test_managed_browsers_come_after_jina_and_before_unblockers(monkeypatch):
    for name in ("BROWSERBASE", "FIRECRAWL", "ZENROWS"):
        monkeypatch.setenv("FRANKENSURF_" + name + "_API_KEY", "k")
    order = DEFAULT_PROVIDERS.candidates(WebPolicy(allow_paid_fallbacks=True))
    assert order.index("jina_reader") < order.index("browserbase") < order.index("firecrawl")
    assert order.index("browserbase") < order.index("zenrows")
    assert "hyperbrowser" not in order


def test_chromium_block_errors_are_the_targets_answer():
    assert _navigation_failure("Page.goto: net::ERR_HTTP_RESPONSE_CODE_FAILURE at x") == "BLOCKED"
    assert _navigation_failure("net::ERR_NAME_NOT_RESOLVED") == "NOT_FOUND"
    assert _navigation_failure("Target closed") is None
