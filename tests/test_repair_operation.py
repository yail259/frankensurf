import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import threading
import time

import httpx
import pytest

from frankensurf import cli, mcp_server
from frankensurf import (
    PromotionPolicy,
    RepairPolicy,
    Runtime,
    WebPolicy,
    WORKLOAD_ASSERTIONS_SCHEMA,
)
from frankensurf.adapters import (
    AdapterManifest,
    DEFAULT_ADAPTERS,
)
from frankensurf.plugin_catalog import build_plugin_catalog
from frankensurf.providers import (
    DEFAULT_PROVIDERS,
    ProviderManifest,
)
from frankensurf.search_plugins import DEFAULT_SEARCHES
import frankensurf.repair as repair_module


HTML = """<html><head><title>Changed product</title></head>
<body><h1 class="new-title">Changed product 42</h1>
<p>Public fixture with enough exact subject content for ordinary HTTP acquisition.</p>
</body></html>"""
URL = "https://repair.test/item/42"
ASSERTIONS = {
    "schema": WORKLOAD_ASSERTIONS_SCHEMA,
    "checks": [{"path": "title", "operator": "equals",
                "value": "Changed product 42"}],
}
PROPOSAL = {
    "kind": "adapter_patch",
    "summary": "The product title moved from the retired selector.",
    "bindings": [{"path": "title", "selector": "h1.new-title",
                  "source": "text"}],
}


class BrokenSchemaAdapter:
    manifest = AdapterManifest("repair_fixture", "1")

    def extract(self, request):
        return {
            "text": "",
            "structured": {"title": None},
            "image_urls": [],
        }


class FixtureDiagnosisProvider:
    manifest = ProviderManifest(
        "fixture_diagnosis", "1", rendering=True, diagnosis=True)

    async def acquire(self, request, services):
        raise AssertionError("diagnosis plugin must not acquire ordinary reads")

    async def diagnose(self, request, services):
        assert request.context["failure"]["code"] == "SCHEMA_CHANGED"
        assert request.context["workload_assertions"] == ASSERTIONS
        return {
            "url": request.url,
            "content": HTML,
            "raw": HTML.encode(),
            "content_type": "text/html; rendered=1",
            "http_status": None,
            "headers": {},
            "cost_usd": 0,
            "proposal": PROPOSAL,
        }


def repair_catalog(tmp_path):
    providers = DEFAULT_PROVIDERS.clone()
    providers.register(FixtureDiagnosisProvider())
    adapters = DEFAULT_ADAPTERS.clone()
    adapters.register(BrokenSchemaAdapter())
    return build_plugin_catalog(
        config_path=tmp_path / "missing-plugin-policy.json",
        entry_points=[],
        base_providers=providers,
        base_searches=DEFAULT_SEARCHES,
        base_adapters=adapters,
    )


