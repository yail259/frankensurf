import asyncio
from dataclasses import asdict
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading

import pytest

from frankensurf import Runtime, WebPolicy
from frankensurf import browser_use_config as config
from frankensurf.browser_use_provider import BrowserUseProvider, _retain_failure, _group_running, _terminate
from frankensurf.providers import ProviderRequest, DEFAULT_PROVIDERS
from frankensurf.runtime import WebFailure


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), "0"])
def test_cost_policy_rejects_invalid(value):
    with pytest.raises(ValueError):
        WebPolicy(max_cost_usd=value)


@pytest.mark.parametrize("field,value", [
    ("browser_agent_max_steps", True), ("browser_agent_max_actions", 0),
    ("browser_agent_allowed_actions", ("evaluate",)), ("browser_agent_allowed_actions", ("done", "done")),
    ("browser_agent_allowed_origins", ("https://example.com/path",)),
    ("browser_agent_allowed_origins", ("https://name:secret@example.com",)),
    ("browser_agent_llm_timeout_seconds", float("nan")), ("browser_agent_use_vision", 1),
    ("browser_agent_entry_url", "javascript:alert(1)"), ("browser_agent_readiness_poll_ms", 0),
])
def test_agent_policy_rejects_invalid(field, value):
    with pytest.raises(ValueError):
        WebPolicy(**{field: value})


async def test_missing_model_is_typed_unavailable(monkeypatch):
    monkeypatch.setattr(config, "config_path", lambda: Path("/nonexistent/frankensurf-test-browser-agent.json"))
    monkeypatch.delenv("FRANKENSURF_BROWSER_USE_MODEL_FACTORY", raising=False)
    monkeypatch.delenv("FRANKENSURF_BROWSER_USE_BILLING", raising=False)
    policy = WebPolicy(provider="browser_use")
    with pytest.raises(WebFailure) as caught:
        await DEFAULT_PROVIDERS.acquire("browser_use", ProviderRequest("https://example.com/", policy), None)
    assert caught.value.code == "PROVIDER_UNAVAILABLE"


async def test_named_identity_rejected_before_configuration(monkeypatch):
    monkeypatch.setattr(config, "configured", lambda: pytest.fail("configuration must not be checked"))
    with pytest.raises(WebFailure) as caught:
        await BrowserUseProvider().acquire(ProviderRequest("https://example.com/", WebPolicy(identity="owned")), None)
    assert caught.value.code == "IDENTITY_POLICY_DENIED"


def test_private_evidence_respects_authority_bytes_retention(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _retain_failure("public", WebPolicy(retain_public_failure_evidence=False)) == []
    assert _retain_failure("public", WebPolicy(identity="owned")) == []
    assert _retain_failure("public", WebPolicy(max_bytes=1)) == []
    evidence = _retain_failure("public", WebPolicy(max_bytes=6))
    assert len(evidence) == 1 and evidence[0]["bytes"] == 6
    assert Path(evidence[0]["path"]).read_text() == "public"
    assert Path(evidence[0]["path"]).stat().st_mode & 0o777 == 0o600


def test_fragments_remain_part_of_exact_subject():
    assert config.subject_url("https://example.com/#/item/A") != config.subject_url("https://example.com/#/item/B")


async def test_optional_readiness_reserves_final_capture_deadline():
    from types import SimpleNamespace
    from time import monotonic
    from frankensurf import browser_use_worker as worker

    class Runtime:
        async def evaluate(self, *, params, session_id):
            if "JSON.stringify" in params["expression"]:
                value = json.dumps({"url": "https://example.com/item",
                    "content": "<html><p>current unknown state</p></html>"})
            else:
                value = False
            return {"result": {"value": value}}

    session = SimpleNamespace(
        cdp_client=SimpleNamespace(
            send=SimpleNamespace(Runtime=Runtime())),
        session_id="fixture")
    class Browser:
        async def get_or_create_cdp_session(self):
            return session

    state = {"deadline": monotonic() + 0.2, "failure": None}
    policy = asdict(WebPolicy(
        timeout_seconds=2, content_ready_selector="#eventual",
        content_ready_timeout_seconds=1,
        browser_agent_readiness_poll_ms=5))
    readiness = await worker.content_readiness(Browser(), state, policy)
    page = await worker.observe(Browser(), state)
    assert readiness == {"status": "timed_out", "timeout_seconds": 1}
    assert state["failure"] is None
    assert page["url"] == "https://example.com/item"


@pytest.mark.skipif(os.name != "posix", reason="POSIX owned process-group worker")
async def test_cleanup_kills_descendants_after_worker_exits():
    process = await asyncio.create_subprocess_exec(sys.executable, "-c",
              "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)",
              start_new_session=True)
    await process.wait()
    assert _group_running(process.pid)
    await _terminate(process, 1)
    assert not _group_running(process.pid)


@pytest.fixture
def fixture_server(tmp_path):
    (tmp_path / "catalogue").write_text('<html><body><h1>Public catalogue</h1><a id="detail" href="/listing">Exact listing</a><button id="publish">Publish listing</button></body></html>')
    (tmp_path / "listing").write_text('<html><head><title>Fixture listing 123</title></head><body><h1>Fixture listing 123</h1><p>Current public seller asking price is AUD 200. Independent browser source for exact listing 123, with sufficient content for the ordinary runtime.</p><div id="hidden" style="display:none">not ready</div></body></html>')
    (tmp_path / "wrong").write_text('<html><body><h1>Wrong listing 456</h1><p>Never accept a model claim that this unrelated listing is the requested subject.</p></body></html>')
    (tmp_path / "challenge").write_text('<html><body><h1>Verify you are human</h1><p>Just a moment... Cloudflare challenge</p></body></html>')
    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def guess_type(self, path):
            return "text/html"
        def do_GET(self):
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "http://localhost:" + str(self.server.server_port) + "/wrong")
                self.end_headers()
                return
            super().do_GET()
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(tmp_path)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:" + str(server.server_port)
    server.shutdown()
    server.server_close()
    thread.join()


