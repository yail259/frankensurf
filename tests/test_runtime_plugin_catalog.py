import asyncio
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

import frankensurf.adapters as adapter_module
import frankensurf.providers as provider_module
import frankensurf.search_plugins as search_module
from frankensurf import Runtime, WebPolicy
from frankensurf.adapters import AdapterManifest, AdapterRegistry
from frankensurf.plugin_catalog import (ENTRY_POINT_GROUPS, PLUGIN_KINDS,
    PLUGIN_POLICY_SCHEMA, PluginCatalogError, PluginFactory,
    PluginSecretReference, build_plugin_catalog)
from frankensurf.providers import (ProviderManifest, ProviderRegistry,
    acquire_composite_child, attach_provider_attempt_tree,
    provider_attempt_tree)
from frankensurf.routes import PublicRouteRecipe
from frankensurf.runtime import WebFailure, _total_cost
from frankensurf.search_plugins import SearchManifest, SearchRegistry


class EntryPoint:
    def __init__(self, kind, name, distribution, value):
        self.group = ENTRY_POINT_GROUPS[kind]
        self.name = name
        self.dist = SimpleNamespace(name=distribution, version="1")
        self.value = value
        self.loads = 0

    def load(self):
        self.loads += 1
        return self.value


def policy_file(tmp_path, *, trusted=None, disabled=None):
    trusted = trusted or {}
    disabled = disabled or {}
    payload = {"schema": PLUGIN_POLICY_SCHEMA,
        "trusted": {kind: dict(trusted.get(kind, {})) for kind in PLUGIN_KINDS},
        "disabled": {kind: list(disabled.get(kind, ())) for kind in PLUGIN_KINDS}}
    tmp_path.mkdir(parents=True, exist_ok=True)
    tmp_path.chmod(0o700)
    path = tmp_path / "plugins.json"
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    return path


class ChildProvider:
    manifest = ProviderManifest("ep_child", "1")
    calls = []

    async def acquire(self, request, services):
        type(self).calls.append(request.url)
        return {"url": request.url, "content": "item:child",
                "content_type": "text/plain", "http_status": 200,
                "cost_usd": 0}


class CompositeProvider:
    manifest = ProviderManifest("ep_composite", "2")
    calls = []

    async def acquire(self, request, services):
        type(self).calls.append(request.url)
        attempts, costs = [], []
        try:
            child = await acquire_composite_child(services, "ep_child",
                request.url, request.policy, costs, attempts)
        except WebFailure as error:
            raise attach_provider_attempt_tree(error, attempts,
                                               _total_cost(costs))
        return {**child, "cost_usd": _total_cost(costs),
                "provider_attempts": provider_attempt_tree(attempts)}


class FixtureAdapter:
    manifest = AdapterManifest("ep_adapter", "3")
    calls = []

    def extract(self, request):
        type(self).calls.append(request.url)
        if request.content != "item:child":
            raise WebFailure("SCHEMA_CHANGED", "fixture content changed")
        return {"title": "External item", "text": request.content,
                "image_urls": [], "structured": {"listing_id": "child"}}


class FixtureSearch:
    manifest = SearchManifest("ep_search", "4")
    calls = []

    async def search(self, request, services):
        type(self).calls.append(request.query)
        request_url = "https://catalog.test/search?q=fixture"
        attribution = {"query": request.query, "adapter": "ep_search",
            "source": "ep_search", "source_version": "4",
            "request_url": request_url}
        receipt = {"trace_id": "f" * 32, "status": "observed",
            "failure": None, "attempts": [], "evidence": [],
            "cost_usd": 0, "latency_ms": 0}
        response = {"query": request.query, "source": "ep_search",
            "results": [{"url": "https://catalog.test/item", "title": "Item",
                "snippet": "Fixture", "engines": ["ep_search"],
                "indexed_date": None, "listing_state": "unknown",
                "verification": "indexed_discovery",
                "query_attribution": attribution}],
            "receipt": receipt, "query_attribution": attribution,
            "coverage": "returned-results", "upstream_failures": []}
        return {"response": response,
                "acquisition": {"url": request_url, "receipt": receipt}}


