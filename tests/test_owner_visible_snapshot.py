"""Live isolated-browser tests; no real account or owner profile is accessed."""
import json
import io
import socket
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from playwright.async_api import async_playwright
from PIL import Image

from frankensurf import Runtime, WebPolicy
from frankensurf.identity import IdentityRegistry


@pytest.fixture
async def owner_browser(tmp_path):
    requests = []
    image_bytes = io.BytesIO()
    Image.new("RGB",(60,40),"blue").save(image_bytes,format="PNG")
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def do_GET(self):
            requests.append(self.path)
            if self.path.startswith("/fixture-photo.png"):
                self.send_response(200 if "fixture_session=synthetic" in self.headers.get("Cookie","") else 401)
                self.send_header("Content-Type","image/png")
                self.end_headers()
                self.wfile.write(image_bytes.getvalue())
                return
            self.send_response(200)
            self.send_header("Content-Type","text/html")
            if self.path == "/login":
                self.send_header("Set-Cookie","fixture_session=synthetic; Path=/; HttpOnly; SameSite=Strict")
            self.end_headers()
            if self.path == "/marketplace/item/123/":
                data = '''<main id="listing"><input type="hidden" value="hidden-input-secret"><div hidden>hidden-text-secret</div><h1>Loading</h1><img src="/fixture-photo.png?opaque-signature=private-value#private-fragment" width="60" height="40"><a href="/marketplace/item/789/?tracking=private-link#private-fragment">Other visible lens</a><img hidden src="/hidden-photo.png"></main><script>window.fixtureToken="inline-script-secret";fetch('/read-context').then(()=>{document.querySelector('h1').textContent='Nikon fixture lens';document.querySelector('main').insertAdjacentHTML('beforeend','<p>A$1,400</p><p>Owner-visible description, sold as is.</p><span id="ready">Authenticated fixture</span>');});</script>'''
            elif self.path == "/marketplace/item/124/":
                data = '''<main id="listing"><h1>Clean image fixture</h1><p>Owner-visible clean URL.</p><img src="/fixture-photo.png" width="60" height="40"><span id="ready">Authenticated fixture</span></main>'''
            elif self.path == "/marketplace/item/456/":
                data = '<main id="listing"><h1>Message panel</h1><input value="visible-private-secret"></main>'
            else:
                data = '<p>Fixture response</p>'
            self.wfile.write(data.encode())
    server = ThreadingHTTPServer(("127.0.0.1",0),Handler)
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1",0))
        debug_port = probe.getsockname()[1]
    profile = tmp_path/"dedicated-fixture-profile"
    registry_path = tmp_path/"identity"/"registry.json"
    reg = IdentityRegistry(registry_path)
    reg.enroll_executor("fixture",endpoint=f"http://127.0.0.1:{debug_port}",user_data_dir=str(profile))
    options = dict(executor_id="fixture",domains=["127.0.0.1"],
                   image_domains=["127.0.0.1"],
                   snapshot_policy={"path_prefixes":["/marketplace/"],"root_selectors":["#listing"]})
    reg.enroll_identity("owner",**options)
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(str(profile),headless=True,chromium_sandbox=True,
                    args=["--enable-automation","--remote-debugging-address=127.0.0.1",f"--remote-debugging-port={debug_port}"],
                    viewport={"width":1000,"height":800})
        page = context.pages[0]
        await page.goto(base+"/login")
        await page.goto(base+"/marketplace/item/123/")
        await page.locator("#ready").wait_for()
        yield base,context,page,reg,registry_path,requests,options,profile
        await context.close()
    server.shutdown()
    server.server_close()


def reject_http(request):
    raise AssertionError("Named snapshot reached anonymous HTTP")


async def test_projection_text_read_uses_policy_timeout():
    from frankensurf.identity_snapshots import OwnerVisibleSnapshot
    observed = []

    class EmptyLocator:
        @property
        def first(self):
            return self

        async def count(self):
            return 0

        async def all(self):
            return []

    class Root:
        async def inner_text(self, *, timeout):
            observed.append(timeout)
            return "Visible text"

        def locator(self, selector):
            return EmptyLocator()

    class Registry:
        def permits_url(self, resolved, url, image=False):
            return None

    projection = await OwnerVisibleSnapshot()._visible_projection(
        object(), Root(), "https://shop.test/item", object(), Registry(),
        WebPolicy(timeout_seconds=1.25))

    assert observed == [1250]
    assert projection["text"] == "Visible text"


