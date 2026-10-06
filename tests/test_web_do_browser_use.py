"""Live local acceptance for the isolated Browser Use ``web.do`` worker.

The server, browser profile, identity and write target are synthetic and
loopback-only.  No external site or account is contacted.
"""
import asyncio
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import monotonic
from types import SimpleNamespace

import pytest
from playwright.async_api import async_playwright

from frankensurf import BrowserAction, Runtime, WebIntent, WebPolicy
from frankensurf import browser_use_config
from frankensurf.browser_use_action_worker import WorkerFailure, _target_guard
from frankensurf.identity import IdentityRegistry
from frankensurf.web_do import (LOCAL_REVERSIBLE_DRAFT_CONTRACT,
    RAW_BROWSER_CONTROL_CONTRACT, RAW_CONTROL_REQUIRED_ACTION_CLASSES)


class BrowserRecords(list):
    pass


async def test_target_guard_rolls_back_root_autoattach_on_setup_failure():
    original = lambda _event, _session: None

    class Registry:
        def __init__(self):
            self._handlers = {"Target.attachedToTarget": original}

        def register(self, method, callback):
            self._handlers[method] = callback

        def unregister(self, method):
            self._handlers.pop(method, None)

    registry = Registry()

    class TargetCommands:
        def __init__(self):
            self.calls = []

        async def setAutoAttach(self, params, session_id=None):
            self.calls.append((params.copy(), session_id))
            if len(self.calls) == 2:
                raise RuntimeError("owned session rejected auto-attach")
            return {}

    commands = TargetCommands()
    register = SimpleNamespace(Target=SimpleNamespace(
        attachedToTarget=lambda callback: registry.register(
            "Target.attachedToTarget", callback)))
    client = SimpleNamespace(_event_registry=registry, register=register,
        send=SimpleNamespace(Target=commands))
    browser = SimpleNamespace(cdp_client=client)

    with pytest.raises(WorkerFailure) as failure:
        await _target_guard(browser, "owned-target", "owned-session",
            {"deadline": monotonic() + 2}, [], set())

    assert failure.value.code == "PROVIDER_DOWN"
    assert commands.calls == [
        ({"autoAttach": True, "waitForDebuggerOnStart": True,
          "flatten": True}, None),
        ({"autoAttach": True, "waitForDebuggerOnStart": True,
          "flatten": True}, "owned-session"),
        ({"autoAttach": True, "waitForDebuggerOnStart": False,
          "flatten": True}, "owned-session"),
        ({"autoAttach": True, "waitForDebuggerOnStart": False,
          "flatten": True}, None),
    ]
    assert registry._handlers["Target.attachedToTarget"] is original