def external_catalog(tmp_path):
    entries = [
        EntryPoint("provider", "ep_child", "ep-child-pkg", ChildProvider()),
        EntryPoint("provider", "ep_composite", "ep-composite-pkg", CompositeProvider()),
        EntryPoint("adapter", "ep_adapter", "ep-adapter-pkg", FixtureAdapter()),
        EntryPoint("search", "ep_search", "ep-search-pkg", FixtureSearch()),
    ]
    path = policy_file(tmp_path, trusted={
        "provider": {"ep_child": "ep-child-pkg",
                     "ep_composite": "ep-composite-pkg"},
        "adapter": {"ep_adapter": "ep-adapter-pkg"},
        "search": {"ep_search": "ep-search-pkg"}})
    catalog = build_plugin_catalog(config_path=path, entry_points=entries,
        base_providers=ProviderRegistry(), base_searches=SearchRegistry(),
        base_adapters=AdapterRegistry())
    return catalog, entries


@pytest.mark.asyncio
async def test_runtime_owns_entry_point_catalog_for_routes_memory_search_and_children(tmp_path):
    ChildProvider.calls.clear(); CompositeProvider.calls.clear()
    FixtureAdapter.calls.clear(); FixtureSearch.calls.clear()
    catalog, entries = external_catalog(tmp_path / "config")
    state = tmp_path / "state"
    async with Runtime(state, plugin_catalog=catalog) as web:
        assert web.inspect_plugins() == catalog.inspect()
        ordinary = await web.read("https://catalog.test/item")
        assert ordinary["receipt"]["routing"]["provider_plan"]["ordered"] == [
            "ep_child", "ep_composite"]

        web.routes.register(PublicRouteRecipe(id="external_route", version="1",
            origin="https://catalog.test", path_pattern="/item",
            operation="extract", provider="ep_composite", provider_version="2",
            adapter="ep_adapter", adapter_version="3"))
        routed = await web.extract("https://catalog.test/item", "ep_adapter")
        searched = await web.search("fixture", source="ep_search")

    assert ordinary["receipt"]["method"] == "ep_child"
    assert routed["structured"] == {"listing_id": "child"}
    assert routed["receipt"]["route_recipe"]["provider"] == "ep_composite"
    assert routed["receipt"]["cost_usd"] == 0
    assert len(routed["receipt"]["attempts"]) == 1
    tree = routed["receipt"]["attempts"][0]["children"]
    assert tree["billing"] == "included_in_parent_cost"
    assert [(item["provider"], item["provider_version"], item["cost_usd"])
            for item in tree["attempts"]] == [("ep_child", "1", 0)]
    assert searched["source"] == "ep_search" and searched["results"]
    # Class-owned mutable state is copied onto each session subclass instead of
    # mutating the source definitions retained by the entry point.
    assert ChildProvider.calls == CompositeProvider.calls == []
    assert FixtureAdapter.calls == FixtureSearch.calls == []
    assert all(entry.loads == 1 for entry in entries)


@pytest.mark.asyncio
async def test_rejected_duplicate_and_disabled_entry_points_never_execute(tmp_path):
    class MustNotRun:
        manifest = ProviderManifest("blocked_ep", "1")
        async def acquire(self, request, services):
            raise AssertionError("rejected plugin executed")

    disabled = EntryPoint("provider", "blocked_ep", "blocked-pkg", MustNotRun())
    duplicate_a = EntryPoint("provider", "duplicate_ep", "duplicate-a", MustNotRun())
    duplicate_b = EntryPoint("provider", "duplicate_ep", "duplicate-b", MustNotRun())
    path = policy_file(tmp_path / "config",
        trusted={"provider": {"blocked_ep": "blocked-pkg",
                              "duplicate_ep": "duplicate-a"}},
        disabled={"provider": ["blocked_ep"]})
    catalog = build_plugin_catalog(config_path=path,
        entry_points=[duplicate_b, disabled, duplicate_a],
        base_providers=ProviderRegistry(), base_searches=SearchRegistry(),
        base_adapters=AdapterRegistry())

    async with Runtime(tmp_path / "state", plugin_catalog=catalog) as web:
        disabled_result = await web.read("https://catalog.test/item",
            WebPolicy(provider="blocked_ep"))
        duplicate_result = await web.read("https://catalog.test/item",
            WebPolicy(provider="duplicate_ep"))

    assert disabled.loads == duplicate_a.loads == duplicate_b.loads == 0
    assert disabled_result["receipt"]["failure"]["code"] == "PLUGIN_DISABLED"
    assert duplicate_result["receipt"]["failure"]["code"] == "PLUGIN_DISABLED"