async def test_public_schema_failure_repair_promote_and_disable(tmp_path):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, text=HTML, headers={"content-type": "text/html"}))
    catalog = repair_catalog(tmp_path)
    policy = WebPolicy(provider="http")

    async with Runtime(
            tmp_path / "state", transport=transport,
            plugin_catalog=catalog) as web:
        failed = await web.extract(
            URL, "repair_fixture", policy,
            workload_assertions=ASSERTIONS)
        assert failed["receipt"]["failure"]["code"] == "SCHEMA_CHANGED"
        recovery = failed["receipt"]["recovery"]
        assert recovery["status"] == "available"
        assert recovery["operation"] == "repair"
        assert Path(recovery["repair_input"]["path"]).is_file()
        trace_id = failed["receipt"]["trace_id"]

        repair = await web.repair(
            trace_id,
            RepairPolicy(
                diagnosis_provider="fixture_diagnosis",
                canary_provider="http",
                run_live_canary=True,
                max_canary_attempts=1,
            ))
        assert repair["status"] == "proposal_ready"
        assert repair["proposal"]["version"] == 1
        assert repair["proposal"]["state"] == "canary_validated"
        assert repair["validation"]["status"] == "passed"
        assert repair["validation"]["live_canary"]["status"] == "passed"
        assert repair["promotion"] == {
            "state": "awaiting_explicit_promotion",
            "automatic": False,
            "ordinary_execution_changed": False,
            "required_action":
                "Explicit operator promotion after benchmark comparison",
        }

        still_failed = await web.extract(
            URL, "repair_fixture", policy,
            workload_assertions=ASSERTIONS)
        assert still_failed["receipt"]["failure"]["code"] == "SCHEMA_CHANGED"
        assert "repair_overlay" not in still_failed["receipt"]

        proposal_id = repair["proposal"]["id"]
        proposal_sha = repair["receipt"]["proposal_artifact"]["sha256"]
        with pytest.raises(ValueError, match="hash"):
            web.promote_repair(proposal_id, "0" * 64)

        validation_path = Path(
            repair["receipt"]["validation_artifact"]["path"])
        validation_bytes = validation_path.read_bytes()
        validation_path.write_bytes(validation_bytes + b"\n")
        with pytest.raises(ValueError, match="Promotion"):
            web.promote_repair(
                proposal_id, proposal_sha, PromotionPolicy())
        validation_path.write_bytes(validation_bytes)
        validation_path.chmod(0o600)

        promoted = web.promote_repair(
            proposal_id, proposal_sha, PromotionPolicy())
        assert promoted["status"] == "promoted"
        assert promoted["automatic"] is False
        assert promoted["overlay"]["proposal_sha256"] == proposal_sha
        assert promoted["rollback"]["overlay_id"] == proposal_id

        named_projection = repair_module.active_overlay_projection(
            web.state_dir, "repair_fixture",
            promoted["overlay"]["target"]["adapter"]["version"],
            promoted["overlay"]["target"]["adapter"]["binding_id"],
            URL, HTML, ASSERTIONS, identity_class="named",
            max_registry_bytes=4 * 1024 * 1024)
        assert named_projection is None

        observed = await web.extract(
            URL, "repair_fixture", policy,
            workload_assertions=ASSERTIONS)
        assert observed["receipt"]["status"] == "observed"
        assert observed["structured"]["title"] == "Changed product 42"
        assert observed["receipt"]["repair_overlay"]["id"] == proposal_id
        assert observed["receipt"]["workload_assertions"]["status"] == "passed"

        disabled = web.disable_repair(
            proposal_id, "Fixture rollback proof")
        assert disabled["status"] == "disabled"
        registry = json.loads(
            (tmp_path / "state" / "repairs" / "active.json").read_text())
        assert registry["overlays"][proposal_id]["status"] == "disabled"
        assert registry["history"][-1]["event"] == "disable"
        assert registry["history"][-1]["rollback_of_revision"] == 1

        failed_again = await web.extract(
            URL, "repair_fixture", policy,
            workload_assertions=ASSERTIONS)
        assert failed_again["receipt"]["failure"]["code"] == "SCHEMA_CHANGED"


async def test_repair_rejects_missing_assertions_before_diagnosis(tmp_path):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            500, text=HTML, headers={"content-type": "text/html"}))
    catalog = repair_catalog(tmp_path)
    async with Runtime(
            tmp_path / "state", transport=transport,
            plugin_catalog=catalog) as web:
        failed = await web.extract(
            URL, "repair_fixture", WebPolicy(provider="http"))
        result = await web.repair(
            failed["receipt"]["trace_id"],
            RepairPolicy(diagnosis_provider="fixture_diagnosis"))
    assert result["receipt"]["failure"]["code"] == "REPAIR_ASSERTIONS_REQUIRED"



def browser_repair_catalog(tmp_path):
    adapters = DEFAULT_ADAPTERS.clone()
    adapters.register(BrokenSchemaAdapter())
    return build_plugin_catalog(
        config_path=tmp_path / "missing-plugin-policy.json",
        entry_points=[],
        base_providers=DEFAULT_PROVIDERS,
        base_searches=DEFAULT_SEARCHES,
        base_adapters=adapters,
    )