@pytest.fixture
async def reversible_draft_browser(tmp_path):
    saves = BrowserRecords()
    saves.foreign_hits = []
    saves.spoof_values = []
    saves.raw_drift_gets = 0
    saves.draft_drift_gets = 0

    class ForeignHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def record(self, method):
            size = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(size).decode(errors="replace") if size else ""
            saves.foreign_hits.append(
                {"method": method, "path": self.path, "body": body})
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            self.record("GET")

        def do_POST(self):
            self.record("POST")

    foreign_server = ThreadingHTTPServer(("127.0.0.1", 0), ForeignHandler)
    foreign_thread = threading.Thread(
        target=foreign_server.serve_forever, daemon=True)
    foreign_thread.start()
    foreign_base = f"http://127.0.0.1:{foreign_server.server_port}"
    foreign_ws = f"ws://127.0.0.1:{foreign_server.server_port}"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_html(self, status, body, *, cookie=None):
            encoded = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(encoded)

        def send_script(self, body):
            encoded = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def authenticated(self):
            return "fixture_session=synthetic" in self.headers.get("Cookie", "")

        def do_GET(self):
            if self.path == "/login":
                self.send_html(200, "<main>Fixture login complete</main>",
                    cookie="fixture_session=synthetic; Path=/; HttpOnly; SameSite=Strict")
                return
            if self.path == "/account":
                self.send_html(200 if self.authenticated() else 401,
                    ("<main id=owner>Authenticated fixture owner</main>"
                     if self.authenticated() else "<main>Sign in</main>"))
                return
            if self.path == "/drafts/new" and self.authenticated():
                self.send_html(200, """<!doctype html><html><body>
                  <form id="draft" data-frankensurf-contract="reversible-draft-v1">
                    <label>Title <input id="title" name="title"></label>
                    <label>Description <textarea id="description" name="description"></textarea></label>
                    <button id="save" type="submit"
                      data-frankensurf-action="save-reversible-draft">Save draft</button>
                  </form>
                  <output id="status">Not saved</output>
                  <script>
                    document.querySelector('#draft').addEventListener('submit', async event => {
                      event.preventDefault();
                      const response = await fetch('/drafts/save', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                          title: document.querySelector('#title').value,
                          description: document.querySelector('#description').value
                        })
                      });
                      document.querySelector('#status').textContent =
                        response.ok ? 'Draft saved' : 'Draft failed';
                    });
                  </script>
                </body></html>""")
                return
            if self.path == "/drafts/drift" and self.authenticated():
                saves.draft_drift_gets += 1
                self.send_html(200, """<!doctype html><html><body>
                  <form data-frankensurf-contract="reversible-draft-v1">
                    <input id="title">
                  </form>
                </body></html>""")
                return
            if self.path in {"/raw/new", "/raw/uncertain"} and self.authenticated():
                self.send_html(200, """<!doctype html><html><body>
                  <form id="raw">
                    <label>Title <input id="raw-title" name="title"></label>
                    <button id="raw-save" type="submit">Save</button>
                  </form>
                  <output id="raw-status">Not saved</output>
                  <script>
                    document.querySelector('#raw').addEventListener('submit', async event => {
                      event.preventDefault();
                      const response = await fetch('/raw/save', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                          title: document.querySelector('#raw-title').value
                        })
                      });
                      document.querySelector('#raw-status').textContent =
                        response.ok ? 'Raw saved' : 'Raw failed';
                      const complete = document.createElement('output');
                      complete.id = 'raw-complete';
                      complete.textContent = response.ok ? 'Raw saved' : 'Raw failed';
                      document.body.append(complete);
                    });
                  </script>
                </body></html>""")
                return
            if self.path == "/raw/drift" and self.authenticated():
                saves.raw_drift_gets += 1
                self.send_html(200, """<!doctype html><html><body>
                  <form><input id="raw-title"></form>
                  <output id="raw-status">Not saved</output>
                </body></html>""")
                return
            if self.path == "/raw/step-one" and self.authenticated():
                self.send_html(200, """<!doctype html><html><body>
                  <a id="next" href="/raw/step-two">Continue</a>
                </body></html>""")
                return
            if self.path == "/raw/step-two" and self.authenticated():
                self.send_html(200, """<!doctype html><html><body>
                  <form id="step-form">
                    <input id="step-title">
                    <button id="step-save" type="submit">Save</button>
                  </form>
                  <script>
                    document.querySelector('#step-form').addEventListener(
                      'submit', async event => {
                        event.preventDefault();
                        const response = await fetch('/raw/save', {
                          method:'POST',
                          headers:{'Content-Type':'application/json'},
                          body:JSON.stringify({title:
                            document.querySelector('#step-title').value})});
                        const output = document.createElement('output');
                        output.id = 'step-complete';
                        output.textContent = response.ok ? 'Saved' : 'Failed';
                        document.body.append(output);
                      });
                  </script>
                </body></html>""")
                return
            if self.path == "/raw/worker" and self.authenticated():
                self.send_html(200, """<!doctype html><html><body>
                  <input id="worker-secret">
                  <button id="worker-start" type="button">Start worker</button>
                  <script>
                    document.querySelector('#worker-start').addEventListener(
                      'click', () => {
                        const worker = new Worker('/raw/worker-script');
                        worker.postMessage(
                          document.querySelector('#worker-secret').value);
                      });
                  </script>
                </body></html>""")
                return
            if self.path == "/raw/worker-script":
                self.send_script("""
                  self.onmessage = event => {
                    new WebSocket('%s/worker-ws?secret=' +
                      encodeURIComponent(event.data));
                  };
                """ % foreign_ws)
                return
            if self.path == "/raw/popup" and self.authenticated():
                self.send_html(200, """<!doctype html><html><body>
                  <button id="popup-start" type="button">Open target</button>
                  <script>
                    document.querySelector('#popup-start').addEventListener(
                      'click', () => {
                        const link = document.createElement('a');
                        link.href = '%s/popup-get';
                        link.target = '_blank';
                        document.body.append(link);
                        link.click();
                      });
                  </script>
                </body></html>""" % foreign_base)
                return
            if self.path == "/raw/external-protocol" and self.authenticated():
                self.send_html(200, """<!doctype html><html><body>
                  <button id="external-start" type="button">Launch handler</button>
                  <script>
                    document.querySelector('#external-start').addEventListener(
                      'click', () => {
                        location.href =
                          'mailto:frankensurf-test@example.invalid?body=escape';
                      });
                  </script>
                </body></html>""")
                return
            if self.path == "/raw/redirect-external" and self.authenticated():
                self.send_response(302)
                self.send_header("Location", foreign_base + "/redirect-escape")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path == "/raw/spoof" and self.authenticated():
                self.send_html(200, """<!doctype html><html><body>
                  <input id="safe" type="text">
                  <input id="victim" type="password">
                  <script>
                    const nativeQueryAll = document.querySelectorAll.bind(document);
                    document.querySelectorAll = selector => {
                      if (selector === '#victim') {
                        const safe = nativeQueryAll('#safe')[0];
                        return {0:safe,length:1,
                          [Symbol.iterator]:function*(){yield safe;}};
                      }
                      return nativeQueryAll(selector);
                    };
                    Object.defineProperty(document, 'readyState', {
                      get:() => 'loading'});
                    Object.defineProperty(document.documentElement, 'outerHTML', {
                      get:() => '<html>forged-evidence</html>'});
                    nativeQueryAll('#victim')[0].addEventListener(
                      'input', event => fetch('/raw/spoof-capture', {
                        method:'POST',body:event.target.value}));
                  </script>
                </body></html>""")
                return
            self.send_html(401 if not self.authenticated() else 404,
                           "<main>Unavailable</main>")

        def do_POST(self):
            if self.path == "/raw/spoof-capture" and self.authenticated():
                size = int(self.headers.get("Content-Length", "0"))
                saves.spoof_values.append(
                    self.rfile.read(size).decode(errors="replace"))
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if (self.path not in {"/drafts/save", "/raw/save"}
                    or not self.authenticated()):
                self.send_html(401, "<main>Denied</main>")
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                value = json.loads(self.rfile.read(size))
            except (ValueError, TypeError, json.JSONDecodeError):
                self.send_html(400, "<main>Invalid</main>")
                return
            saves.append(value)
            encoded = b'{"saved":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        debug_port = probe.getsockname()[1]
    profile = tmp_path / "profile"
    registry_path = tmp_path / "authority" / "identities.json"
    registry = IdentityRegistry(registry_path)
    registry.enroll_executor("fixture", endpoint=(
        f"http://127.0.0.1:{debug_port}"), user_data_dir=str(profile))
    registry.enroll_identity("owner", executor_id="fixture",
        domains=["127.0.0.1"],
        allowed_actions=["READ_AUTHENTICATED", "WRITE_REVERSIBLE"],
        auth_check={"url": base + "/account",
                    "authenticated_selector": "#owner"})
    registry.enroll_identity("raw-owner", executor_id="fixture",
        domains=["127.0.0.1"],
        allowed_actions=["READ_AUTHENTICATED",
                         *RAW_CONTROL_REQUIRED_ACTION_CLASSES],
        auth_check={"url": base + "/account",
                    "authenticated_selector": "#owner"})
    try:
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                str(profile), headless=True, chromium_sandbox=True,
                args=["--enable-automation",
                      "--remote-debugging-address=127.0.0.1",
                      f"--remote-debugging-port={debug_port}"],
                viewport={"width": 1000, "height": 800})
            original = context.pages[0]
            await original.goto(base + "/login")
            yield base, context, original, registry_path, saves
            await context.close()
    finally:
        server.shutdown()
        server.server_close()
        foreign_server.shutdown()
        foreign_server.server_close()