@pytest.mark.asyncio
async def test_startup_catalog_pins_dynamic_default_manifests(monkeypatch, tmp_path):
    class MutableProvider:
        def __init__(self):
            self.manifest = ProviderManifest("frozen_provider", "1")

        async def acquire(self, request, services):
            content = "item:pinned"
            return {"url": request.url, "content": content,
                    "content_type": "text/plain", "raw": content.encode(),
                    "http_status": 200, "headers": {}, "cost_usd": 0}

    class MutableSearch:
        def __init__(self):
            self.manifest = SearchManifest("frozen_search", "1")

        async def search(self, request, services):
            raise AssertionError("manifest pinning does not execute search")

    class MutableAdapter:
        def __init__(self):
            self.manifest = AdapterManifest("frozen_adapter", "1")

        def extract(self, request):
            return {"title": "Pinned item", "text": request.content,
                    "image_urls": [], "structured": {"listing_id": "pinned"}}

    provider = MutableProvider()
    search = MutableSearch()
    adapter = MutableAdapter()
    providers, searches, adapters = ProviderRegistry(), SearchRegistry(), AdapterRegistry()
    providers.register(provider)
    searches.register(search)
    adapters.register(adapter)
    monkeypatch.setattr(provider_module, "DEFAULT_PROVIDERS", providers)
    monkeypatch.setattr(search_module, "DEFAULT_SEARCHES", searches)
    monkeypatch.setattr(adapter_module, "DEFAULT_ADAPTERS", adapters)

    # Omitted base registries must resolve the current module globals rather
    # than bindings captured when plugin_catalog was imported.
    catalog = build_plugin_catalog(config_path=tmp_path / "missing.json",
                                   entry_points=[])
    startup_snapshot = catalog.inspect()
    assert {(item["kind"], item["id"], item["version"])
            for item in startup_snapshot["plugins"]} == {
                ("provider", "frozen_provider", "1"),
                ("search", "frozen_search", "1"),
                ("adapter", "frozen_adapter", "1"),
            }

    async with Runtime(tmp_path / "state", plugin_catalog=catalog) as web:
        # Replacing manifests on the live source plugins after Runtime startup
        # must not change this Runtime's versions, routing, or paid policy.
        provider.manifest = ProviderManifest("frozen_provider", "99", paid=True)
        search.manifest = SearchManifest("frozen_search", "99", paid=True,
                                         transport_provider="missing_provider")
        adapter.manifest = AdapterManifest("frozen_adapter", "99")

        policy = WebPolicy(use_route_memory=False)
        assert web.inspect_plugins() == startup_snapshot
        assert web.providers.require_enabled("frozen_provider", policy).version == "1"
        assert web.searches.require_enabled("frozen_search", policy).version == "1"
        assert web.adapters.require_enabled("frozen_adapter").version == "1"
        assert web.providers.candidates(policy) == ["frozen_provider"]
        assert web.searches.candidates(policy, explicit="frozen_search") == [
            "frozen_search"]

        result = await web.extract("https://catalog.test/item", "frozen_adapter",
                                   policy=policy)

    assert result["receipt"]["status"] == "observed"
    assert result["receipt"]["method"] == "frozen_provider"
    assert result["receipt"]["provider_version"] == "1"
    assert result["receipt"]["adapter_version"] == "1"
    assert result["receipt"]["routing"]["provider_plan"]["ordered"] == [
        "frozen_provider"]
    assert result["structured"] == {"listing_id": "pinned"}
    assert catalog.inspect() == startup_snapshot


def test_catalog_execution_snapshots_isolate_nested_mutable_config(tmp_path):
    class ConfiguredAdapter:
        manifest = AdapterManifest("configured_adapter", "1")

        def __init__(self):
            self.config = {
                "nested": {"label": "startup"},
                "history": [],
            }

        def extract(self, request):
            self.config["history"].append(request.content)
            return {"text": request.content, "image_urls": [],
                    "structured": {
                        "label": self.config["nested"]["label"],
                        "history": list(self.config["history"]),
                    }}

    source = ConfiguredAdapter()
    adapters = AdapterRegistry(); adapters.register(source)

    def catalog():
        return build_plugin_catalog(
            config_path=tmp_path / "missing.json", entry_points=[],
            base_providers=ProviderRegistry(),
            base_searches=SearchRegistry(), base_adapters=adapters)

    first, second = catalog(), catalog()
    first_snapshot, second_snapshot = first.inspect(), second.inspect()
    source.config["nested"]["label"] = "mutated-source"

    from frankensurf.adapters import AdapterRequest
    request = lambda content: AdapterRequest(
        content, "text/plain", "https://catalog.test/item")
    first_session = first.open_session()
    second_session = second.open_session()
    first_result = first_session.adapters.project(
        "configured_adapter", request("first"))
    second_result = second_session.adapters.project(
        "configured_adapter", request("second"))
    first_again = first_session.adapters.project(
        "configured_adapter", request("first-again"))

    assert first_result["structured"] == {
        "label": "startup", "history": ["first"]}
    assert second_result["structured"] == {
        "label": "startup", "history": ["second"]}
    assert first_again["structured"] == {
        "label": "startup", "history": ["first", "first-again"]}
    assert source.config == {
        "nested": {"label": "mutated-source"}, "history": []}
    assert first.inspect() == first_snapshot
    assert second.inspect() == second_snapshot