async def test_js_rendered_owner_page_is_sampled_without_navigation_or_private_state(owner_browser,tmp_path):
    base,context,page,reg,path,requests,_,_ = owner_browser
    before = list(requests)
    original_pages = list(context.pages)
    async with Runtime(tmp_path/"evidence",identity_registry=path,transport=httpx.MockTransport(reject_http)) as web:
        result = await web.read(base+"/marketplace/item/123/",WebPolicy(identity="owner",settle_ms=0))
    assert result["receipt"]["status"] == "observed"
    assert result["title"] == "Nikon fixture lens"
    assert "Owner-visible description" in result["text"]
    assert result["receipt"]["authentication"] == "not_configured"
    assert result["receipt"]["visible_snapshot"]["source_freshness"] == "unknown"
    assert result["receipt"]["freshness_seconds"] is None
    assert result["receipt"]["capture_freshness_seconds"] == 0
    assert result["receipt"]["requested_freshness_satisfied"] is False
    assert requests == before
    assert list(context.pages) == original_pages and not page.is_closed()
    exported = json.dumps(result)
    for secret in ("hidden-input-secret","hidden-text-secret","inline-script-secret","fixture_session"):
        assert secret not in exported
        for evidence in (tmp_path/"evidence").rglob("*.html"):
            assert secret not in evidence.read_text()


async def test_wrong_url_duplicate_page_and_login_paths_require_owner_action(owner_browser,tmp_path):
    base,context,page,reg,path,requests,_,_ = owner_browser
    async with Runtime(tmp_path/"evidence",identity_registry=path,transport=httpx.MockTransport(reject_http)) as web:
        missing = await web.read(base+"/marketplace/item/999/",WebPolicy(identity="owner"))
        outside = await web.read(base+"/login",WebPolicy(identity="owner"))
        assert missing["receipt"]["failure"]["code"] == "IDENTITY_OPERATOR_HANDOFF_REQUIRED"
        assert outside["receipt"]["failure"]["code"] == "IDENTITY_POLICY_DENIED"
        duplicate = await context.new_page()
        await duplicate.goto(page.url)
        await duplicate.locator("#ready").wait_for()
        before = list(requests)
        result = await web.read(page.url,WebPolicy(identity="owner"))
        assert result["receipt"]["failure"]["code"] == "IDENTITY_OPERATOR_HANDOFF_REQUIRED"
        assert requests == before
        await duplicate.close()


async def test_listing_with_message_box_is_captured(owner_browser,tmp_path):
    # Marketplace listings carry a "message seller" box inside the listing.
    base,context,page,reg,path,requests,_,_ = owner_browser
    await page.goto(base+"/marketplace/item/456/")
    async with Runtime(tmp_path/"evidence",identity_registry=path) as web:
        result = await web.read(page.url,WebPolicy(identity="owner"))
    assert result["receipt"]["status"] == "observed"
    assert result["title"] == "Message panel"


async def test_global_search_outside_listing_region_does_not_block_capture(
        owner_browser, tmp_path):
    base, context, page, reg, path, requests, _, _ = owner_browser
    await page.locator("body").evaluate(
        "node => node.insertAdjacentHTML('afterbegin',"
        "'<header><input aria-label=Search value=camera></header>')")
    async with Runtime(tmp_path/"evidence", identity_registry=path) as web:
        result = await web.read(page.url, WebPolicy(identity="owner"))
    assert result["receipt"]["status"] == "observed"
    assert result["title"] == "Nikon fixture lens"


async def test_global_password_field_blocks_capture(owner_browser, tmp_path):
    base, context, page, reg, path, requests, _, _ = owner_browser
    await page.locator("body").evaluate(
        "node => node.insertAdjacentHTML('afterbegin',"
        "'<header><input type=password value=private-password></header>')")
    async with Runtime(tmp_path/"evidence", identity_registry=path) as web:
        result = await web.read(page.url, WebPolicy(identity="owner"))
    assert result["receipt"]["failure"]["code"] == "IDENTITY_POLICY_DENIED"
    assert not result["receipt"]["evidence"]
    assert "private-password" not in json.dumps(result)


async def test_editable_overlay_does_not_block_capture(
        owner_browser, tmp_path):
    base, context, page, reg, path, requests, _, _ = owner_browser
    await page.locator("body").evaluate(
        "node => node.insertAdjacentHTML('beforeend',"
        "'<input value=private-overlay style=\"position:fixed;left:0;top:0;"
        "width:900px;height:500px\">')")
    async with Runtime(tmp_path/"evidence", identity_registry=path) as web:
        result = await web.read(page.url, WebPolicy(identity="owner"))
    assert result["receipt"]["status"] == "observed"