def configure_model(monkeypatch, tmp_path, actions, *, billing="unmetered", delay=0, bound=None):
    source = tmp_path / "model_factory.py"
    source.write_text('''
import asyncio
from frankensurf.browser_use_binding import ModelBinding
from browser_use.llm.views import ChatInvokeCompletion

class FixtureModel:
    model = "fixture-public-read"
    provider = "fixture"
    name = "fixture-public-read"
    model_name = model
    _verified_api_keys = True
    def __init__(self):
        self.index = 0
    async def ainvoke(self, messages, output_format=None, **kwargs):
        await asyncio.sleep(DELAY)
        action = ACTIONS[min(self.index, len(ACTIONS)-1)]
        self.index += 1
        data = {"evaluation_previous_goal":"Continue fixture read", "memory":"Read public fixture only", "next_goal":"Observe requested subject", "action":[action]}
        return ChatInvokeCompletion(completion=output_format.model_validate(data), usage=None)

def make_model():
    return ModelBinding(FixtureModel(), BILLING, next_call_cost_upper_bound=(lambda messages, output_format: BOUND) if BOUND is not None else None)
'''.replace("DELAY", repr(delay)).replace("ACTIONS", repr(actions)).replace("BILLING", repr(billing)).replace("BOUND", repr(bound)))
    monkeypatch.setenv("FRANKENSURF_BROWSER_USE_MODEL_FACTORY", str(source) + ":make_model")
    monkeypatch.setenv("FRANKENSURF_BROWSER_USE_BILLING", billing)
    return source


SDK_AVAILABLE = importlib.util.find_spec("browser_use") is not None and config.installed() and config.browser_path() is not None
sdk = pytest.mark.skipif(not SDK_AVAILABLE, reason="Run in the pinned optional agent environment to exercise the actual SDK")


def sdk_worker():
    from frankensurf import browser_use_worker
    return browser_use_worker


@sdk
async def test_actual_sdk_dispatch_denies_reregistered_click():
    worker = sdk_worker()
    policy = asdict(WebPolicy())
    state = {"deadline": __import__("time").monotonic() + 10, "actions":0,"pages":0,"failure":None}
    tools = worker.make_tools(policy, state, ("https://example.com",))
    # The pinned registry normally prevents re-registration first. Deliberately
    # bypass that schema barrier to exercise the independent dispatch barrier.
    tools.registry.exclude_actions.remove("click")
    tools.set_coordinate_clicking(True)
    Action = tools.registry.create_action_model()
    with pytest.raises(worker.WorkerFailure) as caught:
        await tools.act(Action(click={"index":1}), None)
    assert caught.value.code == "POLICY_DENIED"


