"""T5 human handoff: explicit only, last in line, named as the next step."""
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from frankensurf import handoff, providers
from frankensurf.providers import ProviderManifest, ProviderRegistry
from frankensurf.runtime import Runtime, WebFailure, WebPolicy

URL = "https://shop.example.com/item/1"
REAL = "<html><title>Item</title><body>" + "<p>Item text with price $90.</p>" * 20 + "</body></html>"


def plugin(identifier, failure=None, **manifest):
    class Plugin:
        calls = []

        def __init__(self):
            self.manifest = ProviderManifest(identifier, "1", **manifest)

        def available(self, configured):
            return True

        async def acquire(self, request, services):
            Plugin.calls.append(request.policy.timeout_seconds)
            if failure:
                raise WebFailure(failure, "fixture wall")
            return {"url": request.url, "content": REAL, "content_type": "text/html", "http_status": 200}
    return Plugin()


@pytest.fixture
def registry(monkeypatch):
    def install(*plugins):
        registry = ProviderRegistry()
        for item in plugins:
            registry.register(item)
        monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    return install


def tried(result):
    return [attempt["provider"] for attempt in result["receipt"]["attempts"]]


def test_cleared_means_no_challenge_no_sign_in_and_real_text():
    assert handoff.cleared(("Item", "Item text " * 20, URL), URL, 50)
    assert not handoff.cleared(("Just a moment...", "Checking your browser", URL), URL, 50)
    assert not handoff.cleared(("Sign in", "Please sign in " * 20, "https://shop.example.com/login?next=/item/1"), URL, 50)
    assert not handoff.cleared(("Item", "short", URL), URL, 50)
    assert not handoff.cleared(None, URL, 50)


async def test_handoff_runs_only_when_asked_and_last(tmp_path, registry):
    walled = plugin("walled", "CAPTCHA")
    person = plugin("handoff", rendering=True, route_scope_required=True)
    registry(walled, person)
    policy = WebPolicy(origin_cooldown_seconds=0, origin_route_hint_ttl_seconds=0)
    async with Runtime(tmp_path) as web:
        stopped = await web.read(URL, policy)
        assert tried(stopped) == ["walled"]
        assert stopped["receipt"]["next_step"]["provider"] == "handoff"
        assert stopped["receipt"]["next_step"]["reason"] == "CAPTCHA"
        resumed = await web.read(URL, policy_overrides={
            "allow_handoff": True, "origin_cooldown_seconds": 0, "origin_route_hint_ttl_seconds": 0})
    assert tried(resumed) == ["walled", "handoff"]
    assert resumed["receipt"]["status"] == "observed" and "next_step" not in resumed["receipt"]
    # The person gets handoff_timeout_seconds, not the ordinary attempt deadline.
    assert type(person).calls == [300.0]


async def test_explicit_handoff_is_not_held_by_an_origin_cool_down(tmp_path, registry, monkeypatch):
    monkeypatch.setattr(Runtime, "pacing_enabled", True)
    registry(plugin("walled", "BLOCKED"), plugin("handoff", rendering=True, route_scope_required=True))
    async with Runtime(tmp_path) as web:
        blocked = await web.read(URL, policy_overrides={"origin_min_interval_seconds": 0})
        held = await web.read(URL, policy_overrides={"origin_min_interval_seconds": 0})
        assert "cooling down" in held["receipt"]["failure"]["message"]
        person = await web.read(URL, provider="handoff", policy_overrides={"origin_min_interval_seconds": 0})
    assert tried(blocked) == ["walled"] and person["receipt"]["status"] == "observed"


def test_policy_validation():
    with pytest.raises(ValueError):
        WebPolicy(allow_handoff=1)
    with pytest.raises(ValueError):
        WebPolicy(handoff_timeout_seconds=0)


class _SelfClearingPage(BaseHTTPRequestHandler):
    """A wall that clears itself after two seconds, standing in for a person."""
    PAGE = ("<html><title>Just a moment...</title><body>Checking your browser"
            "<script>setTimeout(() => { document.title = 'Item';"
            " document.body.innerHTML = '" + "<p>Item text with price $90.</p>" * 20 + "'; }, 2000)"
            "</script></body></html>").encode()

    def do_GET(self):
        self.send_response(200)
        self.send_header("content-type", "text/html")
        self.end_headers()
        self.wfile.write(self.PAGE)

    def log_message(self, *args):
        pass


@pytest.mark.skipif(not os.environ.get("FRANKENSURF_LIVE_HANDOFF"),
                    reason="opens a visible browser window; set FRANKENSURF_LIVE_HANDOFF=1")
async def test_live_handoff_waits_for_the_wall_to_clear_then_resumes(tmp_path, monkeypatch):
    monkeypatch.setenv("FRANKENSURF_HANDOFF_PROFILE", str(tmp_path / "profile"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SelfClearingPage)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/item" % server.server_port
    try:
        async with Runtime(tmp_path / "state") as web:
            result = await web.read(url, provider="handoff",
                                    policy_overrides={"handoff_timeout_seconds": 30})
    finally:
        server.shutdown()
    assert result["receipt"]["status"] == "observed", result["receipt"].get("failure")
    assert "price $90" in result["text"] and result["title"] == "Item"
