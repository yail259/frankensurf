"""The real-browser fallback: opt-in, ahead after walls, waits out a challenge that clears itself."""
import glob
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from frankensurf import providers
from frankensurf.providers import ProviderManifest, ProviderRegistry
from frankensurf.runtime import Runtime, WebFailure, WebPolicy

URL = "https://walled.example.com/item/lamp-123456"
PAGE = "<html><title>Lamp</title><body>" + "<p>A brass lamp, $49, ships in two days.</p>" * 30 + "</body></html>"


def provider(identifier, outcome, *, scope=False, paid=False):
    class Plugin:
        manifest = ProviderManifest(identifier, "1", rendering=True, paid=paid, route_scope_required=scope)

        def available(self, configured):
            return True

        async def acquire(self, request, services):
            if outcome != "ok":
                raise WebFailure(outcome, "fixture outcome")
            return {"url": request.url, "content": PAGE, "content_type": "text/html", "http_status": 200}
    return Plugin()


@pytest.fixture
def registry(monkeypatch):
    def install(*plugins):
        registry = ProviderRegistry()
        for plugin in plugins:
            registry.register(plugin)
        monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    return install


POLICY = dict(origin_route_hint_ttl_seconds=0, provider_max_attempts_per_candidate=1,
              warm_up_on_wall=False, hedge_after_seconds=0)


def tried(result):
    return [attempt["provider"] for attempt in result["receipt"]["attempts"]]


async def test_the_real_browser_is_opt_in_and_moves_ahead_after_walls(tmp_path, registry, monkeypatch):
    monkeypatch.delenv("FRANKENSURF_REAL_BROWSER", raising=False)
    registry(provider("w1", "BLOCKED"), provider("w2", "CAPTCHA"), provider("w3", "BLOCKED"),
             provider("free_ok", "ok"), provider("real_browser", "ok", scope=True))
    async with Runtime(tmp_path) as web:
        off = await web.read(URL, WebPolicy(escalate_after_walls=2, **POLICY))
        on = await web.read(URL, WebPolicy(escalate_after_walls=2, allow_real_browser=True, **POLICY))
    assert "real_browser" not in tried(off) and tried(off)[-1] == "free_ok"
    assert tried(on) == ["w1", "w2", "real_browser"]
    assert on["receipt"]["routing"]["escalated_to"] == "real_browser"


async def test_the_real_browser_goes_before_paid_tools_and_can_be_switched_on_by_environment(
        tmp_path, registry, monkeypatch):
    monkeypatch.setenv("FRANKENSURF_REAL_BROWSER", "1")
    registry(provider("w1", "BLOCKED"), provider("w2", "BLOCKED"), provider("paid_ok", "ok", paid=True),
             provider("real_browser", "CAPTCHA", scope=True))
    async with Runtime(tmp_path) as web:
        result = await web.read(URL, WebPolicy(escalate_after_walls=2, allow_paid_fallbacks=True, **POLICY))
    assert tried(result) == ["w1", "w2", "real_browser", "paid_ok"]
    assert result["receipt"]["status"] == "observed"


def test_the_real_browser_is_found_where_it_installs_or_where_you_point(tmp_path, monkeypatch):
    from frankensurf import real_browser
    fake = tmp_path / "chrome"
    fake.write_text("")
    monkeypatch.setenv("FRANKENSURF_REAL_BROWSER_PATH", str(fake))
    assert real_browser.executable() == fake
    monkeypatch.setenv("FRANKENSURF_REAL_BROWSER_PATH", str(tmp_path / "missing"))
    assert real_browser.executable() is None
    monkeypatch.delenv("FRANKENSURF_REAL_BROWSER_PATH")
    monkeypatch.setattr(real_browser, "_candidates", lambda: [tmp_path / "missing", fake])
    assert real_browser.executable() == fake
    with pytest.raises(ValueError):
        WebPolicy(real_browser_wait_seconds=-1)


# --- End to end: a challenge page that clears by itself ----------------------
CHALLENGE = """<html><head><title>Just a moment...</title></head><body><p>Checking your browser.</p>
<script>setTimeout(() => { document.title = 'Brass lamp';
  document.body.innerHTML = '<h1>Brass lamp</h1>' + '<p>A brass lamp, $49, ships in two days.</p>'.repeat(30);
}, 2000);</script></body></html>"""
STUCK = """<html><head><title>Just a moment...</title></head><body><p>Checking your browser.</p></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (STUCK if self.path.startswith("/stuck") else CHALLENGE).encode()
        self.send_response(200)
        self.send_header("content-type", "text/html")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _chromium():
    found = sorted(glob.glob(str(Path.home() / ".cache/ms-playwright/chromium-*/chrome-linux*/chrome")))
    return found[-1] if found else None


@pytest.fixture
def real(monkeypatch, tmp_path):
    pytest.importorskip("playwright")
    chromium = _chromium()
    if not chromium or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        pytest.skip("needs a Chromium build and a display")
    # Playwright's own Chromium stands in for Chrome: the same code path.
    monkeypatch.setenv("FRANKENSURF_REAL_BROWSER_PATH", chromium)
    monkeypatch.setenv("FRANKENSURF_REAL_BROWSER_PROFILE", str(tmp_path / "profile"))


async def test_the_real_browser_waits_out_a_challenge_that_clears_itself(tmp_path, site, real):
    async with Runtime(tmp_path / "state") as web:
        result = await web.read(site + "/item", WebPolicy(provider="real_browser", settle_ms=0,
                                                          real_browser_wait_seconds=10))
    receipt = result["receipt"]
    assert receipt["status"] == "observed", receipt.get("failure")
    assert receipt["method"] == "real_browser" and "ships in two days" in result["text"]


async def test_a_challenge_that_needs_a_person_fails_as_a_wall(tmp_path, site, real):
    async with Runtime(tmp_path / "state") as web:
        result = await web.read(site + "/stuck", WebPolicy(provider="real_browser", settle_ms=0,
                                                           real_browser_wait_seconds=2))
    assert result["receipt"]["failure"]["code"] == "CAPTCHA"
