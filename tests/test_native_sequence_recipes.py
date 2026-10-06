"""Native recipes retain leaf contracts, authority, raw sources and acquisition cost."""
import copy
import json
import runpy
from dataclasses import replace
from pathlib import Path

import pytest
from frankensurf import Runtime, WebPolicy
from frankensurf import providers, adapters
from frankensurf.providers import ProviderManifest, ProviderRegistry
from frankensurf.adapters import AdapterManifest, AdapterRegistry
from frankensurf.routes import PublicRouteRecipe, RouteRecipeError
from frankensurf.pagination import native_sequence_pages, SequencePage
from frankensurf.runtime import WebFailure

URL = "https://www.depop.com/search/?q=camera"


COMMON = "https://example.test/search?q=camera"
NEXT = "https://example.test/next?q=camera&after=2"
THIRD = "https://example.test/next?q=camera&after=3"


def common_packet():
    return {"pages": [
        {"url": COMMON, "content_type": "application/json", "http_status": 200,
         "content": json.dumps({"leaf": "first", "listings": [{"listing_id": "1", "title": "One"},
             {"listing_id": "2", "title": "Two", "promotion_claim": "unobserved"}], "next_url": NEXT})},
        {"url": NEXT, "content_type": "application/json", "http_status": 200,
         "content": json.dumps({"leaf": "continuation", "listings": [{"listing_id": "2", "title": "Two"},
             {"listing_id": "3", "title": "Three"}], "next_url": THIRD})},
    ]}


def common_recipe(**changes):
    values = dict(id="native_common", version="1", origin="https://example.test", path_pattern="/search",
        operation="paginate", representation="native_sequence", provider="native", provider_version="1",
        adapter="first_leaf", adapter_version="11", continuation_adapter="continued_leaf",
        continuation_adapter_version="22", acquisition_adapter="native_capture", acquisition_adapter_version="33",
        query_keys=("q",))
    values.update(changes)
    return PublicRouteRecipe(**values)


@pytest.fixture
def common_plugins(monkeypatch):
    state = {"packet": common_packet(), "cost": 0.2, "calls": [], "failure": None, "hook": None}
    class Provider:
        def __init__(self, native):
            self.native = native
            self.manifest = ProviderManifest("native" if native else "http", "1", rendering=True,
                                             navigation=native, requires_local_browser=native)
        def available(self, configured): return not self.native
        async def acquire(self, request, services):
            state["calls"].append((self.manifest.id, request))
            if self.native and state["hook"]: state["hook"]()
            if self.native and state["failure"]: raise WebFailure(state["failure"], "Controlled native failure")
            if self.native:
                content = json.dumps(state["packet"])
            else:
                content = common_packet()["pages"][0 if request.url == COMMON else 1]["content"]
            result = {"url": request.url, "content": content, "content_type": "application/json", "http_status": 200}
            if self.native: result["cost_usd"] = state["cost"]
            elif "baseline_cost" in state:
                cost = state["baseline_cost"]
                if request.policy.max_cost_usd is not None and cost > request.policy.max_cost_usd:
                    raise WebFailure("BUDGET_EXHAUSTED", "Controlled paid call stopped before charge")
                state["charged_baseline_calls"] = state.get("charged_baseline_calls", 0) + 1
                result["cost_usd"] = cost
            if self.native and "raw" in state: result["raw"] = state["raw"]
            return result
    class Leaf:
        def __init__(self, first):
            self.first = first
            self.manifest = AdapterManifest("first_leaf" if first else "continued_leaf", "11" if first else "22")
        def extract(self, request):
            packet = json.loads(request.content)
            if packet.get("leaf") != ("first" if self.first else "continuation"):
                raise WebFailure("SCHEMA_CHANGED", "Requested leaf schema did not match")
            return {"title": "Controlled leaf", "text": "Leaf", "image_urls": [], "structured": packet}
    class NativeAdapter:
        manifest = AdapterManifest("native_capture", "33")
        def extract(self, request):
            # Acquisition claims are deliberately insufficient to certify leaf truth.
            return {"title": "Native claim", "text": "Claim", "image_urls": [],
                    "structured": {"listings": [{"listing_id": "unverified-native-claim"}]}}
        def sequence_pages(self, request): return native_sequence_pages(request)
    provider_registry = ProviderRegistry(); native_provider = Provider(True)
    provider_registry.register(native_provider); provider_registry.register(Provider(False))
    adapter_registry = AdapterRegistry(); native_adapter = NativeAdapter()
    adapter_registry.register(Leaf(True)); adapter_registry.register(Leaf(False)); adapter_registry.register(native_adapter)
    adapter_registry.register(adapters.LegacyAdapter("html")); adapter_registry.register(adapters.LegacyAdapter("json"))
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", provider_registry)
    monkeypatch.setattr(adapters, "DEFAULT_ADAPTERS", adapter_registry)
    state.update(provider_registry=provider_registry, adapter_registry=adapter_registry,
                 native_provider=native_provider, native_adapter=native_adapter)
    return state