def test_plugin_deepcopy_hook_is_never_used_and_unsafe_state_is_rejected(
        tmp_path):
    class MaliciousDeepcopy:
        manifest = AdapterManifest("safe_snapshot", "1")
        deepcopy_calls = 0

        def __init__(self):
            self.config = {"history": []}

        def __deepcopy__(self, memo):
            type(self).deepcopy_calls += 1
            return self

        def extract(self, request):
            self.config["history"].append(request.content)
            return {"text": request.content, "image_urls": [],
                    "structured": {"history": list(self.config["history"])}}

    class Nonisolatable:
        manifest = AdapterManifest("nonisolatable", "1")

        def __init__(self):
            self.opaque = object()

        def extract(self, request):
            raise AssertionError("nonisolatable plugin executed")

    safe = EntryPoint(
        "adapter", "safe_snapshot", "safe-snapshot-pkg", MaliciousDeepcopy())
    unsafe = EntryPoint(
        "adapter", "nonisolatable", "nonisolatable-pkg", Nonisolatable())
    path = policy_file(tmp_path / "config",
        trusted={"adapter": {
            "safe_snapshot": "safe-snapshot-pkg",
            "nonisolatable": "nonisolatable-pkg"}})
    catalog = build_plugin_catalog(config_path=path,
        entry_points=[safe, unsafe],
        base_providers=ProviderRegistry(), base_searches=SearchRegistry(),
        base_adapters=AdapterRegistry())

    assert catalog.adapters.contains("safe_snapshot")
    assert not catalog.adapters.contains("nonisolatable")
    assert MaliciousDeepcopy.deepcopy_calls == 0
    first, second = catalog.open_session(), catalog.open_session()
    from frankensurf.adapters import AdapterRequest
    request = lambda value: AdapterRequest(
        value, "text/plain", "https://catalog.test/item")
    assert first.adapters.project(
        "safe_snapshot", request("one"))["structured"]["history"] == ["one"]
    assert second.adapters.project(
        "safe_snapshot", request("two"))["structured"]["history"] == ["two"]
    assert MaliciousDeepcopy.deepcopy_calls == 0
    assert [(item.kind, item.plugin_id, item.code)
            for item in catalog.snapshot.rejected] == [
                ("adapter", "nonisolatable", "PLUGIN_BINDING_UNSAFE")]


@pytest.mark.asyncio
async def test_explicit_factory_sessions_are_independent_and_close_in_reverse(
        tmp_path):
    class LifecycleProvider:
        manifest = ProviderManifest("lifecycle_provider", "1")

        def __init__(self):
            self.calls = 0
            self.events = []

        async def start(self):
            self.binding_id = "f" * 64
            self.events.append("start")

        async def aclose(self):
            self.events.append("close")

        async def acquire(self, request, services):
            self.calls += 1
            content = str(self.calls)
            return {"url": request.url, "content": content,
                    "content_type": "text/plain", "raw": content.encode(),
                    "http_status": 200, "headers": {}, "cost_usd": 0}

    factory = PluginFactory(
        LifecycleProvider.manifest, (("mode", "fixture"),),
        LifecycleProvider)
    entry = EntryPoint("provider", "lifecycle_provider", "lifecycle-pkg",
                       factory)
    path = policy_file(tmp_path / "config", trusted={
        "provider": {"lifecycle_provider": "lifecycle-pkg"}})
    catalog = build_plugin_catalog(
        config_path=path, entry_points=[entry],
        base_providers=ProviderRegistry(), base_searches=SearchRegistry(),
        base_adapters=AdapterRegistry())
    binding_id = catalog.inspect()["plugins"][0]["binding_id"]
    assert len(binding_id) == 64

    first = Runtime(tmp_path / "first", plugin_catalog=catalog)
    second = Runtime(tmp_path / "second", plugin_catalog=catalog)
    first_plugin = first._plugin_session._executions[0]
    second_plugin = second._plugin_session._executions[0]
    async with first:
        first_result = await first.read(
            "https://catalog.test/one", WebPolicy(
                provider="lifecycle_provider", use_route_memory=False))
        async with second:
            second_result = await second.read(
                "https://catalog.test/two", WebPolicy(
                    provider="lifecycle_provider", use_route_memory=False))
            first_again = await first.read(
                "https://catalog.test/three", WebPolicy(
                    provider="lifecycle_provider", use_route_memory=False))

    assert first_result["content"] == "1"
    assert second_result["content"] == "1"
    assert first_again["content"] == "2"
    assert first_result["receipt"]["provider_binding_id"] == binding_id
    assert first_result["receipt"]["attempts"][0][
        "provider_binding_id"] == binding_id
    assert first_plugin.events == second_plugin.events == ["start", "close"]
    assert first_plugin is not second_plugin


