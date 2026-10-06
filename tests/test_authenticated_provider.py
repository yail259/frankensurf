"""Catalogued named-identity providers keep browser authority in Core."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
import httpx
import pytest

from frankensurf import Runtime, WebPolicy
from frankensurf.identity import IdentityRegistry
from frankensurf import providers
from frankensurf.providers import (
    ProviderManifest,
    ProviderRegistry,
    ProviderRequest,
    ProviderServices,
)
from frankensurf.runtime import (
    WebFailure,
    _core_identity_capture,
    _identity_acquisition_fingerprint,
)


URL = "https://shop.test/detail"


def identity_registry(tmp_path, *, snapshot=False):
    path = tmp_path / "identities.json"
    profile = tmp_path / "profile"
    profile.mkdir()
    registry = IdentityRegistry(path)
    registry.enroll_executor("local-test",
        endpoint="http://127.0.0.1:9331",
        user_data_dir=str(profile), profile_directory="Default")
    identity = {
        "executor_id": "local-test",
        "domains": ["shop.test"],
        "image_domains": ["cdn.shop.test"],
        "authority_mode": "LOCAL_ONLY",
    }
    if snapshot:
        identity["snapshot_policy"] = {
            "path_prefixes": ["/detail/"],
            "root_selectors": ["#listing"],
        }
    registry.enroll_identity("personal", **identity)
    return path


def response(url, title="Protected item"):
    content = "<title>" + title + "</title><p>Authenticated detail</p>"
    return {"url": url, "content": content, "raw": content.encode(),
            "content_type": "text/html", "http_status": 200,
            "headers": {}}


def forbid_http(request):
    raise AssertionError("named identity reached anonymous HTTP")


async def stub_core_identity(web, *, title="Protected item"):
    calls = []

    async def context(resolved, policy):
        calls.append("context")
        return object()

    async def health(context, resolved, policy, **kwargs):
        calls.append("health")
        return "verified"

    async def acquire(url, policy, resolved, context, **kwargs):
        calls.append("acquire")
        return response(url, title)

    web._identity_context = context
    web._identity_health = health
    web._get_identity_browser = acquire
    return calls


class CataloguedIdentityProvider:
    manifest = ProviderManifest("local_cdp", "fixture-1", rendering=True,
        requires_local_browser=True, authentication=True, navigation=True)

    async def acquire(self, request, services):
        return await services.authenticated(request.url, request.policy)


class PublicTransportBypass(CataloguedIdentityProvider):
    manifest = ProviderManifest("local_cdp", "bypass-1", rendering=True,
        requires_local_browser=True, authentication=True, navigation=True)

    async def acquire(self, request, services):
        return await services.http(request.url, request.policy)


def install(monkeypatch, plugin, *, enabled=True):
    registry = ProviderRegistry()
    registry.register(plugin)
    if not enabled:
        registry.enable("local_cdp", False)
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    return registry


@pytest.mark.asyncio
async def test_default_named_identity_runs_through_catalogued_provider(tmp_path):
    path = identity_registry(tmp_path)
    async with Runtime(tmp_path / "state", identity_registry=path,
            transport=httpx.MockTransport(forbid_http)) as web:
        calls = await stub_core_identity(web)
        provider_calls = []
        original = web.providers.acquire

        async def tracked(identifier, request, services, **kwargs):
            provider_calls.append(identifier)
            return await original(identifier, request, services, **kwargs)

        web.providers.acquire = tracked
        result = await web.read(URL, WebPolicy(identity="personal"))
        record = next(item for item in web.providers.inspect()
                      if item["id"] == "local_cdp")

    assert result["receipt"]["status"] == "observed"
    assert provider_calls == ["local_cdp"]
    assert calls == ["context", "health", "acquire"]
    assert {name: record[name] for name in (
        "authentication", "rendering", "navigation",
        "requires_local_browser")} == {
            "authentication": True, "rendering": True,
            "navigation": False, "requires_local_browser": True}
    assert result["receipt"]["provider_version"] == record["version"]
    assert result["receipt"]["provider_binding_id"] == record["binding_id"]
    assert result["receipt"]["attempts"] == [{
        "provider": "local_cdp",
        "provider_version": record["version"],
        "provider_binding_id": record["binding_id"],
        "status": "observed",
        "latency_ms": result["receipt"]["attempts"][0]["latency_ms"],
    }]


@pytest.mark.asyncio
async def test_external_catalogued_identity_provider_uses_core_service(
        tmp_path, monkeypatch):
    path = identity_registry(tmp_path)
    install(monkeypatch, CataloguedIdentityProvider())
    async with Runtime(tmp_path / "state", identity_registry=path,
            transport=httpx.MockTransport(forbid_http)) as web:
        calls = await stub_core_identity(web)
        result = await web.read(URL, WebPolicy(
            identity="personal", provider="local_cdp"))

    assert result["receipt"]["status"] == "observed"
    assert result["receipt"]["provider_version"] == "fixture-1"
    assert result["receipt"]["provider_binding_id"]
    assert calls == ["context", "health", "acquire"]


@pytest.mark.asyncio
async def test_disabled_identity_provider_fails_before_browser_connection(
        tmp_path, monkeypatch):
    path = identity_registry(tmp_path)
    install(monkeypatch, CataloguedIdentityProvider(), enabled=False)
    async with Runtime(tmp_path / "state", identity_registry=path) as web:
        async def forbidden(*args):
            raise AssertionError("disabled plugin reached identity executor")
        web._identity_context = forbidden
        result = await web.read(URL, WebPolicy(identity="personal"))

    assert result["receipt"]["failure"]["code"] == "PLUGIN_DISABLED"
    assert result["receipt"]["attempts"] == []


@pytest.mark.asyncio
async def test_identity_provider_cannot_use_public_transport(
        tmp_path, monkeypatch):
    path = identity_registry(tmp_path)
    install(monkeypatch, PublicTransportBypass())
    async with Runtime(tmp_path / "state", identity_registry=path,
            transport=httpx.MockTransport(forbid_http)) as web:
        calls = await stub_core_identity(web)
        result = await web.read(URL, WebPolicy(identity="personal"))

    assert result["receipt"]["failure"]["code"] == (
        "IDENTITY_POLICY_DENIED")
    assert result["receipt"]["attempts"][0]["provider"] == "local_cdp"
    assert calls == ["context", "health"]


@pytest.mark.asyncio
async def test_identity_provider_candidates_cannot_widen_executor_binding(
        tmp_path):
    path = identity_registry(tmp_path)
    async with Runtime(tmp_path / "state", identity_registry=path) as web:
        async def forbidden(*args):
            raise AssertionError("invalid provider plan reached browser")
        web._identity_context = forbidden
        result = await web.read(URL, WebPolicy(identity="personal",
            provider_candidates=("http",)))

    assert result["receipt"]["failure"]["code"] == (
        "IDENTITY_PROVIDER_DENIED")
    assert result["receipt"]["attempts"] == []


@pytest.mark.asyncio
async def test_authenticated_service_capability_closes_with_provider_scope():
    captured = {}
    registry = ProviderRegistry()

    class Capture(CataloguedIdentityProvider):
        async def acquire(self, request, services):
            captured["service"] = services.authenticated
            return await services.authenticated(request.url, request.policy)

    registry.register(Capture())

    async def denied(*args, **kwargs):
        raise AssertionError("public service executed")

    async def authenticated(url, policy):
        return response(url)

    services = ProviderServices(denied, denied, denied,
        authenticated=authenticated)
    policy = WebPolicy(identity="personal", provider="local_cdp")
    result = await registry.acquire("local_cdp", ProviderRequest(URL, policy),
                                    services)

    assert result["url"] == URL
    with pytest.raises(WebFailure) as error:
        await captured["service"](URL, policy)
    assert error.value.code == "POLICY_DENIED"


@pytest.mark.asyncio
async def test_identity_cache_is_scoped_to_provider_binding(
        tmp_path, monkeypatch):
    path = identity_registry(tmp_path)

    class Versioned(CataloguedIdentityProvider):
        def __init__(self, version):
            self.manifest = ProviderManifest("local_cdp", version,
                rendering=True, requires_local_browser=True,
                authentication=True, navigation=True)

    install(monkeypatch, Versioned("v1"))
    async with Runtime(tmp_path / "state", identity_registry=path) as web:
        await stub_core_identity(web, title="v1")
        first = await web.read(URL, WebPolicy(identity="personal"))

    install(monkeypatch, Versioned("v2"))
    async with Runtime(tmp_path / "state", identity_registry=path) as web:
        await stub_core_identity(web, title="v2")
        second = await web.read(URL, WebPolicy(
            identity="personal", freshness="hour"))

    assert first["title"] == "v1"
    assert second["title"] == "v2"
    assert not second["receipt"]["cache_hit"]
    assert second["receipt"]["provider_version"] == "v2"


@pytest.mark.asyncio
async def test_authenticated_provider_must_consume_core_capability(
        tmp_path, monkeypatch):
    path = identity_registry(tmp_path)

    class Fabricated(CataloguedIdentityProvider):
        async def acquire(self, request, services):
            return response(request.url, "private-secret-token")

    install(monkeypatch, Fabricated())
    async with Runtime(tmp_path / "state", identity_registry=path) as web:
        calls = await stub_core_identity(web)
        result = await web.read(URL, WebPolicy(identity="personal"))

    assert result["receipt"]["failure"]["code"] == (
        "IDENTITY_PROVIDER_DENIED")
    assert result["receipt"]["http_status"] is None
    assert result["content"] == ""
    assert result["receipt"]["evidence"] == []
    assert "private-secret-token" not in json.dumps(result)
    assert calls == ["context", "health"]


@pytest.mark.asyncio
async def test_authenticated_capability_reentry_poisoned_even_if_caught(
        tmp_path, monkeypatch):
    path = identity_registry(tmp_path)

    class Reentrant(CataloguedIdentityProvider):
        async def acquire(self, request, services):
            acquired = await services.authenticated(
                request.url, request.policy)
            try:
                await services.authenticated(request.url, request.policy)
            except WebFailure:
                pass
            return acquired

    install(monkeypatch, Reentrant())
    async with Runtime(tmp_path / "state", identity_registry=path) as web:
        calls = await stub_core_identity(web)
        result = await web.read(URL, WebPolicy(identity="personal"))

    assert result["receipt"]["failure"]["code"] == (
        "IDENTITY_PROVIDER_DENIED")
    assert result["receipt"]["http_status"] is None
    assert result["content"] == ""
    assert calls == ["context", "health", "acquire"]


@pytest.mark.asyncio
async def test_authenticated_provider_cannot_compose_child_provider(
        tmp_path, monkeypatch):
    path = identity_registry(tmp_path)
    child_calls = []

    class Composing(CataloguedIdentityProvider):
        async def acquire(self, request, services):
            return await services.acquire_provider(
                "http", ProviderRequest(request.url, request.policy))

    class Child:
        manifest = ProviderManifest("http", "fixture-1")

        async def acquire(self, request, services):
            child_calls.append(request.url)
            return response(request.url)

    registry = ProviderRegistry()
    registry.register(Composing())
    registry.register(Child())
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    async with Runtime(tmp_path / "state", identity_registry=path) as web:
        calls = await stub_core_identity(web)
        result = await web.read(URL, WebPolicy(identity="personal"))

    assert result["receipt"]["failure"]["code"] == (
        "IDENTITY_POLICY_DENIED")
    assert child_calls == []
    assert calls == ["context", "health"]


@pytest.mark.asyncio
async def test_provider_cannot_forge_reserved_visible_snapshot(
        tmp_path, monkeypatch):
    path = identity_registry(tmp_path)

    class ForgedCapture(CataloguedIdentityProvider):
        async def acquire(self, request, services):
            acquired = await services.authenticated(
                request.url, request.policy)
            acquired["visible_snapshot"] = {
                "source_freshness": "now",
                "source_refresh_performed": True,
            }
            return acquired

    install(monkeypatch, ForgedCapture())
    async with Runtime(tmp_path / "state", identity_registry=path) as web:
        await stub_core_identity(web)
        result = await web.read(URL, WebPolicy(identity="personal"))

    assert result["receipt"]["failure"]["code"] == "PROVIDER_DOWN"
    assert "visible_snapshot" not in result["receipt"]


@pytest.mark.asyncio
async def test_core_attested_visible_snapshot_survives_provider_boundary(
        tmp_path, monkeypatch):
    from frankensurf.identity_snapshots import owner_visible_capture_binding_id
    path = identity_registry(tmp_path, snapshot=True)
    plugin_observations = []
    metadata = {
        "strategy": "owner_visible_snapshot",
        "strategy_version": "2",
        "strategy_binding_id": owner_visible_capture_binding_id(),
        "scope": "owner_opened_visible_region",
        "navigation_performed": False,
        "source_refresh_performed": False,
        "source_freshness": "unknown",
        "gallery_coverage": "partial_unverified_candidates",
    }

    class Passthrough(CataloguedIdentityProvider):
        async def acquire(self, request, services):
            acquired = await services.authenticated(
                request.url, request.policy)
            plugin_observations.append("visible_snapshot" in acquired)
            return acquired

    install(monkeypatch, Passthrough())
    async with Runtime(tmp_path / "state", identity_registry=path) as web:
        await stub_core_identity(web)

        async def captured(url, policy, resolved, context, **kwargs):
            return {**response(url), "visible_snapshot": dict(metadata)}

        web._get_identity_browser = captured
        result = await web.read(URL, WebPolicy(
            identity="personal", freshness="now"))

    assert result["receipt"]["status"] == "observed"
    assert plugin_observations == [False]
    assert result["receipt"]["visible_snapshot"] == metadata
    assert result["receipt"]["freshness_seconds"] is None
    assert result["receipt"]["capture_freshness_seconds"] == 0
    assert result["receipt"]["source_freshness"] == "unknown"
    assert result["receipt"]["source_refresh_performed"] is False
    assert result["receipt"]["requested_freshness_satisfied"] is False


def test_core_capture_requires_exact_selected_strategy_and_fingerprints_it():
    binding = "a" * 64
    selected = SimpleNamespace(
        id="owner_visible_snapshot", version="2", binding_id=binding)
    metadata = {
        "strategy": selected.id,
        "strategy_version": selected.version,
        "strategy_binding_id": selected.binding_id,
        "scope": "owner_opened_visible_region",
        "navigation_performed": False,
        "source_refresh_performed": False,
        "source_freshness": "unknown",
        "gallery_coverage": "partial_unverified_candidates",
    }
    acquired = {**response(URL), "visible_snapshot": dict(metadata)}
    detached, capture, private_sources = _core_identity_capture(
        acquired, 1_000_000, capture_strategy=selected)

    assert capture == metadata
    assert private_sources == {}
    assert "visible_snapshot" not in detached
    assert (_identity_acquisition_fingerprint(
                detached, 1_000_000, capture_metadata=capture)
            != _identity_acquisition_fingerprint(
                detached, 1_000_000,
                capture_metadata={**capture, "strategy_version": "3"}))

    forged = {**response(URL), "visible_snapshot": {
        **metadata, "strategy_version": "3"}}
    with pytest.raises(WebFailure) as error:
        _core_identity_capture(
            forged, 1_000_000, capture_strategy=selected)
    assert error.value.code == "PROVIDER_DOWN"


@pytest.mark.asyncio
async def test_provider_cannot_replace_core_authenticated_payload(
        tmp_path, monkeypatch):
    path = identity_registry(tmp_path)

    class Replacement(CataloguedIdentityProvider):
        async def acquire(self, request, services):
            acquired = await services.authenticated(
                request.url, request.policy)
            acquired["content"] = "<title>replacement</title>"
            acquired["raw"] = acquired["content"].encode()
            return acquired

    install(monkeypatch, Replacement())
    async with Runtime(tmp_path / "state", identity_registry=path) as web:
        await stub_core_identity(web)
        result = await web.read(URL, WebPolicy(identity="personal"))

    assert result["receipt"]["failure"]["code"] == (
        "IDENTITY_PROVIDER_DENIED")
    assert result["content"] == ""
    assert result["receipt"]["evidence"] == []


@pytest.mark.asyncio
async def test_provider_cannot_replace_core_content_readiness(
        tmp_path, monkeypatch):
    path = identity_registry(tmp_path)

    class ReadinessReplacement(CataloguedIdentityProvider):
        async def acquire(self, request, services):
            acquired = await services.authenticated(
                request.url, request.policy)
            acquired["content_readiness"] = {
                "status": "timed_out", "timeout_seconds": 2}
            return acquired

    install(monkeypatch, ReadinessReplacement())
    async with Runtime(tmp_path / "state", identity_registry=path) as web:
        await stub_core_identity(web)

        async def captured(url, policy, resolved, context):
            return {**response(url), "content_readiness": {
                "status": "satisfied", "timeout_seconds": 2}}

        web._get_identity_browser = captured
        result = await web.read(URL, WebPolicy(
            identity="personal", content_ready_selector="#ready",
            content_ready_timeout_seconds=2))

    assert result["receipt"]["failure"]["code"] == (
        "IDENTITY_PROVIDER_DENIED")
    assert result["receipt"]["evidence"] == []


@pytest.mark.asyncio
async def test_provider_cannot_replace_core_screenshot_reference(
        tmp_path, monkeypatch):
    path = identity_registry(tmp_path)
    original = {"sha256": "a" * 64, "path": "/private/core.png",
                "bytes": 10}
    replacement = {"sha256": "b" * 64,
                   "path": "/private/plugin.png", "bytes": 10}

    class ScreenshotReplacement(CataloguedIdentityProvider):
        async def acquire(self, request, services):
            acquired = await services.authenticated(
                request.url, request.policy)
            acquired["screenshot"] = dict(replacement)
            return acquired

    install(monkeypatch, ScreenshotReplacement())
    async with Runtime(tmp_path / "state", identity_registry=path) as web:
        await stub_core_identity(web)

        async def captured(url, policy, resolved, context):
            return {**response(url), "screenshot": dict(original)}

        web._get_identity_browser = captured
        result = await web.read(URL, WebPolicy(identity="personal"))

    assert result["receipt"]["failure"]["code"] == (
        "IDENTITY_PROVIDER_DENIED")
    assert result["receipt"]["evidence"] == []
    assert "plugin.png" not in json.dumps(result)


@pytest.mark.asyncio
async def test_private_signed_image_source_stays_core_only_and_redacted(
        tmp_path, monkeypatch):
    path = identity_registry(tmp_path)
    public_url = "https://cdn.shop.test/image.jpg"
    private_url = public_url + "?token=private-secret-token#tracking"
    provider_saw_private_map = []
    fetched = []

    class Passthrough(CataloguedIdentityProvider):
        async def acquire(self, request, services):
            acquired = await services.authenticated(
                request.url, request.policy)
            provider_saw_private_map.append(
                "identity_private_image_sources" in acquired)
            return acquired

    install(monkeypatch, Passthrough())
    async with Runtime(tmp_path / "state", identity_registry=path) as web:
        await stub_core_identity(web)

        async def captured(url, policy, resolved, context):
            content = ("<html><head><title>item</title></head>"
                       "<body><img src='" + public_url + "'></body></html>")
            return {"url": url, "content": content,
                    "raw": content.encode(), "content_type": "text/html",
                    "http_status": 200, "headers": {},
                    "identity_private_image_sources": {
                        public_url: private_url}}

        async def image(fetch_url, policy, resolved, context, *,
                        public_url=None):
            fetched.append(fetch_url)
            return {"url": fetch_url, "status": "decoded",
                    "failure": None}

        web._get_identity_browser = captured
        web._get_identity_image = image
        result = await web.read(URL, WebPolicy(
            identity="personal", include_images=True))

    encoded = json.dumps(result)
    assert result["receipt"]["status"] == "observed"
    assert provider_saw_private_map == [False]
    assert fetched == [private_url]
    assert result["image_urls"] == [public_url]
    assert result["images"][0]["url"] == public_url
    assert "private-secret-token" not in encoded
    assert "identity_private_image_sources" not in encoded


@pytest.mark.asyncio
async def test_provider_cannot_supply_private_image_source_map(
        tmp_path, monkeypatch):
    path = identity_registry(tmp_path)

    class ForgedPrivateMap(CataloguedIdentityProvider):
        async def acquire(self, request, services):
            acquired = await services.authenticated(
                request.url, request.policy)
            acquired["identity_private_image_sources"] = {
                "https://cdn.shop.test/image.jpg":
                    "https://cdn.shop.test/image.jpg?secret=plugin"}
            return acquired

    install(monkeypatch, ForgedPrivateMap())
    async with Runtime(tmp_path / "state", identity_registry=path) as web:
        await stub_core_identity(web)
        result = await web.read(URL, WebPolicy(identity="personal"))

    assert result["receipt"]["failure"]["code"] == "PROVIDER_DOWN"
    assert "secret=plugin" not in json.dumps(result)


@pytest.mark.asyncio
async def test_provider_binding_drift_fails_before_authenticated_capture(
        tmp_path):
    path = identity_registry(tmp_path)
    async with Runtime(tmp_path / "state", identity_registry=path) as web:
        calls = []
        original_binding = web.providers.binding_id
        drifted = False

        def binding_id(identifier):
            return ("f" * 64 if drifted
                    else original_binding(identifier))

        async def context(resolved, policy):
            nonlocal drifted
            calls.append("context")
            drifted = True
            return object()

        async def health(context, resolved, policy):
            calls.append("health")
            return "verified"

        async def forbidden(*args):
            raise AssertionError("drifted binding reached identity capture")

        web.providers.binding_id = binding_id
        web._identity_context = context
        web._identity_health = health
        web._get_identity_browser = forbidden
        result = await web.read(URL, WebPolicy(identity="personal"))

    assert result["receipt"]["failure"]["code"] == (
        "IDENTITY_PROVIDER_DENIED")
    assert result["receipt"]["evidence"] == []
    assert calls == ["context", "health"]


@pytest.mark.asyncio
async def test_inflight_background_authenticated_call_is_cancelled(
        tmp_path, monkeypatch):
    path = identity_registry(tmp_path)
    capture_started = asyncio.Event()
    capture_cancelled = asyncio.Event()

    class FireAndForget(CataloguedIdentityProvider):
        async def acquire(self, request, services):
            asyncio.create_task(services.authenticated(
                request.url, request.policy))
            await capture_started.wait()
            return response(request.url, "fabricated")

    install(monkeypatch, FireAndForget())
    async with Runtime(tmp_path / "state", identity_registry=path) as web:
        await stub_core_identity(web)

        async def blocked_capture(url, policy, resolved, context):
            capture_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                capture_cancelled.set()

        web._get_identity_browser = blocked_capture
        result = await web.read(URL, WebPolicy(
            identity="personal", provider_cleanup_grace_seconds=0.1))

    assert result["receipt"]["failure"]["code"] == (
        "IDENTITY_PROVIDER_DENIED")
    assert capture_cancelled.is_set()
    assert result["content"] == ""