async def configured_common(tmp_path, state, *, recipe_changes=None, overrides=None):
    async with Runtime(tmp_path, identity_registry=tmp_path / "unused-identities.json") as web:
        web.routes.register(common_recipe(**(recipe_changes or {})))
        return await web.paginate(COMMON, "first_leaf", continuation_adapter="continued_leaf",
                                  policy_overrides=overrides)


async def test_common_sequence_keeps_leaf_truth_and_one_aggregate_cost(tmp_path, common_plugins):
    result = await configured_common(tmp_path, common_plugins, overrides={"max_cost_usd": 0.3})
    receipt = result["receipt"]
    assert [name for name, _ in common_plugins["calls"]] == ["native"]
    assert [row["listing_id"] for row in result["listings"]] == ["1", "2", "3"]
    assert result["overlap_count"] == 1 and result["catalogue_complete"] is None
    assert receipt["adapter_version"] == "11" and receipt["continuation_adapter_version"] == "22"
    assert receipt["acquisition_adapter"] == "native_capture" and receipt["acquisition_adapter_version"] == "33"
    assert receipt["cost_usd"] == 0.2 and receipt["cost_budget_satisfied"] is True
    assert len(receipt["acquisitions"]) == len(receipt["evidence"]) == 1
    assert sum(page["receipt"]["cost_usd"] for page in result["pages"]) == receipt["cost_usd"]
    assert all(page["field_status"]["availability"] == "unknown" for page in result["pages"])
    assert all(page["receipt"]["source_page"]["artifact_sha256"] == receipt["evidence"][0]["sha256"] for page in result["pages"])


@pytest.mark.parametrize("field", ["adapter_version", "continuation_adapter_version", "acquisition_adapter_version", "provider_version"])
async def test_all_version_bindings_are_fingerprinted_and_checked_before_acquisition(tmp_path, common_plugins, field):
    original = common_recipe(); changed = replace(original, **{field: "old"})
    assert changed.fingerprint != original.fingerprint
    result = await configured_common(tmp_path, common_plugins, recipe_changes={field: "old"})
    assert not any(name == "native" for name, _ in common_plugins["calls"])
    reason = result["receipt"]["routing"]["operator_recipes"]["skipped"][0]["reason"]
    assert reason == ("PROVIDER_VERSION_MISMATCH" if field == "provider_version" else "ADAPTER_VERSION_MISMATCH")


@pytest.mark.parametrize("disabled", ["recipe", "provider", "acquisition_adapter"])
async def test_disabled_native_configuration_is_not_executed(tmp_path, common_plugins, disabled):
    changes = {}
    if disabled == "recipe": changes["enabled"] = False
    if disabled == "provider": common_plugins["provider_registry"].enable("native", False)
    if disabled == "acquisition_adapter": common_plugins["adapter_registry"].enable("native_capture", False)
    result = await configured_common(tmp_path, common_plugins, recipe_changes=changes)
    assert not any(name == "native" for name, _ in common_plugins["calls"])
    assert result["receipt"]["routing"]["operator_recipes"]["skipped"][0]["reason"] == "PLUGIN_DISABLED"


async def test_missing_sequence_protocol_has_typed_skip_without_native_acquisition(tmp_path, common_plugins):
    common_plugins["native_adapter"].sequence_pages = None
    result = await configured_common(tmp_path, common_plugins)
    assert [name for name, _ in common_plugins["calls"]] == ["http", "http"]
    assert result["receipt"]["routing"]["operator_recipes"]["skipped"][0]["reason"] == "SEQUENCE_UNSUPPORTED"