@sdk
async def test_actual_sdk_dispatch_denies_overwritten_done():
    from pydantic import BaseModel
    worker = sdk_worker()
    class Output(BaseModel):
        claim: str
    policy = asdict(WebPolicy())
    state = {"deadline": __import__("time").monotonic() + 10, "actions":0,"pages":0,"failure":None}
    tools = worker.make_tools(policy, state, ("https://example.com",))
    tools.use_structured_output_action(Output)
    Action = tools.registry.create_action_model()
    with pytest.raises(worker.WorkerFailure) as caught:
        await tools.act(Action(done={"success":True,"data":{"claim":"forged"}}), None)
    assert caught.value.code == "POLICY_DENIED"


@sdk
async def test_actual_sdk_model_call_count_and_conservative_cost():
    from frankensurf.browser_use_binding import ModelBinding
    worker = sdk_worker()
    calls = []
    class LLM:
        model = "fixture"
        async def ainvoke(self, *args, **kwargs):
            calls.append(True)
            return "observed"
    policy = asdict(WebPolicy(max_cost_usd=0.1, browser_agent_max_model_calls=2))
    state = {"deadline": __import__("time").monotonic() + 10,"failure":None}
    model = worker.BudgetedModel(ModelBinding(LLM(), "paid", next_call_cost_upper_bound=lambda *args:0.06),policy,state)
    assert await model.ainvoke([]) == "observed"
    with pytest.raises(worker.WorkerFailure) as caught:
        await model.ainvoke([])
    assert caught.value.code == "BUDGET_EXHAUSTED" and len(calls) == 1
    policy["max_cost_usd"] = None
    state["failure"] = None
    model = worker.BudgetedModel(ModelBinding(LLM(), "unmetered"),policy,state)
    await model.ainvoke([])
    await model.ainvoke([])
    with pytest.raises(worker.WorkerFailure) as caught:
        await model.ainvoke([])
    assert caught.value.code == "BUDGET_EXHAUSTED" and model.calls == 2


@sdk
async def test_actual_sdk_fallback_observes_real_subject(monkeypatch, tmp_path, fixture_server):
    url = fixture_server + "/listing"
    configure_model(monkeypatch, tmp_path, [{"navigate_public":{"url":fixture_server + "/catalogue"}}, {"follow_link":{"selector":"#detail"}}, {"done":{"success":True}}])
    runtime = Runtime(state_dir=tmp_path / "runtime")
    async def blocked(*args):
        raise WebFailure("BLOCKED", "Fixture initial route blocked")
    monkeypatch.setattr(runtime, "_get_http", blocked)
    async with runtime:
        result = await runtime.read(url, WebPolicy(provider_candidates=("http", "browser_use"), timeout_seconds=45,
                                                  browser_agent_entry_url=fixture_server + "/catalogue", wait_selector="h1", settle_ms=0))
    assert result["receipt"]["status"] == "observed", result["receipt"]
    assert [attempt["provider"] for attempt in result["receipt"]["attempts"]] == ["http", "browser_use"]
    assert result["receipt"]["final_url"] == url
    assert "Fixture listing 123" in result["text"]
    assert result["receipt"]["cost_usd"] is None


@sdk
@pytest.mark.parametrize("retain", [False, True])
async def test_actual_sdk_done_cannot_certify_wrong_subject(monkeypatch, tmp_path, fixture_server, retain):
    configure_model(monkeypatch, tmp_path, [{"navigate_public":{"url":fixture_server + "/wrong"}}, {"done":{"success":True}}])
    with pytest.raises(WebFailure) as caught:
        await BrowserUseProvider().acquire(ProviderRequest(fixture_server + "/listing", WebPolicy(timeout_seconds=40,
                                           retain_public_failure_evidence=retain, settle_ms=0)), None)
    assert caught.value.code == "CONTENT_MISMATCH"
    assert bool(caught.value._public_failure_evidence) is retain


@sdk
async def test_actual_sdk_refuses_button_and_no_retention(monkeypatch, tmp_path, fixture_server):
    configure_model(monkeypatch, tmp_path, [{"navigate_public":{"url":fixture_server + "/catalogue"}}, {"follow_link":{"selector":"#publish"}}, {"done":{"success":True}}])
    with pytest.raises(WebFailure) as caught:
        await BrowserUseProvider().acquire(ProviderRequest(fixture_server + "/listing", WebPolicy(timeout_seconds=40, settle_ms=0)), None)
    assert caught.value.code == "POLICY_DENIED"
    assert caught.value._public_failure_evidence == []


