import json
import re
from dataclasses import dataclass, FrozenInstanceError
from types import FunctionType, ModuleType, SimpleNamespace
from urllib.parse import urljoin, urlsplit

import pytest

from frankensurf.adapters import AdapterManifest, AdapterRequest, DEFAULT_ADAPTERS
from frankensurf.plugin_catalog import (ENTRY_POINT_GROUPS, PLUGIN_KINDS,
    PLUGIN_POLICY_SCHEMA, PluginCatalogError, PluginConfigurationError,
    PluginSecretReference, build_plugin_catalog, load_plugin_policy)
from frankensurf.providers import DEFAULT_PROVIDERS, ProviderManifest
from frankensurf.search_plugins import DEFAULT_SEARCHES, SearchManifest
from frankensurf.runtime import WebFailure


class FakeEntryPoint:
    def __init__(self, kind, name, distribution, loaded):
        self.group = ENTRY_POINT_GROUPS[kind]
        self.name = name
        self.dist = SimpleNamespace(name=distribution, version="1")
        self.loaded = loaded
        self.calls = 0

    def load(self):
        self.calls += 1
        if isinstance(self.loaded, BaseException):
            raise self.loaded
        return self.loaded


def private_policy(tmp_path, *, trusted=None, disabled=None):
    trusted = trusted or {}
    disabled = disabled or {}
    value = {
        "schema": PLUGIN_POLICY_SCHEMA,
        "trusted": {kind: dict(trusted.get(kind, {})) for kind in PLUGIN_KINDS},
        "disabled": {kind: list(disabled.get(kind, ())) for kind in PLUGIN_KINDS},
    }
    tmp_path.chmod(0o700)
    path = tmp_path / "plugins.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


class FixtureProvider:
    manifest = ProviderManifest("fixture_provider", "2.1")
    secret = PluginSecretReference("vault://provider-secret", "1")

    async def acquire(self, request, services):
        return {"url": request.url, "content": "fixture", "content_type": "text/plain"}


class FixtureSearch:
    manifest = SearchManifest("fixture_search", "3")

    async def search(self, request, services):
        raise AssertionError("contract discovery must not execute plugin operations")


class FixtureAdapter:
    manifest = AdapterManifest("fixture_adapter", "4")

    def extract(self, request):
        return {"text": "fixture", "image_urls": [], "structured": {"fixture": True}}


def _bundled_adapter_bindings():
    return {item["id"]: item["binding_id"]
            for item in build_plugin_catalog(entry_points=()).inspect()[
                "plugins"]
            if item["kind"] == "adapter"}


def _bindings_or_rejection():
    try:
        return _bundled_adapter_bindings()
    except PluginCatalogError:
        return None


def test_entry_points_build_independent_registries_and_frozen_safe_snapshot(tmp_path):
    provider = FakeEntryPoint("provider", "fixture_provider", "fixture-provider-pkg",
                              FixtureProvider)
    search = FakeEntryPoint("search", "fixture_search", "fixture-search-pkg",
                            FixtureSearch())
    adapter = FakeEntryPoint("adapter", "fixture_adapter", "fixture-adapter-pkg",
                             lambda: FixtureAdapter())
    path = private_policy(tmp_path, trusted={
        "provider": {"fixture_provider": "fixture-provider-pkg"},
        "search": {"fixture_search": "fixture-search-pkg"},
        "adapter": {"fixture_adapter": "fixture-adapter-pkg"},
    })

    catalog = build_plugin_catalog(config_path=path,
                                   entry_points=[adapter, provider, search])

    assert catalog.providers.contains("fixture_provider")
    assert catalog.searches.contains("fixture_search")
    assert catalog.adapters.contains("fixture_adapter")
    assert not DEFAULT_PROVIDERS.contains("fixture_provider")
    assert not DEFAULT_SEARCHES.contains("fixture_search")
    assert not DEFAULT_ADAPTERS.contains("fixture_adapter")
    assert provider.calls == search.calls == adapter.calls == 1
    session = catalog.open_session()
    assert session.adapters.project("fixture_adapter", SimpleNamespace()) == {
        "text": "fixture", "image_urls": [], "structured": {"fixture": True}}

    inspected = catalog.inspect()
    external = [item for item in inspected["plugins"] if item["source"] == "entry_point"]
    assert [(item["kind"], item["id"], item["distribution"]) for item in external] == [
        ("adapter", "fixture_adapter", "fixture-adapter-pkg"),
        ("provider", "fixture_provider", "fixture-provider-pkg"),
        ("search", "fixture_search", "fixture-search-pkg"),
    ]
    assert "provider-secret" not in json.dumps(inspected)
    with pytest.raises(FrozenInstanceError):
        catalog.snapshot.schema = "changed"
    assert isinstance(catalog.snapshot.plugins, tuple)
    for registry, identifier in ((catalog.providers, "http"),
                                 (catalog.searches, "searxng"),
                                 (catalog.adapters, "html")):
        with pytest.raises(RuntimeError, match="registry is frozen"):
            registry.enable(identifier, False)
    mutable_clone = catalog.providers.clone()
    mutable_clone.enable("http", False)
    assert next(item for item in mutable_clone.inspect() if item["id"] == "http")["enabled"] is False
    assert next(item for item in catalog.providers.inspect() if item["id"] == "http")["enabled"] is True