async def test_caller_page_limit_and_false_local_permission_are_preserved(tmp_path, common_plugins):
    result = await configured_common(tmp_path, common_plugins,
        recipe_changes={"policy_defaults": {"max_pages": 8, "timeout_seconds": 90}},
        overrides={"max_pages": 1, "allow_local_browser": False})
    assert len(result["pages"]) == 1 and common_plugins["calls"][0][1].policy.max_pages == 1
    assert not any(name == "native" for name, _ in common_plugins["calls"])
    assert result["receipt"]["routing"]["operator_recipes"]["skipped"][0]["reason"] == "EXPLICIT_POLICY_CONFLICT"


async def test_whole_policy_is_protected_and_omitted_defaults_are_applied(tmp_path, common_plugins):
    async with Runtime(tmp_path, identity_registry=tmp_path / "unused-identities.json") as web:
        web.routes.register(common_recipe(policy_defaults={"timeout_seconds": 90}))
        whole = await web.paginate(COMMON, "first_leaf", WebPolicy(), continuation_adapter="continued_leaf")
        omitted = await web.paginate(COMMON, "first_leaf", continuation_adapter="continued_leaf")
    assert whole["receipt"]["routing"]["operator_recipes"]["skipped"][0]["reason"] == "EXPLICIT_POLICY_CONFLICT"
    native_request = next(request for name, request in common_plugins["calls"] if name == "native")
    assert native_request.policy.timeout_seconds == 90 and omitted["native_sequence"] is True


async def test_named_identity_and_explicit_provider_bypass_even_invalid_registry(tmp_path, common_plugins):
    async with Runtime(tmp_path, identity_registry=tmp_path / "unused-identities.json") as web:
        web.routes.path.parent.mkdir(parents=True); web.routes.path.write_text("invalid")
        named = await web.paginate(COMMON, "first_leaf", continuation_adapter="continued_leaf", policy_overrides={"identity": "unenrolled"})
        explicit = await web.paginate(COMMON, "first_leaf", continuation_adapter="continued_leaf", policy_overrides={"provider": "http"})
    assert named["failure"]["code"] == "IDENTITY_UNKNOWN"
    assert explicit["status"] == "page_limit_reached"
    assert [name for name, _ in common_plugins["calls"]] == ["http", "http"]


async def test_sequence_recipe_never_changes_ordinary_read_or_extract_adapter(tmp_path, common_plugins):
    async with Runtime(tmp_path, identity_registry=tmp_path / "unused-identities.json") as web:
        web.routes.register(common_recipe())
        read = await web.read(COMMON)
        extracted = await web.extract(COMMON, "json")
    assert [name for name, _ in common_plugins["calls"]] == ["http", "http"]
    assert read["structured"]["leaf"] == extracted["structured"]["leaf"] == "first"
    assert extracted["receipt"]["adapter"] == "json"
    assert "page_sequence" not in read and "route_recipe" not in extracted["receipt"]


async def test_leaf_schema_conflict_remains_failed_partial_traversal(tmp_path, common_plugins):
    content = json.loads(common_plugins["packet"]["pages"][1]["content"]); content["leaf"] = "wrong"
    common_plugins["packet"]["pages"][1]["content"] = json.dumps(content)
    result = await configured_common(tmp_path, common_plugins, overrides={"terminal_failures": ["SCHEMA_CHANGED"]})
    assert result["status"] == "failed" and result["failure"]["code"] == "SCHEMA_CHANGED"
    assert [row["listing_id"] for row in result["listings"]] == ["1", "2"]
    assert result["pages"][1]["receipt"]["projection_performed"] is False
    assert result["receipt"]["cost_usd"] == 0.2 and len(result["receipt"]["evidence"]) == 1
    assert [name for name, _ in common_plugins["calls"]] == ["native"]