@pytest.mark.asyncio
async def test_session_startup_rollback_and_body_cancellation_close_plugins(
        tmp_path):
    class Starts:
        manifest = ProviderManifest("starts", "1")

        def __init__(self):
            self.started_at = None
            self.closed_at = None

        async def start(self):
            self.started_at = time.monotonic_ns()

        async def aclose(self):
            self.closed_at = time.monotonic_ns()

        async def acquire(self, request, services):
            raise AssertionError("startup test does not acquire")

    class FailsStart:
        manifest = SearchManifest("fails_start", "1")

        def __init__(self):
            self.started_at = None
            self.closed_at = None

        async def start(self):
            self.started_at = time.monotonic_ns()
            raise RuntimeError("secret startup detail")

        async def aclose(self):
            self.closed_at = time.monotonic_ns()

        async def search(self, request, services):
            raise AssertionError("startup test does not search")

    providers = ProviderRegistry(); providers.register(Starts())
    searches = SearchRegistry(); searches.register(FailsStart())
    catalog = build_plugin_catalog(
        config_path=tmp_path / "missing.json", entry_points=[],
        base_providers=providers, base_searches=searches,
        base_adapters=AdapterRegistry())
    session = catalog.open_session()
    with pytest.raises(PluginCatalogError, match="startup failed") as caught:
        await session.start()
    assert "secret" not in str(caught.value)
    assert session.state == "closed"
    started_plugin, failed_plugin = session._executions
    assert started_plugin.started_at <= failed_plugin.started_at
    assert failed_plugin.closed_at <= started_plugin.closed_at

    searches = SearchRegistry()
    catalog = build_plugin_catalog(
        config_path=tmp_path / "missing.json", entry_points=[],
        base_providers=providers, base_searches=searches,
        base_adapters=AdapterRegistry())

    cancelled_runtime = Runtime(tmp_path / "cancelled", plugin_catalog=catalog)
    cancelled_plugin = cancelled_runtime._plugin_session._executions[0]

    async def cancelled_body():
        async with cancelled_runtime:
            asyncio.current_task().cancel()
            await asyncio.sleep(0)

    task = asyncio.create_task(cancelled_body())
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled_plugin.started_at is not None
    assert cancelled_plugin.closed_at is not None