@sdk
async def test_actual_sdk_content_byte_authority(monkeypatch, tmp_path, fixture_server):
    configure_model(monkeypatch, tmp_path, [{"navigate_public":{"url":fixture_server + "/listing"}}, {"done":{"success":True}}])
    with pytest.raises(WebFailure) as caught:
        await BrowserUseProvider().acquire(ProviderRequest(fixture_server + "/listing", WebPolicy(timeout_seconds=40, max_bytes=20, settle_ms=0)), None)
    assert caught.value.code == "LIMIT_EXCEEDED"
    assert caught.value._public_failure_evidence == []


@sdk
async def test_actual_sdk_visible_readiness_timeout_cleanup(monkeypatch, tmp_path, fixture_server):
    configure_model(monkeypatch, tmp_path, [{"navigate_public":{"url":fixture_server + "/listing"}}, {"done":{"success":True}}])
    launched = []
    original = asyncio.create_subprocess_exec
    async def record(*args, **kwargs):
        process = await original(*args, **kwargs)
        launched.append(process)
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", record)
    with pytest.raises(WebFailure) as caught:
        await BrowserUseProvider().acquire(ProviderRequest(fixture_server + "/listing", WebPolicy(timeout_seconds=10,
                 wait_selector="#hidden", wait_state="visible", retain_public_failure_evidence=False, settle_ms=0)), None)
    assert caught.value.code == "TIMEOUT"
    assert caught.value._public_failure_evidence == []
    assert launched and launched[0].returncode is not None
    assert not _group_running(launched[0].pid)


@sdk
async def test_actual_sdk_paid_unknown_cost_cap_blocks_before_calls(monkeypatch, tmp_path, fixture_server):
    configure_model(monkeypatch, tmp_path, [{"navigate_public":{"url":fixture_server + "/listing"}}, {"done":{"success":True}}], billing="paid")
    with pytest.raises(WebFailure) as caught:
        await BrowserUseProvider().acquire(ProviderRequest(fixture_server + "/listing", WebPolicy(timeout_seconds=40,
                              allow_paid_fallbacks=True, max_cost_usd=0.1, settle_ms=0)), None)
    assert caught.value.code == "BUDGET_EXHAUSTED"
    assert getattr(caught.value, "cost_usd", "missing") is None


@sdk
async def test_actual_sdk_challenge_is_not_success(monkeypatch, tmp_path, fixture_server):
    configure_model(monkeypatch, tmp_path, [{"navigate_public":{"url":fixture_server + "/challenge"}}, {"done":{"success":True}}])
    with pytest.raises(WebFailure) as caught:
        await BrowserUseProvider().acquire(ProviderRequest(fixture_server + "/challenge", WebPolicy(timeout_seconds=40, settle_ms=0)),None)
    assert caught.value.code == "CAPTCHA"


async def test_numbered_pagination_is_denied_without_configuration():
    assert BrowserUseProvider().manifest.navigation is False
    assert BrowserUseProvider().manifest.diagnosis is True
    with pytest.raises(WebFailure) as caught:
        await BrowserUseProvider().acquire(ProviderRequest("https://example.com/", WebPolicy(navigation_page=2)), None)
    assert caught.value.code == "POLICY_DENIED"


@sdk
def test_actual_sdk_security_predicate_uses_exact_origin():
    worker = sdk_worker()
    state = {"deadline": __import__("time").monotonic() + 10,"pages":0,"failure":None}
    _, permitted = worker.guarded_browser_class(("https://allowed.example",), state)
    assert permitted("about:blank")
    assert permitted("https://allowed.example/path?x=1")
    from browser_use.browser.watchdogs.security_watchdog import SecurityWatchdog
    for url in ("https://allowed.example.evil/path", "https://allowed.example:444/path",
                "http://allowed.example/", "https://allowed.example@evil.example/", "data:text/html,secret", "blob:https://allowed.example/opaque"):
        state["failure"] = None
        assert not SecurityWatchdog._is_url_allowed(None, url)
        assert state["failure"] == "POLICY_DENIED"
    state["failure"] = None
    state["pages"] = 1
    assert not permitted("about:blank")


@sdk
def test_actual_sdk_profile_can_enforce_fresh_read_permissions(tmp_path):
    from browser_use import Browser
    browser = Browser(user_data_dir=str(tmp_path / "owned-profile"), is_local=True, use_cloud=False,
                      permissions=[], enable_default_extensions=False, accept_downloads=False,
                      auto_download_pdfs=False, captcha_solver=False, args=["--disable-extensions"])
    profile = browser.browser_profile
    assert profile.permissions == [] and not profile.enable_default_extensions
    assert not profile.accept_downloads and not profile.auto_download_pdfs and not profile.captcha_solver
    assert "--disable-extensions" in profile.get_args()