@pytest.mark.skipif(not browser_use_config.installed(),
                    reason="pinned Browser Use SDK is not installed")
async def test_core_do_runs_isolated_browser_use_worker_once(
        reversible_draft_browser, tmp_path):
    base, context, original, registry_path, saves = reversible_draft_browser
    secret = "Synthetic bicycle title"
    intent = WebIntent(base + "/drafts/new",
        LOCAL_REVERSIBLE_DRAFT_CONTRACT, "WRITE_REVERSIBLE",
        "fixture-draft-1", "Save the synthetic listing as a reversible draft", (
            BrowserAction("fill", "#title", secret),
            BrowserAction("fill", "#description", "Local fixture only"),
            BrowserAction("click", "#save"),
            BrowserAction("wait_for", "#status", state="visible"),
            BrowserAction("assert_text", "#status", "Draft saved"),
            BrowserAction("assert_value", "#title", secret),
        ))
    policy = WebPolicy(identity="owner",
        action_classes=("WRITE_REVERSIBLE",),
        provider="browser_use_local_cdp_do",
        browser_do_allowed_origins=(base,), timeout_seconds=20,
        browser_do_action_timeout_seconds=5, browser_do_settle_ms=25)
    state = tmp_path / "state"
    async with Runtime(state, identity_registry=registry_path) as web:
        denied = await web.do(intent, WebPolicy(identity="owner",
            provider="browser_use_local_cdp_do",
            browser_do_allowed_origins=(base,)))
        first = await web.do(intent, policy)
        reused = await web.do(intent, policy)

    assert denied["receipt"]["failure"]["code"] == "POLICY_DENIED"
    assert first["receipt"]["status"] == "completed"
    assert first["receipt"]["action_contract"] == (
        LOCAL_REVERSIBLE_DRAFT_CONTRACT)
    assert first["receipt"]["provider"] == "browser_use_local_cdp_do"
    assert first["receipt"]["provider_version"] == (
        browser_use_config.SDK_VERSION)
    assert first["outcome"] == {"state": "completed",
        "certainty": "dom_assertions_passed", "reversible": True,
        "reconciliation_required": False}
    assert len(first["receipt"]["evidence"]) == 4
    assert saves == [{"title": secret, "description": "Local fixture only"}]
    assert reused["receipt"]["idempotent_reuse"] is True
    assert context.pages == [original] and not original.is_closed()
    assert secret not in str(first)
    assert secret not in (state / "actions" / "journal.jsonl").read_text()