def test_catalog_includes_evolving_typed_manifest_fields_in_binding(tmp_path):
    @dataclass(frozen=True)
    class FutureProviderManifest(ProviderManifest):
        operations: tuple[str, ...] = ("read", "extract")
        concurrency: int = 4

    class FutureProvider(FixtureProvider):
        manifest = FutureProviderManifest("future_provider", "5")

    entry = FakeEntryPoint("provider", "future_provider", "future-provider-pkg",
                           FutureProvider())
    path = private_policy(tmp_path, trusted={
        "provider": {"future_provider": "future-provider-pkg"}})
    catalog = build_plugin_catalog(config_path=path, entry_points=[entry])
    assert catalog.providers.contains("future_provider")
    record = next(item for item in catalog.inspect()["plugins"]
                  if item["id"] == "future_provider")
    assert record["version"] == "5"
    assert record["manifest"]["operations"] == ("read", "extract")
    assert record["manifest"]["concurrency"] == 4

    class ChangedFutureProvider(FixtureProvider):
        manifest = FutureProviderManifest(
            "future_provider", "5", concurrency=5)

    changed_entry = FakeEntryPoint(
        "provider", "future_provider", "future-provider-pkg",
        ChangedFutureProvider())
    changed = build_plugin_catalog(config_path=path,
                                   entry_points=[changed_entry])
    changed_record = next(item for item in changed.inspect()["plugins"]
                          if item["id"] == "future_provider")
    assert changed_record["binding_id"] != record["binding_id"]


def test_untrusted_and_disabled_plugins_are_filtered_before_import(tmp_path):
    untrusted = FakeEntryPoint("provider", "untrusted_provider", "unknown-pkg",
                               RuntimeError("must never be imported"))
    disabled = FakeEntryPoint("adapter", "disabled_adapter", "adapter-pkg",
                              RuntimeError("must never be imported"))
    path = private_policy(tmp_path,
        trusted={"adapter": {"disabled_adapter": "adapter-pkg"}},
        disabled={"provider": ["http"], "adapter": ["disabled_adapter"]})

    catalog = build_plugin_catalog(config_path=path,
                                   entry_points=[untrusted, disabled])

    assert untrusted.calls == disabled.calls == 0
    assert next(item for item in catalog.providers.inspect() if item["id"] == "http")["enabled"] is False
    assert next(item for item in DEFAULT_PROVIDERS.inspect() if item["id"] == "http")["enabled"] is True
    assert {(item["id"], item["code"]) for item in catalog.inspect()["rejected"]} == {
        ("untrusted_provider", "PLUGIN_NOT_TRUSTED"),
        ("disabled_adapter", "PLUGIN_DISABLED"),
    }


def test_duplicate_ids_are_rejected_before_load_and_order_independent(tmp_path):
    first = FakeEntryPoint("provider", "duplicate", "package-a", FixtureProvider())
    second = FakeEntryPoint("provider", "duplicate", "package-b", FixtureProvider())
    builtin_collision = FakeEntryPoint("provider", "http", "package-http", FixtureProvider())
    path = private_policy(tmp_path, trusted={"provider": {
        "duplicate": "package-a", "http": "package-http"}})

    left = build_plugin_catalog(config_path=path,
                                entry_points=[first, builtin_collision, second])
    right = build_plugin_catalog(config_path=path,
                                 entry_points=[second, builtin_collision, first])

    assert first.calls == second.calls == builtin_collision.calls == 0
    assert not left.providers.contains("duplicate")
    assert left.inspect() == right.inspect()
    rejected = [item for item in left.inspect()["rejected"]
                if item["code"] == "DUPLICATE_PLUGIN_ID"]
    assert [(item["id"], item["distribution"]) for item in rejected] == [
        ("duplicate", "package-a"), ("duplicate", "package-b"),
        ("http", "package-http")]