async def test_unrelated_dialog_does_not_block_capture(owner_browser,tmp_path):
    base,context,page,reg,path,requests,_,_ = owner_browser
    await page.locator("body").evaluate("node => node.insertAdjacentHTML('beforeend','<div role=dialog>cookie banner</div>')")
    async with Runtime(tmp_path/"evidence",identity_registry=path) as web:
        result = await web.read(page.url,WebPolicy(identity="owner"))
    assert result["receipt"]["status"] == "observed"
    assert "cookie banner" not in result["text"]


async def test_projection_drift_between_text_and_screenshot_fails_closed(
        owner_browser, tmp_path, monkeypatch):
    from frankensurf.identity_snapshots import OwnerVisibleSnapshot
    from frankensurf.runtime import WebFailure
    base, context, page, reg, path, requests, _, _ = owner_browser
    original = OwnerVisibleSnapshot._visible_projection
    calls = 0

    async def mutate_after_first_projection(
            self, observed_page, root, url, resolved, registry, policy):
        nonlocal calls
        projection = await original(
            self, observed_page, root, url, resolved, registry, policy)
        calls += 1
        if calls == 1:
            await observed_page.locator("h1").evaluate(
                "node => node.textContent = 'Changed during capture'")
        return projection

    monkeypatch.setattr(
        OwnerVisibleSnapshot, "_visible_projection",
        mutate_after_first_projection)
    with pytest.raises(WebFailure) as error:
        await OwnerVisibleSnapshot().acquire(
            page.url, WebPolicy(identity="owner", settle_ms=0),
            reg.resolve("owner", page.url), context, reg)
    assert error.value.code == "IDENTITY_CHANGED"


async def test_selected_actual_photo_uses_cookie_context_without_export(owner_browser,tmp_path):
    base,context,page,reg,path,requests,_,_ = owner_browser
    async with Runtime(tmp_path/"evidence",identity_registry=path) as web:
        result = await web.read(page.url,WebPolicy(identity="owner",include_images=True))
    assert result["receipt"]["status"] == "observed"
    # Signed CDN parameters are kept so the link loads; fragments are dropped.
    signed = base+"/fixture-photo.png?opaque-signature=private-value"
    assert result["image_urls"] == [signed]
    assert result["images"][0]["url"] == signed
    assert result["images"][0]["status"] == "decoded"
    assert result["images"][0]["width"] == 60
    assert result["images"][0]["height"] == 40
    assert result["receipt"]["visible_snapshot"]["gallery_coverage"] == "partial_unverified_candidates"
    exported = json.dumps(result)
    assert "fixture_session" not in exported
    assert "private-fragment" not in exported


async def test_clean_image_url_is_not_treated_as_a_private_mapping(
        owner_browser, tmp_path):
    base, context, page, reg, path, requests, _, _ = owner_browser
    await page.goto(base + "/marketplace/item/124/")
    async with Runtime(tmp_path/"evidence", identity_registry=path) as web:
        result = await web.read(
            page.url, WebPolicy(identity="owner", include_images=True))

    assert result["receipt"]["status"] == "observed"
    assert result["image_urls"] == [base + "/fixture-photo.png"]
    assert result["images"][0]["status"] == "decoded"
    assert result["images"][0]["width"] == 60
    assert result["images"][0]["height"] == 40


async def test_runtime_uses_import_frozen_strategy_after_preinit_mutation(
        owner_browser, tmp_path, monkeypatch):
    import frankensurf.identity_snapshots as snapshots
    base, context, page, reg, path, requests, _, _ = owner_browser

    async def replaced(self, url, policy, resolved, context, registry):
        raise AssertionError("mutable source method reached execution")

    def replaced_escape(*args, **kwargs):
        raise AssertionError("mutable source global reached execution")

    monkeypatch.setattr(
        snapshots.OwnerVisibleSnapshot.acquire, "__code__",
        replaced.__code__)
    monkeypatch.setattr(
        snapshots.OwnerVisibleSnapshot, "id", "changed_strategy")
    monkeypatch.setattr(
        snapshots.OwnerVisibleSnapshot, "version", "changed-version")
    monkeypatch.setattr(snapshots, "escape", replaced_escape)
    async with Runtime(tmp_path/"evidence", identity_registry=path) as web:
        result = await web.read(
            page.url, WebPolicy(identity="owner", settle_ms=0))

    assert result["receipt"]["status"] == "observed"
    snapshot = result["receipt"]["visible_snapshot"]
    assert snapshot["strategy"] == "owner_visible_snapshot"
    assert snapshot["strategy_version"] == "2"
    assert snapshot["strategy_binding_id"] == (
        snapshots.owner_visible_capture_binding_id())