@pytest.mark.asyncio
async def test_session_cancellation_finishes_startup_rollback_and_shutdown(
        tmp_path):
    class CancellableProvider:
        manifest = ProviderManifest("cancellable_provider", "1")

        def __init__(self):
            self.entered = asyncio.Event()
            self.events = []

        async def start(self):
            self.events.append("start")
            self.entered.set()
            await asyncio.Future()

        async def aclose(self):
            self.events.append("close")

        async def acquire(self, request, services):
            raise AssertionError("lifecycle test does not acquire")

    startup_factory = PluginFactory(
        CancellableProvider.manifest, (), CancellableProvider)
    startup_entry = EntryPoint(
        "provider", "cancellable_provider", "cancellable-pkg",
        startup_factory)
    startup_path = policy_file(tmp_path / "startup", trusted={
        "provider": {"cancellable_provider": "cancellable-pkg"}})
    startup_catalog = build_plugin_catalog(
        config_path=startup_path, entry_points=[startup_entry],
        base_providers=ProviderRegistry(), base_searches=SearchRegistry(),
        base_adapters=AdapterRegistry())
    startup_session = startup_catalog.open_session()
    startup_plugin = startup_session._executions[0]
    startup_task = asyncio.create_task(startup_session.start())
    await startup_plugin.entered.wait()
    startup_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await startup_task
    assert startup_session.state == "closed"
    assert startup_plugin.events == ["start", "close"]

    class SlowCloseProvider:
        manifest = ProviderManifest("slow_close_provider", "1")

        def __init__(self):
            self.close_entered = asyncio.Event()
            self.close_release = asyncio.Event()
            self.events = []

        async def aclose(self):
            self.events.append("close-entered")
            self.close_entered.set()
            await self.close_release.wait()
            self.events.append("close-finished")

        async def acquire(self, request, services):
            raise AssertionError("lifecycle test does not acquire")

    close_factory = PluginFactory(
        SlowCloseProvider.manifest, (), SlowCloseProvider)
    close_entry = EntryPoint(
        "provider", "slow_close_provider", "slow-close-pkg", close_factory)
    close_path = policy_file(tmp_path / "shutdown", trusted={
        "provider": {"slow_close_provider": "slow-close-pkg"}})
    close_catalog = build_plugin_catalog(
        config_path=close_path, entry_points=[close_entry],
        base_providers=ProviderRegistry(), base_searches=SearchRegistry(),
        base_adapters=AdapterRegistry())
    close_session = close_catalog.open_session()
    close_plugin = close_session._executions[0]
    await close_session.start()
    close_task = asyncio.create_task(close_session.close())
    await close_plugin.close_entered.wait()
    close_task.cancel()
    close_plugin.close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await close_task
    assert close_session.state == "closed"
    assert close_plugin.events == ["close-entered", "close-finished"]


def test_binding_fingerprint_changes_with_secret_free_configuration(tmp_path):
    class ConfiguredProvider:
        manifest = ProviderManifest("configured_provider", "1")

        async def acquire(self, request, services):
            raise AssertionError("fingerprint test does not acquire")

    def catalog(model, secret_version, distribution_version="7.1"):
        factory = PluginFactory(
            ConfiguredProvider.manifest,
            (("model_revision", model), ("api_token",
              PluginSecretReference("vault://provider/api", secret_version))),
            ConfiguredProvider)
        entry = EntryPoint(
            "provider", "configured_provider", "configured-pkg", factory)
        entry.dist.version = distribution_version
        path = policy_file(tmp_path / model / secret_version, trusted={
            "provider": {"configured_provider": "configured-pkg"}})
        return build_plugin_catalog(
            config_path=path, entry_points=[entry],
            base_providers=ProviderRegistry(),
            base_searches=SearchRegistry(), base_adapters=AdapterRegistry())

    first = catalog("model-1", "do-not-export-a")
    same = catalog("model-1", "do-not-export-a")
    changed_model = catalog("model-2", "do-not-export-a")
    changed_secret = catalog("model-1", "secret-v2")
    changed_package = catalog("model-1", "do-not-export-a", "7.2")
    ids = [item.inspect()["plugins"][0]["binding_id"] for item in (
        first, same, changed_model, changed_secret, changed_package)]
    assert ids[0] == ids[1]
    assert len({ids[0], ids[2], ids[3], ids[4]}) == 4
    serialized = json.dumps(first.inspect())
    assert "do-not-export" not in serialized
    assert first.inspect()["plugins"][0]["distribution_version"] == "7.1"


@pytest.mark.asyncio
async def test_browser_use_catalog_binding_rejects_configuration_drift(
        monkeypatch, tmp_path):
    from frankensurf import browser_use_config
    from frankensurf.browser_use_provider import BrowserUseProvider
    from frankensurf.providers import ProviderRequest

    def snapshot(fingerprint):
        return browser_use_config.BindingSnapshot(
            tmp_path / "factory.py", "factory", b"source", "unmetered",
            "revision", Path("/runtime/python"), Path("/runtime/browser"),
            fingerprint)

    active = [snapshot("a" * 64)]
    monkeypatch.setattr(browser_use_config, "snapshot", lambda: active[0])
    providers = ProviderRegistry(); providers.register(BrowserUseProvider())
    catalog = build_plugin_catalog(
        config_path=tmp_path / "missing.json", entry_points=[],
        base_providers=providers, base_searches=SearchRegistry(),
        base_adapters=AdapterRegistry())
    original_id = catalog.inspect()["plugins"][0]["binding_id"]

    active[0] = snapshot("b" * 64)
    session = catalog.open_session()
    await session.start()
    with pytest.raises(WebFailure) as caught:
        await session.providers.acquire(
            "browser_use", ProviderRequest("https://catalog.test/item",
                WebPolicy(provider="browser_use", allow_local_browser=True)),
            None)
    await session.close()
    assert caught.value.code == "PROVIDER_UNAVAILABLE"

    changed = ProviderRegistry(); changed.register(BrowserUseProvider())
    changed_catalog = build_plugin_catalog(
        config_path=tmp_path / "missing.json", entry_points=[],
        base_providers=changed, base_searches=SearchRegistry(),
        base_adapters=AdapterRegistry())
    assert changed_catalog.inspect()["plugins"][0][
        "binding_id"] != original_id