def test_load_contract_and_manifest_failures_are_sanitized(tmp_path):
    class WrongName(FixtureProvider):
        manifest = ProviderManifest("different_name", "1")

    exploding = FakeEntryPoint("provider", "exploding", "exploding-pkg",
                               RuntimeError("token=super-secret"))
    malformed = FakeEntryPoint("search", "malformed", "malformed-pkg", object())
    wrong_name = FakeEntryPoint("provider", "wrong_name", "wrong-name-pkg", WrongName())
    path = private_policy(tmp_path, trusted={
        "provider": {"exploding": "exploding-pkg", "wrong_name": "wrong-name-pkg"},
        "search": {"malformed": "malformed-pkg"},
    })

    catalog = build_plugin_catalog(config_path=path,
                                   entry_points=[wrong_name, malformed, exploding])
    serialized = json.dumps(catalog.inspect())

    assert "super-secret" not in serialized and "token=" not in serialized
    assert {(item["id"], item["code"]) for item in catalog.inspect()["rejected"]} == {
        ("exploding", "PLUGIN_LOAD_REJECTED"),
        ("malformed", "PLUGIN_LOAD_REJECTED"),
        ("wrong_name", "PLUGIN_LOAD_REJECTED"),
    }
    assert not catalog.providers.contains("wrong_name")
    assert not catalog.searches.contains("malformed")


def test_private_policy_rejects_loose_symlinked_or_malformed_files(tmp_path):
    valid = private_policy(tmp_path)
    valid.chmod(0o644)
    with pytest.raises(PluginConfigurationError, match="permissions invalid"):
        load_plugin_policy(valid)

    valid.chmod(0o600)
    link = tmp_path / "linked.json"
    link.symlink_to(valid)
    with pytest.raises(PluginConfigurationError, match="permissions invalid"):
        load_plugin_policy(link)

    valid.write_text('{"schema":"frankensurf.plugin-policy/v1","schema":"duplicate"}',
                     encoding="utf-8")
    with pytest.raises(PluginConfigurationError, match="schema invalid"):
        load_plugin_policy(valid)


def test_missing_policy_preserves_builtins_and_does_not_trust_installed_code(tmp_path):
    installed = FakeEntryPoint("provider", "fixture_provider", "fixture-provider-pkg",
                               RuntimeError("untrusted code must not load"))
    catalog = build_plugin_catalog(config_path=tmp_path / "missing.json",
                                   entry_points=[installed])
    assert installed.calls == 0
    def without_bindings(rows):
        return [{key: value for key, value in row.items()
                 if key != "binding_id"} for row in rows]
    assert without_bindings(catalog.providers.inspect()) == DEFAULT_PROVIDERS.inspect()
    assert without_bindings(catalog.searches.inspect()) == DEFAULT_SEARCHES.inspect()
    assert without_bindings(catalog.adapters.inspect()) == (
        DEFAULT_ADAPTERS.inspect())
    assert catalog.inspect()["rejected"] == [{
        "kind": "provider", "id": "fixture_provider",
        "code": "PLUGIN_NOT_TRUSTED", "distribution": "fixture-provider-pkg"}]


def test_catalog_snapshot_records_behavioral_registration_order(tmp_path):
    from frankensurf.adapters import AdapterRegistry
    from frankensurf.providers import ProviderRegistry
    from frankensurf.search_plugins import SearchRegistry

    class OrderedProvider:
        def __init__(self, identifier):
            self.manifest = ProviderManifest(identifier, "1")

        async def acquire(self, request, services):
            raise AssertionError("order test does not acquire")

    def catalog(order):
        providers = ProviderRegistry()
        for identifier in order:
            providers.register(OrderedProvider(identifier))
        return build_plugin_catalog(
            config_path=tmp_path / "missing.json", entry_points=[],
            base_providers=providers, base_searches=SearchRegistry(),
            base_adapters=AdapterRegistry())

    first = catalog(("aa", "bb")).inspect()
    second = catalog(("bb", "aa")).inspect()
    first_order = {item["id"]: item["registration_index"]
                   for item in first["plugins"]}
    second_order = {item["id"]: item["registration_index"]
                    for item in second["plugins"]}
    assert first_order == {"aa": 0, "bb": 1}
    assert second_order == {"aa": 1, "bb": 0}
    assert first != second
