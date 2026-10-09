"""Walls: a walled deep link gets one warm-up retry through the home page; site sessions persist."""
import time

import pytest

from frankensurf import providers
from frankensurf.providers import ProviderManifest, ProviderRegistry
from frankensurf.runtime import Runtime, WebFailure, WebPolicy

PAGE = "<html><title>Item</title><body>" + "<p>Product details and a price of $49.</p>" * 40 + "</body></html>"


def plugin(identifier, *, needs_entry=False, failure="BLOCKED", calls=None):
    class Plugin:
        def __init__(self):
            self.manifest = ProviderManifest(identifier, "1", rendering=True)

        def available(self, configured):
            return True

        async def acquire(self, request, services):
            (calls if calls is not None else []).append((identifier, request.policy.public_entry_url))
            if needs_entry and request.policy.public_entry_url:
                return {"url": request.url, "content": PAGE, "content_type": "text/html", "http_status": 200}
            raise WebFailure(failure, "fixture wall")
    return Plugin()


@pytest.fixture
def registry(monkeypatch):
    def install(*items):
        registry = ProviderRegistry()
        for item in items:
            registry.register(item)
        monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    return install


POLICY = {"origin_route_hint_ttl_seconds": 0, "origin_min_interval_seconds": 0, "origin_cooldown_seconds": 0,
          "second_opinion_text_chars": 0, "hedge_after_seconds": 0, "completeness_escalation": False}


async def test_a_walled_deep_link_is_retried_through_the_home_page(tmp_path, registry):
    calls = []
    registry(plugin("http", calls=calls), plugin("camoufox", needs_entry=True, calls=calls))
    async with Runtime(tmp_path) as web:
        result = await web.read("https://shop.example.com/p/lamp-123456", policy_overrides=POLICY)
    receipt = result["receipt"]
    assert receipt["status"] == "observed" and receipt["method"] == "camoufox"
    assert receipt["warm_up"]["entry"] == "https://shop.example.com/" and receipt["warm_up"]["after"] == "BLOCKED"
    assert ("camoufox", "https://shop.example.com/") in calls


async def test_no_retry_when_switched_off_or_for_a_home_page(tmp_path, registry):
    calls = []
    registry(plugin("http", calls=calls), plugin("camoufox", needs_entry=True, calls=calls))
    async with Runtime(tmp_path) as web:
        off = await web.read("https://shop.example.com/p/lamp-123456",
                             policy_overrides={**POLICY, "warm_up_on_wall": False})
        home = await web.read("https://shop.example.com/", policy_overrides=POLICY)
    assert off["receipt"]["status"] == "failed" and "warm_up" not in off["receipt"]
    assert home["receipt"]["status"] == "failed" and "warm_up" not in home["receipt"]


async def test_site_sessions_keep_cookies_for_a_day(tmp_path):
    async with Runtime(tmp_path) as web:
        web._save_site_session("www.shop.example.com", [
            {"name": "cf_clearance", "value": "x", "domain": ".shop.example.com", "path": "/", "expires": -1},
            {"name": "tracker", "value": "y", "domain": ".ads.example.net", "path": "/", "expires": -1}])
        kept = web._site_session("www.shop.example.com", 24)
        assert [cookie["name"] for cookie in kept] == ["cf_clearance"]
        import json
        path = web._site_session_path("www.shop.example.com")
        saved = json.loads(path.read_text())
        path.write_text(json.dumps({**saved, "at": time.time() - 3 * 86400}))
        assert web._site_session("www.shop.example.com", 24) == []
    with pytest.raises(ValueError):
        WebPolicy(site_session_hours=-1)