@pytest.mark.skipif(not browser_use_config.installed(),
                    reason="pinned Browser Use SDK is not installed")
async def test_fixture_schema_failure_remains_retryable_before_effect(
        reversible_draft_browser, tmp_path):
    base, _context, _original, registry_path, saves = reversible_draft_browser
    intent = WebIntent(base + "/drafts/drift",
        LOCAL_REVERSIBLE_DRAFT_CONTRACT, "WRITE_REVERSIBLE",
        "fixture-drift", "Detect fixture control drift", (
            BrowserAction("fill", "#title", "Must not be entered"),
            BrowserAction("click", "#missing-save"),
        ))
    policy = WebPolicy(identity="owner",
        action_classes=("WRITE_REVERSIBLE",),
        provider="browser_use_local_cdp_do",
        browser_do_allowed_origins=(base,), timeout_seconds=20,
        browser_do_action_timeout_seconds=5, browser_do_settle_ms=25)
    state = tmp_path / "fixture-drift-state"
    async with Runtime(state, identity_registry=registry_path) as web:
        first = await web.do(intent, policy)
        retried = await web.do(intent, policy)

    for result in (first, retried):
        assert result["receipt"]["failure"]["code"] == "SCHEMA_CHANGED"
        assert result["outcome"] == {"state": "failed", "certainty": "certain",
            "reconciliation_required": False}
    assert saves.draft_drift_gets == 2
    assert saves == []
    assert (state / "actions" / "journal.jsonl").read_text().count(
        '"state":"failed_before_effect"') == 2


def raw_control_policy(base, *tools):
    return WebPolicy(identity="raw-owner",
        action_classes=RAW_CONTROL_REQUIRED_ACTION_CLASSES,
        provider="browser_use_local_cdp_do",
        browser_do_allowed_contracts=(RAW_BROWSER_CONTROL_CONTRACT,),
        browser_do_allowed_tools=tools,
        browser_do_allowed_origins=(base,), timeout_seconds=20,
        browser_do_action_timeout_seconds=5, browser_do_settle_ms=25)


@pytest.mark.skipif(not browser_use_config.installed(),
                    reason="pinned Browser Use SDK is not installed")
