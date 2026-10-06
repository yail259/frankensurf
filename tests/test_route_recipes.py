"""Operator routes exercise ordinary API/MCP calls, authority and cache boundaries."""
import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
import sys

import pytest

from frankensurf import Runtime, WebPolicy
from frankensurf import providers
from frankensurf.providers import ProviderManifest, ProviderRegistry
from frankensurf.routes import PublicRouteRecipe, RouteRecipeError, RouteRecipeRegistry, request_policy
from frankensurf.runtime import WebFailure

TARGET = "https://example.test/items/123"


@pytest.fixture
def plugins(monkeypatch):
    calls = []
    class Plugin:
        def __init__(self, identifier, *, opt_in=False):
            self.manifest = ProviderManifest(identifier, "1", rendering=True,
                                             requires_local_browser=opt_in)
            self.opt_in = opt_in
            self.failure = None
            self.failure_sequence = []
        def available(self, configured):
            return not self.opt_in
        async def acquire(self, request, services):
            calls.append((self.manifest.id, request.policy))
            failure = (self.failure_sequence.pop(0)
                       if self.failure_sequence else self.failure)
            if failure:
                raise WebFailure(failure, "test provider unavailable")
            return {"url": request.url, "content": "<html><title>" + self.manifest.id + "</title><p>" + "observed evidence " * 12 + "</p></html>",
                    "content_type": "text/html", "http_status": 200,
                    **({"content_readiness": {"status": "timed_out",
                        "timeout_seconds":
                            request.policy.content_ready_timeout_seconds}}
                       if request.policy.content_ready_selector else {})}
    registry = ProviderRegistry()
    baseline, configured = Plugin("http"), Plugin("recipe_browser", opt_in=True)
    registry.register(baseline)
    registry.register(configured)
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    return registry, baseline, configured, calls


def recipe(**changes):
    values = {"id": "operator_entry", "version": "1", "origin": "https://example.test",
        "path_pattern": r"/items/[0-9]+", "operation": "extract", "provider": "recipe_browser",
        "provider_version": "1", "adapter": "html", "adapter_version": "legacy",
        "query_keys": ("q",), "policy_defaults": {"public_entry_url": "https://example.test/",
            "timeout_seconds": 80, "settle_ms": 8000},
        "provenance": {"source_commit": "a" * 40, "report_sha256": "b" * 64,
            "case_ids": ("entry-control",)}}
    values.update(changes)
    return PublicRouteRecipe(**values)


def web_at(tmp_path):
    return Runtime(tmp_path, identity_registry=tmp_path / "empty-identities.json")


async def test_registered_recipe_executes_through_plain_extract_and_survives_restart(tmp_path, plugins):
    async with web_at(tmp_path) as web:
        info = web.routes.register(recipe())
        result = await web.extract(TARGET, "html")
    async with web_at(tmp_path) as web:
        again = await web.extract(TARGET, "html")
    calls = plugins[-1]
    assert [identifier for identifier, _ in calls] == ["recipe_browser", "recipe_browser"]
    assert all(policy.public_entry_url == "https://example.test/" and policy.timeout_seconds == 80
               and policy.settle_ms == 8000 for _, policy in calls)
    assert result["title"] == again["title"] == "recipe_browser"
    assert result["receipt"]["adapter"] == "html"
    assert result["receipt"]["route_recipe"]["basis"] == "explicit operator configuration"
    assert info["reliability"] is None
    assert result["receipt"]["routing"]["operator_recipes"]["automatic_promotion"] is False
    config = tmp_path / "routes" / "recipes.json"
    assert config.stat().st_mode & 0o777 == 0o600
    assert (config.parent.stat().st_mode & 0o077) == 0


async def test_recipe_retry_defaults_reach_effective_core_policy(
        tmp_path, plugins, monkeypatch):
    defaults = dict(recipe().policy_defaults)
    defaults.update({
        "provider_max_attempts_per_candidate": 3,
        "provider_retry_delay_seconds": 0.25,
        "provider_retry_failures": ("TIMEOUT",),
    })
    plugins[2].failure_sequence = ["TIMEOUT", "TIMEOUT"]
    delays = []

    async def capture_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", capture_sleep)
    async with web_at(tmp_path) as web:
        web.routes.register(recipe(policy_defaults=defaults))
        result = await web.extract(TARGET, "html")

    assert result["receipt"]["status"] == "observed"
    assert len(plugins[-1]) == 3
    actual = plugins[-1][-1][1]
    assert actual.provider_max_attempts_per_candidate == 3
    assert actual.provider_retry_delay_seconds == 0.25
    assert actual.provider_retry_failures == ("TIMEOUT",)
    assert delays == [0.25, 0.25]
    assert [attempt["status"] for attempt in result["receipt"]["attempts"]] == [
        "failed", "failed", "observed"]