@pytest.mark.parametrize("mutation", ["index", "content", "source_url", "too_many_pages", "evidence_bytes"])
async def test_capture_index_bytes_source_and_artifact_binding_are_enforced(tmp_path, common_plugins, mutation):
    if mutation in {"index", "content"}:
        def bad_captures(request):
            captures = list(native_sequence_pages(request))
            captures[0] = replace(captures[0], **({"source_index": 1} if mutation == "index" else {"content": "invented"}))
            return captures
        common_plugins["native_adapter"].sequence_pages = bad_captures
    if mutation == "source_url": common_plugins["packet"]["pages"][1]["url"] = NEXT.replace("camera", "shoes")
    if mutation == "too_many_pages": common_plugins["packet"]["pages"].append(copy.deepcopy(common_plugins["packet"]["pages"][1]))
    if mutation == "evidence_bytes": common_plugins["raw"] = b'{"pages": []}'
    result = await configured_common(tmp_path, common_plugins,
        overrides={"terminal_failures": ["SCHEMA_CHANGED", "CONTENT_MISMATCH", "LIMIT_EXCEEDED"]})
    expected = "CONTENT_MISMATCH" if mutation == "source_url" else "LIMIT_EXCEEDED" if mutation == "too_many_pages" else "SCHEMA_CHANGED"
    assert result["status"] == "failed" and result["failure"]["code"] == expected
    assert [name for name, _ in common_plugins["calls"]] == ["native"]
    assert len(result["receipt"]["evidence"]) == 1


@pytest.mark.parametrize("cost", [None, -1, True, "0.2", float("nan"), 0.4])
async def test_unknown_invalid_or_excess_cost_cannot_claim_satisfied_cap(tmp_path, common_plugins, cost):
    common_plugins["cost"] = cost
    result = await configured_common(tmp_path, common_plugins, overrides={"max_cost_usd": 0.3})
    assert result["receipt"]["status"] == "failed" and result["failure"]["code"] == "BUDGET_EXHAUSTED"
    assert result["receipt"]["cost_budget_satisfied"] is False
    assert [name for name, _ in common_plugins["calls"]] == ["native"]
    assert result["receipt"]["cost_usd"] == (0.4 if cost == 0.4 else None)


async def test_unknown_cost_remains_unknown_without_aggregate_cap(tmp_path, common_plugins):
    common_plugins["cost"] = None
    result = await configured_common(tmp_path, common_plugins)
    assert result["receipt"]["status"] == "observed" and result["receipt"]["cost_usd"] is None
    assert result["receipt"]["cost_budget_satisfied"] is None


async def test_running_catalog_pins_adapter_version_and_next_runtime_sees_change(tmp_path, common_plugins):
    common_plugins["hook"] = lambda: setattr(common_plugins["native_adapter"], "manifest", AdapterManifest("native_capture", "34"))
    result = await configured_common(tmp_path, common_plugins,
        overrides={"terminal_failures": ["SCHEMA_CHANGED"]})
    assert result["receipt"]["status"] == "observed"
    assert result["receipt"]["acquisition_adapter_version"] == "33"
    assert all(page["receipt"]["projection_performed"] is True
               for page in result["pages"])
    async with Runtime(tmp_path / "next",
                       identity_registry=tmp_path / "unused-identities.json") as web:
        assert web.adapters.require_enabled("native_capture").version == "34"


@pytest.mark.parametrize("field,value", [("browser_agent_allowed_actions", ["click"]),
    ("browser_agent_allowed_origins", ["https://other.test"]), ("browser_agent_entry_url", "https://other.test/"),
    ("browser_agent_use_vision", True), ("identity", "owner"), ("allow_paid_fallbacks", True)])
def test_recipes_cannot_grant_browser_agent_identity_or_paid_authority(field, value):
    with pytest.raises(RouteRecipeError): common_recipe(policy_defaults={field: value})


@pytest.mark.parametrize("changes", [{"operation": "extract"}, {"representation": "single_page"},
    {"continuation_adapter_version": None}, {"acquisition_adapter_version": None}])
def test_native_recipe_representation_is_typed_and_versions_complete(changes):
    with pytest.raises(RouteRecipeError): common_recipe(**changes)


@pytest.mark.parametrize("path", ["empty", "skipped", "native_failure"])
async def test_ordinary_pages_keep_false_null_and_zero_override_presence(tmp_path, common_plugins, monkeypatch, path):
    async with Runtime(tmp_path, identity_registry=tmp_path / "unused-identities.json") as web:
        if path != "empty": web.routes.register(common_recipe(policy_defaults={"settle_ms": 80} if path == "skipped" else {}))
        if path == "native_failure":
            bad = json.loads(common_plugins["packet"]["pages"][1]["content"]); bad["leaf"] = "wrong"
            common_plugins["packet"]["pages"][1]["content"] = json.dumps(bad)
        result = await web.paginate(COMMON, "first_leaf", continuation_adapter="continued_leaf",
            policy_overrides={"settle_ms": 0, "public_entry_url": None, "scrapling_google_search": False})
    baseline = [request.policy for name, request in common_plugins["calls"] if name == "http"]
    assert len(baseline) == 2 and all(policy.settle_ms == 0 and policy.public_entry_url is None
                                   and policy.scrapling_google_search is False for policy in baseline)
    assert result["status"] == "page_limit_reached"