@pytest.mark.asyncio
async def test_catalog_pins_slotted_plugin_execution_instance(tmp_path):
    class SlottedProvider:
        __slots__ = ()
        manifest = ProviderManifest("slotted_provider", "1")

        async def acquire(self, request, services):
            content = f"{self.manifest.id}:{self.manifest.version}"
            return {"url": request.url, "content": content,
                "content_type": "text/plain", "raw": content.encode(),
                "http_status": 200, "headers": {}, "cost_usd": 0}

    providers = ProviderRegistry()
    providers.register(SlottedProvider())
    catalog = build_plugin_catalog(config_path=tmp_path / "missing.json",
        entry_points=[], base_providers=providers,
        base_searches=SearchRegistry(), base_adapters=AdapterRegistry())

    async with Runtime(tmp_path / "state", plugin_catalog=catalog) as web:
        result = await web.read("https://catalog.test/item",
            WebPolicy(provider="slotted_provider", use_route_memory=False))

    assert result["receipt"]["status"] == "observed"
    assert result["receipt"]["provider_version"] == "1"
    assert result["content"] == "slotted_provider:1"


@pytest.mark.asyncio
async def test_stateful_nonweak_factory_is_independent_and_close_failure_finalizes(
        tmp_path):
    class SlottedProvider:
        __slots__ = ("counter",)
        manifest = ProviderManifest("stateful_slotted", "1")

        def __init__(self):
            self.counter = 0

        async def acquire(self, request, services):
            self.counter += 1
            content = str(self.counter)
            return {"url": request.url, "content": content,
                "content_type": "text/plain", "raw": content.encode(),
                "http_status": 200, "headers": {}, "cost_usd": 0}

    factory = PluginFactory(
        SlottedProvider.manifest, (("mode", "fixture"),), SlottedProvider)
    entry = EntryPoint(
        "provider", "stateful_slotted", "stateful-slotted-pkg", factory)
    path = policy_file(tmp_path / "config", trusted={
        "provider": {"stateful_slotted": "stateful-slotted-pkg"}})
    catalog = build_plugin_catalog(
        config_path=path, entry_points=[entry],
        base_providers=ProviderRegistry(), base_searches=SearchRegistry(),
        base_adapters=AdapterRegistry())

    first = Runtime(tmp_path / "first", plugin_catalog=catalog)
    second = Runtime(tmp_path / "second", plugin_catalog=catalog)
    policy = WebPolicy(provider="stateful_slotted", use_route_memory=False)
    async with first:
        first_one = await first.read("https://catalog.test/one", policy)
        first_two = await first.read("https://catalog.test/two", policy)
    async with second:
        second_one = await second.read("https://catalog.test/three", policy)
    assert [first_one["content"], first_two["content"],
            second_one["content"]] == ["1", "2", "1"]

    class BadCloseProvider:
        manifest = ProviderManifest("bad_close", "1")

        def __init__(self):
            self.close_calls = 0

        async def aclose(self):
            self.close_calls += 1
            raise RuntimeError("secret close detail")

        async def acquire(self, request, services):
            raise AssertionError("close test does not acquire")

    providers = ProviderRegistry(); providers.register(BadCloseProvider())
    closing = build_plugin_catalog(
        config_path=tmp_path / "missing.json", entry_points=[],
        base_providers=providers, base_searches=SearchRegistry(),
        base_adapters=AdapterRegistry()).open_session()
    closing_plugin = closing._executions[0]
    await closing.start()
    with pytest.raises(PluginCatalogError, match="shutdown failed") as caught:
        await closing.close()
    assert "secret" not in str(caught.value)
    assert closing.state == "closed"
    await closing.close()
    assert closing_plugin.close_calls == 1