@pytest.fixture
def repair_http_server(tmp_path):
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    import threading

    (tmp_path / "item").mkdir()
    (tmp_path / "item" / "42").write_text(HTML)
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def guess_type(self, path):
            return "text/html"

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        partial(Handler, directory=str(tmp_path)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:" + str(server.server_port) + "/item/42"
    server.shutdown()
    server.server_close()
    thread.join()


def configure_repair_model(monkeypatch, tmp_path, url):
    source = tmp_path / "repair_model_factory.py"
    proposal_json = json.dumps(PROPOSAL)
    actions = [
        {"navigate_public": {"url": url}},
        {"propose_repair": {"proposal_json": proposal_json}},
    ]
    source.write_text("""
import asyncio
from frankensurf.browser_use_binding import ModelBinding
from browser_use.llm.views import ChatInvokeCompletion

ACTIONS = ACTION_ROWS

class FixtureRepairModel:
    model = "fixture-repair"
    provider = "fixture"
    name = "fixture-repair"
    model_name = model
    _verified_api_keys = True

    def __init__(self):
        self.index = 0

    async def ainvoke(self, messages, output_format=None, **kwargs):
        action = ACTIONS[min(self.index, len(ACTIONS) - 1)]
        self.index += 1
        value = {
            "evaluation_previous_goal": "Continue bounded diagnosis",
            "memory": "Public fixture only",
            "next_goal": "Submit deterministic repair proposal",
            "action": [action],
        }
        return ChatInvokeCompletion(
            completion=output_format.model_validate(value), usage=None)

def make_model():
    return ModelBinding(FixtureRepairModel(), "unmetered")
""".replace("ACTION_ROWS", repr(actions)))
    monkeypatch.setenv(
        "FRANKENSURF_BROWSER_USE_MODEL_FACTORY",
        str(source) + ":make_model")
    monkeypatch.setenv(
        "FRANKENSURF_BROWSER_USE_BILLING", "unmetered")
    monkeypatch.setenv(
        "FRANKENSURF_BROWSER_USE_BINDING_REVISION",
        "repair-fixture-v1")


async def test_browser_use_agent_is_real_diagnosis_edge(
        monkeypatch, tmp_path, repair_http_server):
    from frankensurf import browser_use_config

    if (not browser_use_config.installed()
            or browser_use_config.browser_path() is None):
        pytest.skip("Pinned isolated Browser Use worker is unavailable")
    configure_repair_model(
        monkeypatch, tmp_path, repair_http_server)
    catalog = browser_repair_catalog(tmp_path)
    policy = WebPolicy(provider="http", settle_ms=0)
    async with Runtime(
            tmp_path / "browser-state",
            plugin_catalog=catalog) as web:
        failed = await web.extract(
            repair_http_server, "repair_fixture", policy,
            workload_assertions=ASSERTIONS)
        result = await web.repair(
            failed["receipt"]["trace_id"],
            RepairPolicy(
                diagnosis_provider="browser_use",
                canary_provider="http",
                run_live_canary=True,
                timeout_seconds=40,
            ))
    assert result["status"] == "proposal_ready"
    assert result["receipt"]["provider"] == "browser_use"
    assert result["proposal"]["change"] == PROPOSAL
    assert result["validation"]["live_canary"]["status"] == "passed"


async def test_cli_and_mcp_expose_explicit_repair_boundaries(
        monkeypatch, tmp_path):
    calls = []

    class FakeRuntime:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def repair(self, trace_id, policy):
            calls.append(("repair", trace_id, policy))
            return {"status": "proposal_ready"}

        def promote_repair(self, proposal_id, proposal_hash):
            calls.append(("promote", proposal_id, proposal_hash))
            return {"status": "promoted"}

        def disable_repair(self, overlay_id, reason):
            calls.append(("disable", overlay_id, reason))
            return {"status": "disabled"}

    monkeypatch.setattr(cli, "Runtime", FakeRuntime)
    monkeypatch.setattr(mcp_server, "runtime", lambda: FakeRuntime())
    trace_id = "1" * 32
    proposal_id = "2" * 64
    proposal_hash = "3" * 64

    repair_result = await cli.run(cli.parse_args([
        "repair", trace_id, "--state", str(tmp_path / "state")]))
    promote_result = await cli.run(cli.parse_args([
        "repair-promote", proposal_id, "--proposal-sha256", proposal_hash,
        "--state", str(tmp_path / "state")]))
    disable_result = await cli.run(cli.parse_args([
        "repair-disable", proposal_id, "--disable-reason", "operator rollback",
        "--state", str(tmp_path / "state")]))
    mcp_result = await mcp_server.repair(
        trace_id, {"allowed_route_providers": ["http"]})

    assert repair_result["status"] == "proposal_ready"
    assert promote_result["status"] == "promoted"
    assert disable_result["status"] == "disabled"
    assert mcp_result["status"] == "proposal_ready"
    assert calls[0][:2] == ("repair", trace_id)
    assert calls[1] == ("promote", proposal_id, proposal_hash)
    assert calls[2] == ("disable", proposal_id, "operator rollback")
    assert calls[3][2].allowed_route_providers == ("http",)


@pytest.mark.parametrize("mutation", ["content", "permissions"])
async def test_repair_input_must_match_private_source_trace(
        tmp_path, monkeypatch, mutation):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, text=HTML, headers={"content-type": "text/html"}))
    catalog = repair_catalog(tmp_path)
    async with Runtime(
            tmp_path / "state", transport=transport,
            plugin_catalog=catalog) as web:
        failed = await web.extract(
            URL, "repair_fixture", WebPolicy(provider="http"),
            workload_assertions=ASSERTIONS)
        input_path = Path(
            failed["receipt"]["recovery"]["repair_input"]["path"])
        if mutation == "content":
            input_path.write_bytes(input_path.read_bytes() + b" ")
        else:
            input_path.chmod(0o640)

        async def unexpected_diagnosis(*args, **kwargs):
            raise AssertionError("unbound repair input reached diagnosis")

        monkeypatch.setattr(
            FixtureDiagnosisProvider, "diagnose", unexpected_diagnosis)
        result = await web.repair(
            failed["receipt"]["trace_id"],
            RepairPolicy(diagnosis_provider="fixture_diagnosis"))

    assert result["receipt"]["failure"]["code"] == "REPAIR_INPUT_INVALID"