async def test_no_registration_leaves_legacy_key_and_route_unchanged(tmp_path, plugins):
    async with web_at(tmp_path) as web:
        policy = WebPolicy()
        key = web._cache_key(TARGET, "html", policy)
        plain = await web.extract(TARGET, "html")
        legacy = await web._read(TARGET, policy, adapter="html")
        assert key == web._cache_key(TARGET, "html", policy, route_scope=None)
    assert not (tmp_path / "routes").exists()
    assert plain["title"] == legacy["title"] == "http"
    assert "route_recipe" not in plain["receipt"]
    assert "operator_recipes" not in plain["receipt"].get("routing", {})


@pytest.mark.parametrize("target", ["https://other.test/items/123", "https://example.test/items/123/extra",
    "https://example.test/other/123", "https://example.test/items/123#state",
    "http://example.test/items/123", "https://example.test/items/123?unknown=ignored",
    "https://example.test/items/123?q=first&q=second", "https://user:password@example.test/items/123"])
def test_exact_origin_full_path_query_key_and_credentials_scope(target):
    assert not recipe().matches(target, "extract", "html")


def test_recipe_metadata_and_journal_do_not_copy_query_values(tmp_path):
    registry = RouteRecipeRegistry(tmp_path / "routes" / "recipes.json")
    item = recipe()
    registry.register(item)
    assert item.matches(TARGET + "?q=private-query", "extract", "html")
    raw = registry.path.read_text()
    assert "private-query" not in raw
    assert "private-query" not in json.dumps(registry.inspect())
    assert "public_entry_url" in registry.inspect()[0]["policy_fields"]
    assert "https://example.test/" not in json.dumps(registry.inspect())


@pytest.mark.parametrize("defaults", [{"identity": "owner"}, {"provider": "http"},
    {"provider_candidates": ["http"]}, {"allow_local_browser": True}, {"allow_paid_fallbacks": True},
    {"freshness": "cached"}, {"terminal_failures": []}, {"context_stop_failures": []},
    {"public_entry_continue_failures": []}, {"use_route_memory": False},
    {"scrapling_solve_cloudflare": True}, {"public_press_hold_attempts": 1},
    {"ebay_capture_pages": True}, {"reverb_capture_pages": True}, {"gumtree_capture_pages": True},
    {"navigation_page": 2}, {"public_entry_url": "https://example.test/?q=private-query"},
    {"public_entry_url": "https://other.test/"}, {"timeout_seconds": float("nan")},
    {"wait_selector": object()}])
def test_recipes_cannot_grant_authority_or_store_unbounded_workflow_or_query_values(defaults):
    with pytest.raises(RouteRecipeError):
        recipe(policy_defaults=defaults)


async def test_operation_and_requested_adapter_never_change(tmp_path, plugins):
    async with web_at(tmp_path) as web:
        web.routes.register(recipe())
        read = await web.read(TARGET)
        extracted = await web.extract(TARGET, "json")
    assert read["title"] == "http"
    assert extracted["receipt"]["adapter"] == "json"
    assert [identifier for identifier, _ in plugins[-1]] == ["http", "http"]


@pytest.mark.parametrize("overrides", [{"timeout_seconds": 5}, {"public_entry_url": None},
    {"settle_ms": 0}])
async def test_explicit_dict_cap_null_and_zero_prevent_recipe_conflicts(tmp_path, plugins, overrides):
    async with web_at(tmp_path) as web:
        web.routes.register(recipe())
        result = await web.extract(TARGET, "html", policy_overrides=overrides)
    assert [identifier for identifier, _ in plugins[-1]] == ["http"]
    actual = plugins[-1][0][1]
    assert all(getattr(actual, key) == value for key, value in overrides.items())
    assert result["receipt"]["routing"]["operator_recipes"]["skipped"][0]["reason"] == "EXPLICIT_POLICY_CONFLICT"