def test_invalid_runtime_arguments_do_not_materialize_factory(tmp_path):
    class ConstructorProbe:
        manifest = ProviderManifest("constructor_probe", "1")
        constructed = 0

        def __init__(self):
            type(self).constructed += 1

        async def acquire(self, request, services):
            raise AssertionError("constructor probe does not acquire")

    factory = PluginFactory(ConstructorProbe.manifest, (), ConstructorProbe)
    entry = EntryPoint(
        "provider", "constructor_probe", "constructor-probe-pkg", factory)
    path = policy_file(tmp_path / "config", trusted={
        "provider": {"constructor_probe": "constructor-probe-pkg"}})
    catalog = build_plugin_catalog(
        config_path=path, entry_points=[entry],
        base_providers=ProviderRegistry(), base_searches=SearchRegistry(),
        base_adapters=AdapterRegistry())
    assert ConstructorProbe.constructed == 0
    with pytest.raises(ValueError, match="invalid concurrency"):
        Runtime(tmp_path / "state", plugin_catalog=catalog, concurrency=0)
    assert ConstructorProbe.constructed == 0


@pytest.mark.asyncio
async def test_shared_catalog_pins_delegated_manifest_behavior_across_runtimes(
        tmp_path):
    class DynamicProvider:
        current_manifest = ProviderManifest("stable_provider", "1")

        @property
        def manifest(self):
            return type(self).current_manifest

        async def acquire(self, request, services):
            manifest = self.manifest
            content = f"{manifest.id}:{manifest.version}:{manifest.paid}"
            return {"url": request.url, "content": content,
                "content_type": "text/plain", "raw": content.encode(),
                "http_status": 200, "headers": {}, "cost_usd": 0}

    class DynamicAdapter:
        def __init__(self):
            self.manifest = AdapterManifest("stable_adapter", "1")

        def extract(self, request):
            manifest = self.manifest
            return {"title": manifest.id, "text": request.content,
                "image_urls": [], "structured": {
                    "adapter": manifest.id, "adapter_version": manifest.version,
                    "provider_projection": request.content}}

    class DynamicSearch:
        def __init__(self):
            self.manifest = SearchManifest("stable_search", "1")

        async def search(self, request, services):
            manifest = self.manifest
            request_url = "https://catalog.test/search?q=pinned"
            attribution = {"query": request.query, "adapter": manifest.id,
                "source": manifest.id, "source_version": manifest.version,
                "request_url": request_url}
            receipt = {"trace_id": "d" * 32, "status": "observed",
                "failure": None, "attempts": [], "evidence": [],
                "cost_usd": 0, "latency_ms": 0}
            response = {"query": request.query, "source": manifest.id,
                "results": [], "receipt": receipt,
                "query_attribution": attribution, "coverage": "unknown",
                "upstream_failures": []}
            return {"response": response,
                    "acquisition": {"url": request_url, "receipt": receipt}}

    provider, search, adapter = (DynamicProvider(), DynamicSearch(),
                                 DynamicAdapter())
    providers = ProviderRegistry(); providers.register(provider)
    searches = SearchRegistry(); searches.register(search)
    adapters = AdapterRegistry(); adapters.register(adapter)
    catalog = build_plugin_catalog(config_path=tmp_path / "missing.json",
        entry_points=[], base_providers=providers,
        base_searches=searches, base_adapters=adapters)
    snapshot = catalog.inspect()

    DynamicProvider.current_manifest = ProviderManifest(
        "mutated_provider", "99", paid=True)
    search.manifest = SearchManifest(
        "mutated_search", "99", paid=True,
        transport_provider="missing_provider")
    adapter.manifest = AdapterManifest("mutated_adapter", "99")

    for number in (1, 2):
        async with Runtime(tmp_path / f"state-{number}",
                           plugin_catalog=catalog) as web:
            extracted = await web.extract("https://catalog.test/item",
                "stable_adapter", policy=WebPolicy(
                    provider="stable_provider", use_route_memory=False))
            searched = await web.search("pinned", source="stable_search")
            assert web.inspect_plugins() == snapshot
        assert extracted["receipt"]["status"] == "observed"
        assert extracted["structured"] == {
            "adapter": "stable_adapter", "adapter_version": "1",
            "provider_projection": "stable_provider:1:False"}
        assert searched["receipt"]["status"] == "observed"
        assert searched["source"] == "stable_search"

    assert DynamicProvider.current_manifest.id == "mutated_provider"
    assert search.manifest.id == "mutated_search"
    assert adapter.manifest.id == "mutated_adapter"
    assert catalog.inspect() == snapshot