async def test_live_canary_cost_cap_is_cumulative(tmp_path):
    class MeteredCanary:
        manifest = ProviderManifest(
            "metered_canary", "1", paid=True, cost_bounded=True)
        calls = 0

        async def acquire(self, request, services):
            type(self).calls += 1
            content = (
                "<html><h1 class='wrong'>wrong</h1></html>"
                if type(self).calls == 1 else HTML)
            return {
                "url": request.url,
                "content": content,
                "raw": content.encode(),
                "content_type": "text/html; rendered=1",
                "http_status": 200,
                "headers": {},
                "cost_usd": 0.6,
            }

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, text=HTML, headers={"content-type": "text/html"}))
    providers = DEFAULT_PROVIDERS.clone()
    providers.register(FixtureDiagnosisProvider())
    providers.register(MeteredCanary())
    adapters = DEFAULT_ADAPTERS.clone()
    adapters.register(BrokenSchemaAdapter())
    catalog = build_plugin_catalog(
        config_path=tmp_path / "missing-plugin-policy.json",
        entry_points=[], base_providers=providers,
        base_searches=DEFAULT_SEARCHES, base_adapters=adapters)

    async with Runtime(
            tmp_path / "state", transport=transport,
            plugin_catalog=catalog) as web:
        failed = await web.extract(
            URL, "repair_fixture", WebPolicy(provider="http"),
            workload_assertions=ASSERTIONS)
        result = await web.repair(
            failed["receipt"]["trace_id"],
            RepairPolicy(
                diagnosis_provider="fixture_diagnosis",
                canary_provider="metered_canary",
                run_live_canary=True,
                max_canary_attempts=2,
                allow_paid_fallbacks=True,
                max_cost_usd=0.9))

    attempts = result["validation"]["live_canary"]["attempts"]
    assert [attempt["status"] for attempt in attempts] == ["failed", "failed"]
    assert attempts[1]["failure"]["code"] == "BUDGET_EXHAUSTED"
    assert result["proposal"]["state"] == "canary_failed"
    assert result["receipt"]["cost_usd"] == pytest.approx(1.2)


