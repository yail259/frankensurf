"""FrankenSurf profiles: an encrypted, scoped login any capable provider can carry."""
import base64
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from frankensurf import profiles
from frankensurf.profiles import Profile, ProfileError, ProfileStore
from frankensurf.runtime import Runtime, WebPolicy

PAGE = "<html><title>Account</title><body>" + "<p>Signed in as Ada. Order history.</p>" * 30 + "</body></html>"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "STORE", tmp_path / "profiles")
    monkeypatch.setattr(profiles, "KEY_FILE", tmp_path / "profile.key")
    return ProfileStore()


def cookie(name, value, domain, **extra):
    return {"name": name, "value": value, "domain": domain, "path": "/", "expires": -1,
            "httpOnly": True, "secure": True, "sameSite": "Lax", **extra}


def make(store, **state):
    profile = Profile(name="shop", sites=["shop.example.com"],
                      fingerprint={"user_agent": "Mozilla/5.0 FrankenTest"},
                      state={"cookies": [cookie("sid", "s3cret", ".shop.example.com"),
                                         cookie("other", "x", ".tracker.example")], "origins": []})
    store.save(profile)
    return profile


def test_vault_is_encrypted_private_and_bound_to_its_key(store, tmp_path, monkeypatch):
    make(store)
    blob = (tmp_path / "profiles/shop.fsp").read_bytes()
    assert blob.startswith(b"FSP1") and b"s3cret" not in blob
    assert (tmp_path / "profiles/shop.fsp").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "profile.key").stat().st_mode & 0o777 == 0o600
    assert store.load("shop").state["cookies"][0]["value"] == "s3cret"
    monkeypatch.setenv("FRANKENSURF_PROFILE_KEY", base64.b64encode(b"k" * 32).decode())
    with pytest.raises(ProfileError):
        store.load("shop")
    assert store.list() == [{"name": "shop", "status": "unreadable with this key"}]
    with pytest.raises(ProfileError):
        store.load("../etc/passwd")


def test_cookies_are_scoped_to_the_profile_sites(store):
    profile = make(store)
    assert [c["name"] for c in profile.scoped_state()["cookies"]] == ["sid"]
    assert profile.cookie_header("https://shop.example.com/account") == "sid=s3cret"
    assert profile.cookie_header("http://shop.example.com/account") is None  # secure cookie
    profile.state["cookies"].append(cookie("old", "1", "shop.example.com", expires=time.time() - 10))
    assert profile.cookie_header("https://shop.example.com/") == "sid=s3cret"
    assert profile.covers("https://www.shop.example.com/x") and not profile.covers("https://evil.example/")


async def test_http_read_carries_the_profile_and_saves_refreshed_cookies(store, tmp_path):
    make(store)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html",
                              "set-cookie": "sid=rotated; Domain=shop.example.com; Path=/; Secure"})
    async with Runtime(tmp_path / "state", transport=httpx.MockTransport(handler)) as web:
        result = await web.read("https://shop.example.com/account", policy_overrides={
            "profile": "shop", "origin_route_hint_ttl_seconds": 0})
        anonymous = await web.read("https://shop.example.com/account")
    assert result["receipt"]["status"] == "observed"
    assert seen[0].headers["cookie"] == "sid=s3cret"
    assert seen[0].headers["user-agent"] == "Mozilla/5.0 FrankenTest"
    assert result["receipt"]["profile"] == {"name": "shop", "version": 2, "sharing": "local",
                                            "carried_by": "http"}
    assert "cookie" not in seen[1].headers  # nothing leaked into the shared client
    saved = {c["name"]: c["value"] for c in store.load("shop").state["cookies"]}
    assert saved["sid"] == "rotated" and "s3cret" not in str(result)


async def test_profile_reads_are_scoped_to_sites_and_carriers(store, tmp_path):
    make(store)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=PAGE,
                                    headers={"content-type": "text/html"}))
    async with Runtime(tmp_path / "state", transport=transport) as web:
        elsewhere = await web.read("https://evil.example/", policy_overrides={"profile": "shop"})
        hosted = await web.read("https://shop.example.com/a", provider="browserbase",
                                policy_overrides={"profile": "shop", "allow_paid_fallbacks": True})
        missing = await web.read("https://shop.example.com/a", policy_overrides={"profile": "nobody"})
    assert elsewhere["receipt"]["failure"]["message"] == "Profile does not cover this site"
    assert "cannot carry this profile" in hosted["receipt"]["failure"]["message"]
    assert "profile-login nobody" in missing["receipt"]["failure"]["message"]
    assert Profile("p", ["a.com"]).carriers() == ("http", "local", "patchright")
    assert "browserbase" in Profile("p", ["a.com"], sharing="hosted").carriers()
    with pytest.raises(ValueError):
        WebPolicy(profile="shop", identity="me")


class _Site(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (PAGE if "sid=let-me-in" in (self.headers.get("Cookie") or "")
                else "<html><body>Please sign in</body></html>")
        self.send_response(200)
        self.send_header("content-type", "text/html")
        if self.path == "/login":
            self.send_header("set-cookie", "sid=let-me-in; Path=/")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass


async def test_login_then_browser_read_with_the_profile(store, tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % server.server_port

    async def signed_in(page):
        return any(c["name"] == "sid" for c in await page.context.cookies())
    try:
        profile = await profiles.login(store, "local-site", ["127.0.0.1"], start_url=base + "/login",
                                       headless=True, on_ready=signed_in, timeout_seconds=30)
        assert [c["name"] for c in profile.state["cookies"]] == ["sid"]
        assert profile.fingerprint["user_agent"]
        async with Runtime(tmp_path / "state") as web:
            result = await web.read(base + "/account", provider="local",
                                    policy_overrides={"profile": "local-site"})
    finally:
        server.shutdown()
    assert result["receipt"]["status"] == "observed", result["receipt"].get("failure")
    assert "Signed in as Ada" in result["text"]
    assert result["receipt"]["profile"]["carried_by"] == "local"