async def test_raw_control_posts_once_and_reuses_completed_outcome(
        reversible_draft_browser, tmp_path):
    base, context, original, registry_path, saves = reversible_draft_browser
    secret = "Synthetic raw-control title"
    intent = WebIntent(base + "/raw/new", RAW_BROWSER_CONTROL_CONTRACT,
        "WRITE_EXTERNAL", "raw-control-once", "Exercise raw local controls", (
            BrowserAction("fill", "#raw-title", secret),
            BrowserAction("click", "#raw-save"),
            BrowserAction("wait_for", "#raw-complete", state="visible"),
            BrowserAction("assert_text", "#raw-complete", "Raw saved"),
            BrowserAction("assert_value", "#raw-title", secret),
        ))
    state = tmp_path / "raw-state"
    async with Runtime(state, identity_registry=registry_path) as web:
        policy = raw_control_policy(
            base, "fill", "click", "wait_for", "assert_text",
            "assert_value")
        first = await web.do(intent, policy)
        reused = await web.do(intent, policy)

    assert first["receipt"]["status"] == "completed"
    assert first["receipt"]["action_contract"] == RAW_BROWSER_CONTROL_CONTRACT
    assert first["receipt"]["plan"]["required_action_classes"] == list(
        RAW_CONTROL_REQUIRED_ACTION_CLASSES)
    assert saves == [{"title": secret}]
    assert reused["receipt"]["idempotent_reuse"] is True
    assert context.pages == [original] and not original.is_closed()
    assert secret not in str(first)
    assert secret not in (state / "actions" / "journal.jsonl").read_text()


@pytest.mark.skipif(not browser_use_config.installed(),
                    reason="pinned Browser Use SDK is not installed")
async def test_raw_control_refreshes_bindings_after_same_origin_navigation(
        reversible_draft_browser, tmp_path):
    base, _context, _original, registry_path, saves = reversible_draft_browser
    secret = "Synthetic two-page raw title"
    intent = WebIntent(base + "/raw/step-one", RAW_BROWSER_CONTROL_CONTRACT,
        "WRITE_EXTERNAL", "raw-two-page",
        "Navigate and save through a same-origin second page", (
            BrowserAction("click", "#next"),
            BrowserAction("wait_for", "#step-title", state="attached"),
            BrowserAction("fill", "#step-title", secret),
            BrowserAction("click", "#step-save"),
            BrowserAction("wait_for", "#step-complete", state="visible"),
            BrowserAction("assert_text", "#step-complete", "Saved"),
            BrowserAction("assert_value", "#step-title", secret),
        ))
    state = tmp_path / "raw-two-page-state"
    async with Runtime(state, identity_registry=registry_path) as web:
        result = await web.do(intent, raw_control_policy(
            base, "fill", "click", "wait_for", "assert_text",
            "assert_value"))

    assert result["receipt"]["status"] == "completed"
    assert saves == [{"title": secret}]
    assert secret not in str(result)
    assert secret not in (state / "actions" / "journal.jsonl").read_text()


@pytest.mark.skipif(not browser_use_config.installed(),
                    reason="pinned Browser Use SDK is not installed")
async def test_raw_control_fences_control_drift_after_navigation(
        reversible_draft_browser, tmp_path):
    base, _context, _original, registry_path, saves = reversible_draft_browser
    intent = WebIntent(base + "/raw/drift", RAW_BROWSER_CONTROL_CONTRACT,
        "WRITE_EXTERNAL", "raw-control-drift", "Detect control schema drift", (
            BrowserAction("fill", "#raw-title", "Bounded drift value"),
            BrowserAction("click", "#missing-save"),
        ))
    state = tmp_path / "drift-state"
    async with Runtime(state, identity_registry=registry_path) as web:
        policy = raw_control_policy(base, "fill", "click")
        result = await web.do(intent, policy)
        fenced = await web.do(intent, policy)

    assert result["receipt"]["status"] == "failed"
    assert result["receipt"]["failure"]["code"] == (
        "EXECUTION_OUTCOME_UNKNOWN")
    assert result["receipt"]["underlying_failure"] == "SCHEMA_CHANGED"
    assert result["outcome"] == {"state": "unknown", "certainty": "unknown",
        "reconciliation_required": True}
    assert fenced["receipt"]["failure"]["code"] == (
        "EXECUTION_OUTCOME_UNKNOWN")
    assert saves.raw_drift_gets == 1
    assert saves == []
    assert '"state":"uncertain"' in (
        state / "actions" / "journal.jsonl").read_text()