async def test_false_permission_and_whole_policy_are_protected(tmp_path, plugins):
    async with web_at(tmp_path) as web:
        web.routes.register(recipe())
        denied = await web.extract(TARGET, "html", policy_overrides={"allow_local_browser": False})
        whole = await web.extract(TARGET, "html", WebPolicy())
    assert [identifier for identifier, _ in plugins[-1]] == ["http", "http"]
    assert plugins[-1][0][1].allow_local_browser is False
    assert denied["receipt"]["routing"]["operator_recipes"]["skipped"][0]["reason"] == "POLICY_DENIED"
    assert whole["receipt"]["routing"]["operator_recipes"]["skipped"][0]["reason"] == "EXPLICIT_POLICY_CONFLICT"


@pytest.mark.parametrize("overrides", [{"provider": "http"}, {"provider_candidates": ["http"]}])
async def test_explicit_provider_decisions_bypass_even_invalid_recipe_registry(tmp_path, plugins, overrides):
    async with web_at(tmp_path) as web:
        web.routes.register(recipe())
        web.routes.path.write_text("bad operator data")
        result = await web.extract(TARGET, "html", policy_overrides=overrides)
    assert result["title"] == "http"
    assert "operator_recipes" not in result["receipt"].get("routing", {})


async def test_named_identity_never_falls_back_to_public_recipe(tmp_path, plugins):
    async with web_at(tmp_path) as web:
        web.routes.register(recipe())
        result = await web.extract(TARGET, "html", policy_overrides={"identity": "unenrolled"})
    assert not plugins[-1]
    assert result["receipt"]["failure"]["code"] == "IDENTITY_UNKNOWN"
    assert "route_recipe" not in result["receipt"]


@pytest.mark.parametrize("mismatch,reason", [({"provider_version": "old"}, "PROVIDER_VERSION_MISMATCH"),
    ({"adapter_version": "old"}, "ADAPTER_VERSION_MISMATCH"), ({"enabled": False}, "PLUGIN_DISABLED")])
async def test_version_and_enablement_must_bind_before_execution(tmp_path, plugins, mismatch, reason):
    async with web_at(tmp_path) as web:
        web.routes.register(recipe(**mismatch))
        result = await web.extract(TARGET, "html")
    assert [identifier for identifier, _ in plugins[-1]] == ["http"]
    assert result["receipt"]["routing"]["operator_recipes"]["skipped"][0]["reason"] == reason


async def test_recipe_update_or_disable_cannot_reuse_old_recipe_cache(tmp_path, plugins):
    async with web_at(tmp_path) as web:
        web.routes.register(recipe())
        first = await web.extract(TARGET, "html", policy_overrides={"freshness": "hour"})
        second = await web.extract(TARGET, "html", policy_overrides={"freshness": "hour"})
        assert not first["receipt"]["cache_hit"] and second["receipt"]["cache_hit"]
        web.routes.register(recipe(version="2"))
        updated = await web.extract(TARGET, "html", policy_overrides={"freshness": "hour"})
        assert not updated["receipt"]["cache_hit"]
        assert updated["receipt"]["route_recipe"]["version"] == "2"
        web.routes.enable("operator_entry", False)
        disabled = await web.extract(TARGET, "html", policy_overrides={"freshness": "hour"})
        plugins[0].enable("recipe_browser", False)
        web.routes.enable("operator_entry", True)
        provider_disabled = await web.extract(TARGET, "html", policy_overrides={"freshness": "hour"})
    assert disabled["title"] == "http"
    assert provider_disabled["title"] == "recipe_browser"
    assert [identifier for identifier, _ in plugins[-1]] == [
        "recipe_browser", "recipe_browser", "http"]
    assert provider_disabled["receipt"]["cache_hit"]
    assert provider_disabled["receipt"]["route_recipe"]["id"] == "operator_entry"


async def test_unavailable_recipe_falls_back_with_complete_attempts_and_no_duplicate_journal(tmp_path, plugins):
    plugins[2].failure = "PROVIDER_UNAVAILABLE"
    async with web_at(tmp_path) as web:
        web.routes.register(recipe())
        result = await web.extract(TARGET + "?q=private-query", "html")
    assert result["title"] == "http"
    assert [attempt["provider"] for attempt in result["receipt"]["attempts"]] == ["recipe_browser", "http"]
    assert result["receipt"]["attempts"][0]["failure"] == "PROVIDER_UNAVAILABLE"
    rows = (tmp_path / "route-observations.jsonl").read_text().splitlines()
    assert len(rows) == 2
    assert "private-query" not in "".join(rows)


async def test_explicit_terminal_stop_rule_stops_after_recipe_failure(tmp_path, plugins):
    plugins[2].failure = "BLOCKED"
    async with web_at(tmp_path) as web:
        web.routes.register(recipe())
        result = await web.extract(TARGET, "html", policy_overrides={"terminal_failures": ["BLOCKED"]})
    assert [identifier for identifier, _ in plugins[-1]] == ["recipe_browser"]
    assert result["receipt"]["failure"]["code"] == "BLOCKED"