def test_overlay_registry_transactions_do_not_lose_concurrent_disables(
        tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    overlay_ids = ("1" * 64, "2" * 64)
    repair_module._atomic_overlay_registry(
        state_dir, {
            "schema": repair_module.ACTIVE_OVERLAYS_SCHEMA,
            "revision": 0,
            "overlays": {
                identifier: {"id": identifier, "status": "active"}
                for identifier in overlay_ids},
            "history": [],
        }, 4 * 1024 * 1024)

    original_load = repair_module._load_overlay_registry
    counter_lock = threading.Lock()
    active_loads = 0
    maximum_active_loads = 0

    def slow_load(*args, **kwargs):
        nonlocal active_loads, maximum_active_loads
        with counter_lock:
            active_loads += 1
            maximum_active_loads = max(
                maximum_active_loads, active_loads)
        try:
            time.sleep(0.05)
            return original_load(*args, **kwargs)
        finally:
            with counter_lock:
                active_loads -= 1

    monkeypatch.setattr(
        repair_module, "_load_overlay_registry", slow_load)
    runtime = SimpleNamespace(state_dir=state_dir)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda identifier: repair_module.disable_repair(
                runtime, identifier, "concurrent rollback"),
            overlay_ids))

    registry = original_load(state_dir)
    assert maximum_active_loads == 1
    assert registry["revision"] == 2
    assert all(registry["overlays"][identifier]["status"] == "disabled"
               for identifier in overlay_ids)
    assert {result["overlay_id"] for result in results} == set(overlay_ids)


@pytest.mark.parametrize(
    "components,symlink_components",
    [
        (("repairs", "inputs", "artifact.json"), ("repairs",)),
        (("repairs", "inputs", "artifact.json"),
         ("repairs", "inputs")),
        (("traces", "artifact.json"), ("traces",)),
        (("evidence", "artifact.html"), ("evidence",)),
    ],
)
def test_private_artifact_io_rejects_intermediate_symlinks(
        tmp_path, components, symlink_components):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    outside = tmp_path / (
        "outside-" + "-".join(symlink_components))
    outside.mkdir()
    link = state_dir.joinpath(*symlink_components)
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)

    redirected = outside.joinpath(
        *components[len(symlink_components):])
    redirected.parent.mkdir(parents=True, exist_ok=True)
    redirected.write_bytes(b"outside-private-data")
    redirected.chmod(0o600)

    with pytest.raises(OSError):
        repair_module._read_private_bytes(
            state_dir, components, 1024)
    with pytest.raises(OSError):
        repair_module._write_private_bytes(
            state_dir, components, b"redirected-write")

    assert redirected.read_bytes() == b"outside-private-data"


@pytest.mark.parametrize("directory_name", ["repairs", "traces"])
async def test_repair_rejects_redirected_state_subtrees(
        tmp_path, directory_name):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, text=HTML, headers={"content-type": "text/html"}))
    catalog = repair_catalog(tmp_path)
    state_dir = tmp_path / "state"
    async with Runtime(
            state_dir, transport=transport,
            plugin_catalog=catalog) as web:
        failed = await web.extract(
            URL, "repair_fixture", WebPolicy(provider="http"),
            workload_assertions=ASSERTIONS)
        redirected = tmp_path / ("redirected-" + directory_name)
        (state_dir / directory_name).rename(redirected)
        (state_dir / directory_name).symlink_to(
            redirected, target_is_directory=True)

        result = await web.repair(
            failed["receipt"]["trace_id"],
            RepairPolicy(diagnosis_provider="fixture_diagnosis"))

    assert result["receipt"]["failure"]["code"] == "REPAIR_INPUT_INVALID"