@pytest.mark.skipif(not browser_use_config.installed(),
                    reason="pinned Browser Use SDK is not installed")
async def test_raw_control_blocks_worker_websocket_before_first_request(
        reversible_draft_browser, tmp_path):
    base, _context, _original, registry_path, saves = reversible_draft_browser
    secret = "worker-websocket-secret"
    intent = WebIntent(base + "/raw/worker", RAW_BROWSER_CONTROL_CONTRACT,
        "WRITE_EXTERNAL", "raw-worker-escape",
        "Block a worker network escape", (
            BrowserAction("fill", "#worker-secret", secret),
            BrowserAction("click", "#worker-start"),
        ))
    state = tmp_path / "worker-escape-state"
    async with Runtime(state, identity_registry=registry_path) as web:
        result = await web.do(intent, raw_control_policy(base, "fill", "click"))

    assert result["receipt"]["failure"]["code"] == (
        "EXECUTION_OUTCOME_UNKNOWN")
    assert result["receipt"]["underlying_failure"] == "POLICY_DENIED"
    assert saves.foreign_hits == []
    assert secret not in str(result)
    assert secret not in (state / "actions" / "journal.jsonl").read_text()


@pytest.mark.skipif(not browser_use_config.installed(),
                    reason="pinned Browser Use SDK is not installed")
async def test_raw_control_blocks_dynamic_blank_target_before_get(
        reversible_draft_browser, tmp_path):
    base, _context, _original, registry_path, saves = reversible_draft_browser
    intent = WebIntent(base + "/raw/popup", RAW_BROWSER_CONTROL_CONTRACT,
        "WRITE_EXTERNAL", "raw-popup-escape",
        "Block a dynamically created popup", (
            BrowserAction("click", "#popup-start"),
        ))
    async with Runtime(tmp_path / "popup-state",
                       identity_registry=registry_path) as web:
        result = await web.do(intent, raw_control_policy(base, "click"))

    assert result["receipt"]["failure"]["code"] == (
        "EXECUTION_OUTCOME_UNKNOWN")
    assert result["receipt"]["underlying_failure"] == "POLICY_DENIED"
    assert saves.foreign_hits == []


@pytest.mark.skipif(not browser_use_config.installed(),
                    reason="pinned Browser Use SDK is not installed")
async def test_raw_control_blocks_button_external_protocol_navigation(
        reversible_draft_browser, tmp_path):
    base, context, original, registry_path, _saves = reversible_draft_browser
    intent = WebIntent(base + "/raw/external-protocol",
        RAW_BROWSER_CONTROL_CONTRACT, "WRITE_EXTERNAL",
        "raw-external-protocol", "Block an external protocol handler", (
            BrowserAction("click", "#external-start"),
        ))
    async with Runtime(tmp_path / "external-protocol-state",
                       identity_registry=registry_path) as web:
        result = await web.do(intent, raw_control_policy(base, "click"))

    assert result["receipt"]["failure"]["code"] == (
        "EXECUTION_OUTCOME_UNKNOWN")
    assert result["receipt"]["underlying_failure"] == "POLICY_DENIED"
    assert context.pages == [original] and not original.is_closed()


@pytest.mark.skipif(not browser_use_config.installed(),
                    reason="pinned Browser Use SDK is not installed")
async def test_raw_control_reports_cross_origin_redirect_as_policy_denied(
        reversible_draft_browser, tmp_path):
    base, _context, _original, registry_path, saves = reversible_draft_browser
    intent = WebIntent(base + "/raw/redirect-external",
        RAW_BROWSER_CONTROL_CONTRACT, "WRITE_EXTERNAL",
        "raw-redirect-external", "Block a cross-origin redirect", (
            BrowserAction("click", "#never-reached"),
        ))
    async with Runtime(tmp_path / "redirect-external-state",
                       identity_registry=registry_path) as web:
        result = await web.do(intent, raw_control_policy(base, "click"))

    assert result["receipt"]["failure"]["code"] == (
        "EXECUTION_OUTCOME_UNKNOWN")
    assert result["receipt"]["underlying_failure"] == "POLICY_DENIED"
    assert saves.foreign_hits == []


@pytest.mark.skipif(not browser_use_config.installed(),
                    reason="pinned Browser Use SDK is not installed")