async def test_invalid_recipe_file_returns_safe_typed_failure(tmp_path, plugins):
    async with web_at(tmp_path) as web:
        web.routes.register(recipe())
        web.routes.path.write_text('{"private-query": "invalid"}')
        result = await web.extract(TARGET, "html")
    assert not plugins[-1]
    assert result["receipt"]["failure"]["code"] == "ROUTE_CONFIG_INVALID"
    assert "private-query" not in json.dumps(result)


def test_private_config_rejects_symlinks_and_loose_permissions(tmp_path):
    target = tmp_path / "target.json"
    target.write_text('{"schema":"frankensurf.public-route-recipes/v1","recipes":[]}')
    registry = RouteRecipeRegistry(tmp_path / "link.json")
    registry.path.symlink_to(target)
    with pytest.raises(RouteRecipeError):
        registry.inspect()
    target.chmod(0o644)
    with pytest.raises(RouteRecipeError):
        RouteRecipeRegistry(target).inspect()


def test_atomic_failed_replace_keeps_previous_config_and_removes_owned_temp(tmp_path, monkeypatch):
    registry = RouteRecipeRegistry(tmp_path / "routes" / "recipes.json")
    registry.register(recipe())
    previous = registry.path.read_bytes()
    def fail_replace(*args):
        raise OSError("private filesystem error")
    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(RouteRecipeError):
        registry.register(recipe(version="2"))
    assert registry.path.read_bytes() == previous
    assert not list(registry.path.parent.glob(".route-recipes-*"))


def test_request_policy_preserves_presence_and_legacy_whole_policy():
    policy, supplied = request_policy(overrides={"scrapling_load_dom": False,
        "wait_selector": None, "settle_ms": 0, "compound_source_candidates": ["http"]})
    assert policy.scrapling_load_dom is False and policy.wait_selector is None and policy.settle_ms == 0
    assert supplied == {"scrapling_load_dom", "wait_selector", "settle_ms", "compound_source_candidates"}
    assert policy.compound_source_candidates == ("http",)
    _, whole = request_policy(WebPolicy())
    assert "identity" in whole and "timeout_seconds" in whole
    with pytest.raises(ValueError):
        request_policy(WebPolicy(), {})


async def test_mcp_plain_calls_execute_recipe_and_explicit_null_false_zero_are_preserved(tmp_path, plugins, monkeypatch):
    from frankensurf import mcp_server
    web_at(tmp_path).routes.register(recipe(policy_defaults={"scrapling_load_dom": True,
        "wait_selector": "#ready", "settle_ms": 8000}))
    monkeypatch.setattr(mcp_server, "runtime", lambda: web_at(tmp_path))
    ordinary = await mcp_server.extract(TARGET)
    explicit = await mcp_server.extract(TARGET, acquisition_policy={"scrapling_load_dom": False,
        "wait_selector": None, "settle_ms": 0})
    direct_read = await mcp_server.read(TARGET, render=False, acquisition_policy={"settle_ms": 0})
    assert ordinary["title"] == "recipe_browser" and "content" not in ordinary
    assert explicit["title"] == direct_read["title"] == "http"
    actual = plugins[-1][1][1]
    assert actual.scrapling_load_dom is False and actual.wait_selector is None and actual.settle_ms == 0
    assert plugins[-1][2][1].render is False
    tools = await mcp_server.server.list_tools()
    for name in ("read", "extract"):
        schema = next(tool for tool in tools if tool.name == name).inputSchema
        assert "acquisition_policy" in schema["properties"]
        assert schema["properties"]["include_images"]["default"] is None