async def test_runtime_pins_strategy_before_postinit_module_mutation(
        owner_browser, tmp_path, monkeypatch):
    import frankensurf.identity_snapshots as snapshots
    base, context, page, reg, path, requests, _, _ = owner_browser
    async with Runtime(tmp_path/"evidence", identity_registry=path) as web:
        async def replaced(self, url, policy, resolved, context, registry):
            raise AssertionError("mutable source method reached execution")

        def replaced_escape(*args, **kwargs):
            raise AssertionError("mutable source global reached execution")

        monkeypatch.setattr(
            snapshots.OwnerVisibleSnapshot.acquire, "__code__",
            replaced.__code__)
        monkeypatch.setattr(snapshots, "escape", replaced_escape)
        monkeypatch.setattr(
            snapshots, "bind_owner_visible_capture_strategy",
            lambda: (_ for _ in ()).throw(
                AssertionError("mutable bind factory reached execution")))
        result = await web.read(
            page.url, WebPolicy(identity="owner", settle_ms=0))

    assert result["receipt"]["status"] == "observed"
    snapshot = result["receipt"]["visible_snapshot"]
    assert snapshot["strategy"] == "owner_visible_snapshot"
    assert snapshot["strategy_version"] == "2"


@pytest.mark.parametrize("tamper", ("code", "global"))
async def test_runtime_rejects_pinned_executable_binding_drift(
        owner_browser, tmp_path, monkeypatch, tamper):
    base, context, page, reg, path, requests, _, _ = owner_browser
    async with Runtime(tmp_path/"evidence", identity_registry=path) as web:
        async def replaced(self, url, policy, resolved, context, registry):
            raise AssertionError("mutated pinned method reached execution")

        acquire = web._owner_visible_capture_strategy.acquire.__func__
        if tamper == "code":
            monkeypatch.setattr(acquire, "__code__", replaced.__code__)
        else:
            monkeypatch.setitem(
                acquire.__globals__, "escape",
                lambda *args, **kwargs: "mutated")
        result = await web.read(page.url, WebPolicy(identity="owner"))

    assert result["receipt"]["failure"]["code"] == "IDENTITY_CHANGED"
    assert result["receipt"]["evidence"] == []


async def test_operation_keeps_its_selected_strategy_after_runtime_swap(
        owner_browser, tmp_path):
    base, context, page, reg, path, requests, _, _ = owner_browser
    async with Runtime(tmp_path/"evidence", identity_registry=path) as web:
        selected = web._owner_visible_capture_strategy
        original_context = web._identity_context

        async def forbidden(*args, **kwargs):
            raise AssertionError("replacement strategy reached execution")

        replacement = replace(selected, acquire=forbidden)

        async def swap_after_selection(resolved, policy):
            observed_context = await original_context(resolved, policy)
            web._owner_visible_capture_strategy = replacement
            return observed_context

        web._identity_context = swap_after_selection
        result = await web.read(
            page.url, WebPolicy(identity="owner", settle_ms=0))

    assert result["receipt"]["status"] == "observed"
    assert result["receipt"]["visible_snapshot"][
        "strategy_binding_id"] == selected.binding_id


async def test_positive_visible_auth_and_wrong_profile_are_distinct(owner_browser,tmp_path):
    base,context,page,reg,path,requests,options,profile = owner_browser
    reg.enroll_identity("owner",**options,auth_check={"url":page.url,"authenticated_selector":"#ready"})
    async with Runtime(tmp_path/"evidence",identity_registry=path) as web:
        good = await web.read(page.url,WebPolicy(identity="owner"))
        assert good["receipt"]["authentication"] == "verified_visible_signal"
        reg.enroll_executor("fixture",endpoint=reg.resolve("owner",page.url).endpoint,user_data_dir=str(tmp_path/"wrong-profile"))
        wrong = await web.read(page.url,WebPolicy(identity="owner",freshness="cached"))
    assert wrong["receipt"]["failure"]["code"] == "IDENTITY_PROFILE_MISMATCH"
    assert not wrong["content"]


async def test_owner_snapshot_never_reuses_old_dom_cache(owner_browser,tmp_path):
    base,context,page,reg,path,requests,options,_ = owner_browser
    async with Runtime(tmp_path/"evidence",identity_registry=path) as web:
        first = await web.read(page.url,WebPolicy(identity="owner"))
        # This is the fixture operator changing the page, outside the runtime.
        await page.locator("h1").evaluate("node => node.textContent = 'Changed fixture lens'")
        second = await web.read(page.url,WebPolicy(identity="owner",freshness="cached"))
    assert first["title"] == "Nikon fixture lens"
    assert second["title"] == "Changed fixture lens"
    assert not second["receipt"]["cache_hit"]