@sdk
async def test_actual_sdk_redirect_never_dispatches_foreign_dom(monkeypatch, tmp_path, fixture_server):
    calls = tmp_path / "model_calls"
    source = configure_model(monkeypatch, tmp_path, [{"navigate_public":{"url":fixture_server + "/redirect"}}, {"done":{"success":True}}])
    source.write_text(source.read_text().replace("await asyncio.sleep(0)",
        "from pathlib import Path\n        with Path(" + repr(str(calls)) + ").open('a') as stream: stream.write('call\\n')\n        assert 'Wrong listing 456' not in str(messages)\n        await asyncio.sleep(0)"))
    with pytest.raises(WebFailure) as caught:
        await BrowserUseProvider().acquire(ProviderRequest(fixture_server + "/redirect", WebPolicy(timeout_seconds=35, settle_ms=0)), None)
    assert caught.value.code == "POLICY_DENIED"
    assert caught.value._public_failure_evidence == []
    assert calls.read_text().splitlines() == ["call"]


@sdk
async def test_worker_refuses_stale_binding_before_loading_factory(monkeypatch, tmp_path, fixture_server):
    worker = sdk_worker()
    configure_model(monkeypatch, tmp_path, [{"done":{"success":True}}])
    monkeypatch.setattr(worker, "load_binding", lambda *args, **kwargs: pytest.fail("stale binding must not execute"))
    packet = await worker.execute({"policy":asdict(WebPolicy()),"url":fixture_server + "/listing",
                                  "binding_fingerprint":"0" * 64,"browser_executable":str(config.browser_path())})
    assert packet["failure"] == "PROVIDER_UNAVAILABLE" and "content" not in packet


@sdk
async def test_worker_refuses_changed_binding_after_model_execution(monkeypatch, tmp_path, fixture_server):
    source = configure_model(monkeypatch, tmp_path, [{"navigate_public":{"url":fixture_server + "/listing"}}, {"done":{"success":True}}])
    source.write_text(source.read_text().replace("self.index += 1", "self.index += 1\n        if self.index == 2:\n            import os\n            os.environ['FRANKENSURF_BROWSER_USE_BINDING_REVISION']='changed-during-execution'"))
    with pytest.raises(WebFailure) as caught:
        await BrowserUseProvider().acquire(ProviderRequest(fixture_server + "/listing", WebPolicy(timeout_seconds=35, settle_ms=0)), None)
    assert caught.value.code == "PROVIDER_UNAVAILABLE"
    assert caught.value._public_failure_evidence == []


@pytest.mark.parametrize("override,expected", [
    ({"unknown":"do not export"},"PROVIDER_DOWN"), ({"url":None},"PROVIDER_DOWN"),
    ({"url":"https://different.example/"},"POLICY_DENIED"),
    ({"content":23},"LIMIT_EXCEEDED"), ({"content_type":"application/json"},"PROVIDER_DOWN"),
    ({"binding_fingerprint":"bad"},"PROVIDER_UNAVAILABLE"),
    ({"status":"failed","failure":"TIMEOUT"},"TIMEOUT"),
    ({"cost_usd":True},"PROVIDER_DOWN"),
])
async def test_worker_packet_rejection_preserves_valid_spend(monkeypatch, tmp_path, override, expected):
    from frankensurf import browser_use_provider as provider
    snapshot = config.BindingSnapshot(tmp_path / "factory.py","factory",b"", "unmetered","v1",Path(sys.executable),tmp_path / "browser","f" * 64)
    monkeypatch.setattr(config, "snapshot", lambda:snapshot)
    packet = {"status":"ok","url":"https://example.com/","content":"<html>Actual source</html>",
              "content_type":"text/html; rendered=1","cost_usd":0.02,"binding_fingerprint":snapshot.fingerprint}
    packet.update(override)
    async def launch(*args, **kwargs): return object()
    async def read(*args, **kwargs): return packet
    async def terminate(*args, **kwargs): pass
    monkeypatch.setattr(asyncio,"create_subprocess_exec",launch)
    monkeypatch.setattr(provider,"_read_packet",read)
    monkeypatch.setattr(provider,"_terminate",terminate)
    with pytest.raises(WebFailure) as caught:
        await BrowserUseProvider().acquire(ProviderRequest("https://example.com/",WebPolicy(retain_public_failure_evidence=False)),None)
    assert caught.value.code == expected
    assert caught.value.cost_usd == (None if override.get("cost_usd") is True else 0.02)
    assert caught.value._public_failure_evidence == []