def paid_baseline(state, cost):
    plugin = state["provider_registry"]._plugins["http"]
    plugin.manifest = replace(plugin.manifest, paid=True, cost_bounded=True)
    state["baseline_cost"] = cost


async def test_native_exact_cap_still_parses_all_already_acquired_pages(tmp_path, common_plugins):
    result = await configured_common(tmp_path, common_plugins, overrides={"max_cost_usd": 0.2})
    assert len(result["pages"]) == 2 and result["receipt"]["status"] == "observed"
    assert result["receipt"]["cost_usd"] == 0.2 and result["receipt"]["cost_budget_satisfied"] is True
    assert [name for name, _ in common_plugins["calls"]] == ["native"]


@pytest.mark.parametrize("configured_cap", [False, True])
async def test_paid_fallback_only_receives_remaining_aggregate_cap(tmp_path, common_plugins, configured_cap):
    bad = json.loads(common_plugins["packet"]["pages"][1]["content"]); bad["leaf"] = "wrong"
    common_plugins["packet"]["pages"][1]["content"] = json.dumps(bad)
    paid_baseline(common_plugins, 0.1)
    changes = {"policy_defaults": {"max_cost_usd": 0.3}} if configured_cap else {}
    overrides = {"allow_paid_fallbacks": True}
    if not configured_cap: overrides["max_cost_usd"] = 0.3
    result = await configured_common(tmp_path, common_plugins, recipe_changes=changes, overrides=overrides)
    assert result["status"] == "failed" and result["failure"]["code"] == "BUDGET_EXHAUSTED"
    assert [name for name, _ in common_plugins["calls"]] == ["native", "http"]
    assert common_plugins["calls"][1][1].policy.max_cost_usd == 0.1
    assert common_plugins["charged_baseline_calls"] == 1
    assert result["receipt"]["cost_usd"] == result["receipt"]["max_cost_usd"] == 0.3
    assert result["receipt"]["cost_budget_satisfied"] is True
    assert len(result["pages"]) == 1


async def test_paid_ordinary_pages_receive_decreasing_caps_and_stop_before_next_call(tmp_path, common_plugins):
    paid_baseline(common_plugins, 0.1)
    async with Runtime(tmp_path, identity_registry=tmp_path / "unused-identities.json") as web:
        result = await web.paginate(COMMON, "first_leaf", continuation_adapter="continued_leaf",
            policy_overrides={"max_cost_usd": 0.2, "max_pages": 3, "allow_paid_fallbacks": True})
    assert result["failure"]["code"] == "BUDGET_EXHAUSTED"
    assert [request.policy.max_cost_usd for name, request in common_plugins["calls"]] == [0.2, 0.1]
    assert common_plugins["charged_baseline_calls"] == 2
    assert result["receipt"]["cost_usd"] == 0.2 and len(result["pages"]) == 2


async def test_zero_cost_cap_allows_known_free_ordinary_pages(tmp_path, common_plugins):
    async with Runtime(tmp_path, identity_registry=tmp_path / "unused-identities.json") as web:
        result = await web.paginate(COMMON, "first_leaf", continuation_adapter="continued_leaf", policy_overrides={"max_cost_usd": 0})
    assert result["status"] == "page_limit_reached" and len(result["pages"]) == 2
    assert result["receipt"]["cost_usd"] == 0 and result["receipt"]["cost_budget_satisfied"] is True


def test_finite_individual_costs_cannot_overflow_aggregate_receipt():
    from frankensurf.pagination import _total_cost
    with pytest.raises(WebFailure) as exc:
        _total_cost([{"trace_id": "a", "cost_usd": 1e308}, {"trace_id": "b", "cost_usd": 1e308}])
    assert exc.value.code == "SCHEMA_CHANGED"
    assert _total_cost([{"trace_id": "same", "cost_usd": 0.2}, {"trace_id": "same", "cost_usd": 0.2}]) == 0.2