async def test_stdio_plain_extract_executes_operator_configuration(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    RouteRecipeRegistry(tmp_path / "routes" / "recipes.json").register(recipe())
    child = """
from frankensurf import providers, mcp_server
from frankensurf.providers import ProviderManifest, ProviderRegistry
class Plugin:
    def __init__(self, name, opt_in=False):
        self.manifest = ProviderManifest(name, '1', rendering=True, requires_local_browser=opt_in)
        self.opt_in = opt_in
    def available(self, configured): return not self.opt_in
    async def acquire(self, request, services):
        title = self.manifest.id
        return {'url': request.url, 'content': '<title>' + title + '</title><p>exact public evidence</p>',
                'content_type': 'text/html', 'http_status': 200}
registry = ProviderRegistry()
registry.register(Plugin('http'))
registry.register(Plugin('recipe_browser', True))
providers.DEFAULT_PROVIDERS = registry
mcp_server.main()
"""
    env = dict(os.environ, FRANKENSURF_STATE=str(tmp_path),
        FRANKENSURF_IDENTITIES=str(tmp_path / "empty-identities.json"),
        PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    params = StdioServerParameters(command=sys.executable, args=["-c", child], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("extract", {"url": TARGET})
            assert not result.isError
            data = result.structuredContent or json.loads(result.content[0].text)
            assert data["title"] == "recipe_browser" and "content" not in data
            assert data["receipt"]["method"] == "recipe_browser"
            denied = await session.call_tool("extract", {"url": TARGET,
                "acquisition_policy": {"timeout_seconds": 5, "settle_ms": 0}})
            assert not denied.isError
            data = denied.structuredContent or json.loads(denied.content[0].text)
            assert data["title"] == "http"
            assert data["receipt"]["routing"]["operator_recipes"]["skipped"][0]["reason"] == "EXPLICIT_POLICY_CONFLICT"


async def test_recipe_fingerprint_scopes_route_memory_without_inherited_preference(tmp_path, plugins):
    from frankensurf.route_memory import acquisition_context, preferred_provider
    async with web_at(tmp_path) as web:
        web.routes.register(recipe())
        first = await web.extract(TARGET, "html")
        await web.extract(TARGET, "html")
        old_context = first["receipt"]["acquisition_context"]
        manifests = plugins[0].inspect()
        assert preferred_provider(tmp_path, TARGET, "html", "legacy", manifests,
            3600, 2, old_context) == "recipe_browser"
        web.routes.register(recipe(version="2"))
        updated = await web.extract(TARGET, "html")
        new_context = updated["receipt"]["acquisition_context"]
        ordinary_context = acquisition_context(replace(WebPolicy(), **dict(recipe().policy_defaults)))
        assert new_context != old_context and new_context != ordinary_context
        assert preferred_provider(tmp_path, TARGET, "html", "legacy", manifests,
            3600, 2, new_context) is None
        assert preferred_provider(tmp_path, TARGET, "html", "legacy", manifests,
            3600, 2, ordinary_context) is None
        assert "memory_provider" not in updated["receipt"].get("routing", {})
        assert "provider_plan" not in updated["receipt"].get("routing", {})


async def test_context_stop_rule_prevents_second_recipe_dispatch_or_fallback(tmp_path, plugins, monkeypatch):
    plugins[2].failure = "AUTH_REQUIRED"
    async with web_at(tmp_path) as web:
        web.routes.register(recipe(id="first"))
        web.routes.register(recipe(id="second"))
        dispatched = []
        original = web._read
        async def record(*args, **kwargs):
            dispatched.append(kwargs.get("_recipe", {}).get("id"))
            return await original(*args, **kwargs)
        monkeypatch.setattr(web, "_read", record)
        result = await web.extract(TARGET, "html", policy_overrides={"terminal_failures": []})
    assert dispatched == ["first"]
    assert [identifier for identifier, _ in plugins[-1]] == ["recipe_browser"]
    assert result["receipt"]["route_recipe"]["id"] == "first"
    assert result["receipt"]["failure"]["code"] == "AUTH_REQUIRED"
    assert result["receipt"]["routing"]["operator_recipes"]["prior_trace_ids"] == []


async def test_recipe_executes_nonfatal_content_readiness_without_site_code(
        tmp_path, plugins):
    item = recipe(policy_defaults={
        "timeout_seconds": 30,
        "content_ready_selector": "#buy:not([disabled]), .terminal-state",
        "content_ready_timeout_seconds": 12,
        "wait_state": "visible",
        "settle_ms": 0})
    async with web_at(tmp_path) as web:
        web.routes.register(item)
        result = await web.extract(TARGET, "html")
        explicit = await web.extract(TARGET, "html",
            policy_overrides={"content_ready_selector": None})
    assert result["content_readiness"] == {
        "status": "timed_out", "timeout_seconds": 12}
    assert result["receipt"]["content_readiness"] == result["content_readiness"]
    assert plugins[-1][0][1].content_ready_selector.startswith("#buy")
    assert plugins[-1][0][1].wait_state == "visible"
    assert explicit["receipt"]["method"] == "http"
    assert explicit["receipt"]["routing"]["operator_recipes"]["skipped"][0][
        "reason"] == "EXPLICIT_POLICY_CONFLICT"