async def test_raw_control_ignores_main_world_selector_spoof(
        reversible_draft_browser, tmp_path):
    base, _context, _original, registry_path, saves = reversible_draft_browser
    secret = "must-never-enter-password"
    intent = WebIntent(base + "/raw/spoof", RAW_BROWSER_CONTROL_CONTRACT,
        "WRITE_EXTERNAL", "raw-selector-spoof",
        "Reject a spoofed password control", (
            BrowserAction("fill", "#victim", secret),
        ))
    state = tmp_path / "selector-spoof-state"
    async with Runtime(state, identity_registry=registry_path) as web:
        result = await web.do(intent, raw_control_policy(base, "fill"))

    assert result["receipt"]["failure"]["code"] == (
        "EXECUTION_OUTCOME_UNKNOWN")
    assert result["receipt"]["underlying_failure"] == "POLICY_DENIED"
    assert saves.spoof_values == []
    assert secret not in str(result)
    assert secret not in (state / "actions" / "journal.jsonl").read_text()
    html_evidence = list((state / "evidence").glob("*.html"))
    assert html_evidence
    assert any('id="victim"' in path.read_text() for path in html_evidence)
    assert all(path.read_text() != "<html>forged-evidence</html>"
               for path in html_evidence)


@pytest.mark.skipif(not browser_use_config.installed(),
                    reason="pinned Browser Use SDK is not installed")
async def test_target_guard_leaves_unrelated_tab_open(
        reversible_draft_browser, tmp_path):
    base, context, original, registry_path, _saves = reversible_draft_browser
    intent = WebIntent(base + "/raw/drift", RAW_BROWSER_CONTROL_CONTRACT,
        "WRITE_EXTERNAL", "raw-unrelated-tab",
        "Wait while an unrelated tab opens", (
            BrowserAction("wait_for", "#never-appears", state="visible"),
            BrowserAction("fill", "#raw-title", "bounded mutation"),
        ))
    state = tmp_path / "unrelated-tab-state"
    async with Runtime(state, identity_registry=registry_path) as web:
        running = asyncio.create_task(web.do(intent,
            raw_control_policy(base, "wait_for", "fill")))
        for _ in range(200):
            if any(page is not original and "/raw/drift" in page.url
                   for page in context.pages):
                break
            await asyncio.sleep(0.025)
        else:
            pytest.fail("owned action page did not become ready")
        unrelated = await context.new_page()
        await unrelated.goto(base + "/account")
        result = await running

    assert result["receipt"]["failure"]["code"] == (
        "EXECUTION_OUTCOME_UNKNOWN")
    assert not unrelated.is_closed()
    assert await unrelated.locator("#owner").count() == 1
    assert original in context.pages and not original.is_closed()
    await unrelated.close()


@pytest.mark.skipif(not browser_use_config.installed(),
                    reason="pinned Browser Use SDK is not installed")
async def test_raw_control_fences_post_effect_failure_as_uncertain(
        reversible_draft_browser, tmp_path):
    base, _context, _original, registry_path, saves = reversible_draft_browser
    intent = WebIntent(base + "/raw/uncertain", RAW_BROWSER_CONTROL_CONTRACT,
        "WRITE_EXTERNAL", "raw-control-uncertain",
        "Fail verification after a raw write", (
            BrowserAction("fill", "#raw-title", "Uncertain raw title"),
            BrowserAction("click", "#raw-save"),
            BrowserAction("wait_for", "#raw-complete", state="visible"),
            BrowserAction("assert_text", "#raw-complete", "Never observed"),
        ))
    state = tmp_path / "uncertain-state"
    async with Runtime(state, identity_registry=registry_path) as web:
        policy = raw_control_policy(
            base, "fill", "click", "wait_for", "assert_text")
        first = await web.do(intent, policy)
        fenced = await web.do(intent, policy)

    assert first["receipt"]["failure"]["code"] == (
        "EXECUTION_OUTCOME_UNKNOWN")
    assert first["receipt"]["underlying_failure"] == "SCHEMA_CHANGED"
    assert first["outcome"] == {"state": "unknown", "certainty": "unknown",
        "reconciliation_required": True}
    assert fenced["receipt"]["failure"]["code"] == (
        "EXECUTION_OUTCOME_UNKNOWN")
    assert saves == [{"title": "Uncertain raw title"}]
    assert '"state":"uncertain"' in (
        state / "actions" / "journal.jsonl").read_text()
