"""Isolated Browser Use executor for one attested LOCAL_ONLY action request."""
from __future__ import annotations

import asyncio
import base64
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import sys
from time import monotonic
from urllib.parse import urljoin, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from frankensurf.browser_use_config import SDK_VERSION, origin


FAILURES = frozenset({
    "POLICY_DENIED", "SCHEMA_CHANGED", "TIMEOUT", "PROVIDER_DOWN",
    "PROVIDER_UNAVAILABLE", "IDENTITY_EXECUTOR_OFFLINE",
    "EXECUTION_OUTCOME_UNKNOWN", "LIMIT_EXCEEDED",
})
FIXTURE_ACTION_CONTRACT = "local_fixture.reversible_draft.v1"
RAW_ACTION_CONTRACT = "browser.raw_control.v1"
FORM_CONTRACT = "reversible-draft-v1"
SAFE_PREFLIGHT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


ELEMENT_METADATA_BODY = r"""
          const e = this;
          const attr = (node, name) =>
            Element.prototype.getAttribute.call(node, name);
          const has = (node, name) =>
            Element.prototype.hasAttribute.call(node, name);
          const tag = String(e.tagName || '').toUpperCase();
          const type = String(attr(e, 'type') || '').toLowerCase();
          const style = getComputedStyle(e);
          const visible = Boolean(
            Element.prototype.getClientRects.call(e).length &&
            style.visibility !== 'hidden' && style.display !== 'none' &&
            style.opacity !== '0' && style.pointerEvents !== 'none');
          const form = ('form' in e) ? e.form : null;
          const base = Document.prototype.querySelector.call(
            document, 'base[target]');
          const baseTarget = base ? String(attr(base, 'target') || '') : '';
          const formTarget = form ? String(
            attr(e, 'formtarget') || attr(form, 'target') || baseTarget) : '';
          const absolute = value => {
            if (!value) return '';
            try { return new URL(value, document.baseURI).href; }
            catch (_) { return '\u0000invalid'; }
          };
          const formAction = form ? absolute(
            attr(e, 'formaction') || attr(form, 'action') || document.URL) : '';
          const formContract = form ? String(
            attr(form, 'data-frankensurf-contract') || '') : '';
          const action = String(
            attr(e, 'data-frankensurf-action') || '');
          const href = tag === 'A' ? absolute(attr(e, 'href') || '') : '';
          const target = String(attr(e, 'target') || baseTarget);
          const metadata = {tag,type,visible,
            disabled:Boolean(Element.prototype.matches.call(e, ':disabled')) ||
              attr(e, 'aria-disabled') === 'true',
            readOnly:has(e, 'readonly'),formTarget,formAction,formContract,
            action,href,target,download:has(e, 'download')};
"""
ELEMENT_METADATA_FUNCTION = "function() {" + ELEMENT_METADATA_BODY + "return metadata;}"
ATOMIC_FILL_FUNCTION = "function(expected,value) {" + ELEMENT_METADATA_BODY + r"""
          if (JSON.stringify(metadata) !== JSON.stringify(expected))
            return {ok:false};
          const prototype = tag === 'INPUT' ?
            HTMLInputElement.prototype : HTMLTextAreaElement.prototype;
          const descriptor = Object.getOwnPropertyDescriptor(prototype, 'value');
          if (!descriptor || typeof descriptor.set !== 'function')
            return {ok:false};
          descriptor.set.call(e, String(value));
          e.dispatchEvent(new Event('input', {bubbles:true,composed:true}));
          e.dispatchEvent(new Event('change', {bubbles:true,composed:true}));
          return {ok:true};
        }"""
ATOMIC_CLICK_FUNCTION = "function(expected) {" + ELEMENT_METADATA_BODY + r"""
          if (JSON.stringify(metadata) !== JSON.stringify(expected))
            return {ok:false};
          const descriptor = Object.getOwnPropertyDescriptor(
            HTMLElement.prototype, 'click');
          if (!descriptor || typeof descriptor.value !== 'function')
            return {ok:false};
          const click = descriptor.value;
          click.call(e);
          return {ok:true};
        }"""
ELEMENT_PROPERTY_FUNCTION = r"""function(name) {
          if (name === 'text') {
            const getter = Object.getOwnPropertyDescriptor(
              Node.prototype, 'textContent').get;
            return {ok:true,value:String(getter.call(this) || '')};
          }
          const tag = String(this.tagName || '').toUpperCase();
          const prototypes = {INPUT:HTMLInputElement.prototype,
            TEXTAREA:HTMLTextAreaElement.prototype,
            SELECT:HTMLSelectElement.prototype};
          const prototype = prototypes[tag];
          const descriptor = prototype &&
            Object.getOwnPropertyDescriptor(prototype, 'value');
          if (name !== 'value' || !descriptor ||
              typeof descriptor.get !== 'function') return {ok:false};
          return {ok:true,value:String(descriptor.get.call(this))};
        }"""


class WorkerFailure(Exception):
    def __init__(self, code):
        self.code = code if code in FAILURES else "PROVIDER_DOWN"
        super().__init__(self.code)


def _remaining(state, cap=None):
    seconds = state["deadline"] - monotonic()
    if cap is not None:
        seconds = min(seconds, cap)
    if seconds <= 0:
        raise WorkerFailure("TIMEOUT")
    return seconds


async def _bounded(state, operation, cap=None):
    try:
        return await asyncio.wait_for(operation(), _remaining(state, cap))
    except TimeoutError:
        raise WorkerFailure("TIMEOUT") from None


def _loopback(endpoint):
    try:
        parsed = urlsplit(endpoint)
        parsed.port
    except (TypeError, ValueError):
        return False
    return (parsed.scheme in {"http", "https"}
            and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            and not parsed.username and not parsed.password
            and parsed.path in {"", "/"} and not parsed.query
            and not parsed.fragment)


def _raw_origin_allowed(value):
    """Allow encrypted remote origins and HTTP only on loopback."""
    try:
        parsed = urlsplit(value)
        parsed.port
    except (TypeError, ValueError):
        return False
    return (not parsed.username and not parsed.password
            and not parsed.path and not parsed.query and not parsed.fragment
            and (parsed.scheme == "https"
                 or (parsed.scheme == "http" and parsed.hostname
                     in {"127.0.0.1", "localhost", "::1"})))


def _mark_effect(path):
    marker = Path(path)
    descriptor = os.open(marker,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(b"effect-started\n")
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(marker.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


async def _ready(page, state):
    while True:
        world = await _isolated_world(page, state)
        result = await _bounded(state,
            lambda: world["client"].send.Runtime.evaluate(params={
                "expression": ("Object.getOwnPropertyDescriptor("
                    "Document.prototype,'readyState').get.call(document)"),
                "contextId": world["context_id"],
                "returnByValue": True,
                "silent": True,
            }, session_id=world["session_id"]))
        if "exceptionDetails" in result:
            raise WorkerFailure("PROVIDER_DOWN")
        ready = result.get("result", {}).get("value")
        if ready in {"interactive", "complete"}:
            return world
        await asyncio.sleep(min(0.05, _remaining(state)))


async def _capture(page, state, maximum_html, maximum_image):
    url = await _bounded(state, page.get_url)
    session_id = await _bounded(state, lambda: page.session_id)
    document = await _bounded(state,
        lambda: page._client.send.DOM.getDocument(session_id=session_id))
    try:
        node_id = document["root"]["nodeId"]
    except (KeyError, TypeError):
        raise WorkerFailure("PROVIDER_DOWN") from None
    result = await _bounded(state,
        lambda: page._client.send.DOM.getOuterHTML(
            params={"nodeId": node_id, "includeShadowDOM": True},
            session_id=session_id))
    content = result.get("outerHTML")
    if not isinstance(content, str) or len(content.encode()) > maximum_html:
        raise WorkerFailure("LIMIT_EXCEEDED" if isinstance(content, str)
                            else "PROVIDER_DOWN")
    screenshot = await _bounded(state, lambda: page.screenshot(format="png"))
    try:
        decoded = base64.b64decode(screenshot, validate=True)
    except (ValueError, TypeError):
        raise WorkerFailure("PROVIDER_DOWN") from None
    if len(decoded) > maximum_image:
        raise WorkerFailure("LIMIT_EXCEEDED")
    return {"url": url, "html": content, "screenshot": screenshot}


async def _isolated_world(page, state):
    session_id = await _bounded(state, lambda: page.session_id)
    tree = await _bounded(state, lambda: page._client.send.Page.getFrameTree(
        session_id=session_id))
    try:
        frame_id = tree["frameTree"]["frame"]["id"]
    except (KeyError, TypeError):
        raise WorkerFailure("PROVIDER_DOWN") from None
    result = await _bounded(state,
        lambda: page._client.send.Page.createIsolatedWorld(params={
            "frameId": frame_id,
            "worldName": "frankensurf-control-policy",
            "grantUniveralAccess": False,
        }, session_id=session_id))
    context_id = result.get("executionContextId")
    if type(context_id) is not int:
        raise WorkerFailure("PROVIDER_DOWN")
    return {"client": page._client, "session_id": session_id,
            "context_id": context_id}


async def _elements(page, selector, state, cap=None):
    try:
        elements = await _bounded(state,
            lambda: page.get_elements_by_css_selector(selector), cap)
    except WorkerFailure:
        raise
    except Exception:
        raise WorkerFailure("SCHEMA_CHANGED") from None
    if not isinstance(elements, list):
        raise WorkerFailure("PROVIDER_DOWN")
    return elements


async def _element_call(element, world, state, function, *args, cap=None,
                        user_gesture=False):
    backend_node_id = getattr(element, "_backend_node_id", None)
    if (type(backend_node_id) is not int
            or getattr(element, "_session_id", None) != world["session_id"]):
        raise WorkerFailure("PROVIDER_DOWN")
    try:
        resolved = await _bounded(state,
            lambda: world["client"].send.DOM.resolveNode(params={
                "backendNodeId": backend_node_id,
                "executionContextId": world["context_id"],
            }, session_id=world["session_id"]), cap)
        object_id = resolved["object"]["objectId"]
        parameters = {
            "functionDeclaration": function,
            "objectId": object_id,
            "arguments": [{"value": value} for value in args],
            "returnByValue": True,
            "awaitPromise": False,
            "silent": True,
            "userGesture": user_gesture,
        }
        result = await _bounded(state,
            lambda: world["client"].send.Runtime.callFunctionOn(
                params=parameters, session_id=world["session_id"]), cap)
        if "exceptionDetails" in result or "value" not in result.get("result", {}):
            raise WorkerFailure("SCHEMA_CHANGED")
        return result["result"]["value"]
    except WorkerFailure:
        raise
    except (KeyError, TypeError):
        raise WorkerFailure("PROVIDER_DOWN") from None
    except Exception:
        raise WorkerFailure("SCHEMA_CHANGED") from None
    finally:
        if "object_id" in locals():
            try:
                await world["client"].send.Runtime.releaseObject(
                    params={"objectId": object_id},
                    session_id=world["session_id"])
            except Exception:
                pass


async def _element_metadata(element, world, state, cap=None):
    value = await _element_call(
        element, world, state, ELEMENT_METADATA_FUNCTION, cap=cap)
    required = {"tag", "type", "visible", "disabled", "readOnly",
                "formTarget", "formAction", "formContract", "action",
                "href", "target", "download"}
    if type(value) is not dict or set(value) != required:
        raise WorkerFailure("PROVIDER_DOWN")
    if (type(value["tag"]) is not str or type(value["type"]) is not str
            or any(type(value[key]) is not bool for key in (
                "visible", "disabled", "readOnly", "download"))
            or any(type(value[key]) is not str for key in (
                "formTarget", "formAction", "formContract", "action",
                "href", "target"))):
        raise WorkerFailure("PROVIDER_DOWN")
    return value


def _scoped_url(value, origins):
    try:
        return not value or origin(value) in origins
    except (TypeError, ValueError):
        return False


def _validate_fill(metadata, *, fixture):
    if (metadata["tag"] not in {"INPUT", "TEXTAREA"}
            or metadata["type"] in {"password", "file", "hidden",
                                     "submit", "button", "reset", "image"}
            or metadata["disabled"] or metadata["readOnly"]
            or not metadata["visible"]):
        raise WorkerFailure("POLICY_DENIED")
    if fixture and metadata["formContract"] != FORM_CONTRACT:
        raise WorkerFailure("POLICY_DENIED")


def _validate_click(metadata, *, fixture, origins):
    if metadata["tag"] == "BUTTON":
        permitted_click = metadata["type"] in {"", "button", "submit"}
    elif metadata["tag"] == "INPUT":
        permitted_click = metadata["type"] in {
            "button", "submit", "checkbox", "radio"}
    elif metadata["tag"] == "A":
        permitted_click = (bool(metadata["href"])
                           and _scoped_url(metadata["href"], origins)
                           and not metadata["download"])
    else:
        permitted_click = False
    if (not permitted_click or metadata["disabled"]
            or not metadata["visible"]
            or metadata["formTarget"] not in {"", "_self"}
            or metadata["target"] not in {"", "_self"}
            or not _scoped_url(metadata["formAction"], origins)):
        raise WorkerFailure("POLICY_DENIED")
    if (fixture and (metadata["formContract"] != FORM_CONTRACT
            or metadata["action"] != "save-reversible-draft")):
        raise WorkerFailure("POLICY_DENIED")


async def _preflight_actions(page, actions, world, state, *, fixture, origins):
    """Validate the complete current control surface before any effect."""
    bindings = {}
    for index, action in enumerate(actions):
        elements = await _elements(page, action["selector"], state)
        if action["tool"] in {"fill", "click"}:
            if len(elements) != 1:
                raise WorkerFailure("SCHEMA_CHANGED")
            element = elements[0]
            metadata = await _element_metadata(element, world, state)
            if action["tool"] == "fill":
                _validate_fill(metadata, fixture=fixture)
            else:
                _validate_click(metadata, fixture=fixture, origins=origins)
            backend_node_id = getattr(element, "_backend_node_id", None)
            if type(backend_node_id) is not int:
                raise WorkerFailure("PROVIDER_DOWN")
            bindings[index] = backend_node_id
        elif len(elements) > 1:
            # A later wait/assert may be absent now, but if it is already
            # present its selector must be deterministic.
            raise WorkerFailure("SCHEMA_CHANGED")
    return bindings


async def _one_element(page, selector, state):
    elements = await _elements(page, selector, state)
    if len(elements) != 1:
        raise WorkerFailure("SCHEMA_CHANGED")
    return elements[0]


async def _wait_for(page, selector, expected_state, state, action_timeout):
    deadline = min(state["deadline"], monotonic() + action_timeout)
    while monotonic() < deadline:
        elements = await _elements(page, selector, state, action_timeout)
        if len(elements) > 1:
            raise WorkerFailure("SCHEMA_CHANGED")
        if expected_state == "attached" and len(elements) == 1:
            return
        if expected_state == "visible" and len(elements) == 1:
            world = await _isolated_world(page, state)
            metadata = await _element_metadata(
                elements[0], world, state, action_timeout)
            if metadata["visible"]:
                return
        await asyncio.sleep(min(0.05, max(0, deadline - monotonic())))
    raise WorkerFailure("TIMEOUT")


async def _check_targets(browser, guard, state):
    owned_targets = guard["owned_targets"]
    result = await _bounded(state, browser.cdp_client.send.Target.getTargets)
    unexpected = []
    pending = list(result.get("targetInfos", []))
    changed = True
    while changed:
        changed = False
        for item in pending:
            target_id = item.get("targetId")
            if (isinstance(target_id, str) and target_id not in owned_targets
                    and item.get("openerId") in owned_targets):
                owned_targets.add(target_id)
                unexpected.append(target_id)
                changed = True
    for target in unexpected:
        guard["blocked_targets"].add(target)
        for _ in range(3):
            try:
                closed = await browser.cdp_client.send.Target.closeTarget(
                    {"targetId": target})
                if closed.get("success") is True:
                    guard["blocked_targets"].discard(target)
                    break
            except Exception:
                pass
    if unexpected:
        raise WorkerFailure("POLICY_DENIED")


async def _target_guard(browser, owned_target, owned_session, state, denied,
                        tasks):
    """Pause and close every target spawned by the owned action page.

    The guard temporarily wraps Browser Use's root target handler.  This keeps
    owned descendants paused until they are closed, while the original handler
    immediately resumes unrelated targets (including a tab opened by the user).
    """
    client = browser.cdp_client
    registry = client._event_registry
    original = registry._handlers.get("Target.attachedToTarget")
    guard = {"client": client, "original": original,
             "owned_session": owned_session,
             "owned_sessions": {owned_session},
             "owned_targets": {owned_target}, "blocked_targets": set()}

    async def handle_attached(event, parent_session):
        target = event.get("targetInfo", {})
        target_id = target.get("targetId")
        session_id = event.get("sessionId")
        waiting = event.get("waitingForDebugger") is True
        if not isinstance(target_id, str) or not isinstance(session_id, str):
            denied.append("PROVIDER_DOWN")
            return
        try:
            if target_id == owned_target:
                guard["owned_sessions"].add(session_id)
                await client.send.Target.setAutoAttach(params={
                    "autoAttach": True,
                    "waitForDebuggerOnStart": True,
                    "flatten": True,
                }, session_id=session_id)
                if waiting:
                    await client.send.Runtime.runIfWaitingForDebugger(
                        session_id=session_id)
                return
            owned = (parent_session in guard["owned_sessions"]
                     or target.get("openerId") in guard["owned_targets"])
            if owned:
                guard["owned_targets"].add(target_id)
                guard["owned_sessions"].add(session_id)
                guard["blocked_targets"].add(target_id)
                denied.append("POLICY_DENIED")
                closed = await client.send.Target.closeTarget(
                    params={"targetId": target_id})
                if closed.get("success") is not True:
                    denied.append("PROVIDER_DOWN")
                else:
                    guard["blocked_targets"].discard(target_id)
                return
            if original is not None:
                result = original(event, parent_session)
                if asyncio.iscoroutine(result):
                    task = asyncio.create_task(result)
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
            elif waiting:
                await client.send.Runtime.runIfWaitingForDebugger(
                    session_id=session_id)
        except Exception:
            denied.append("PROVIDER_DOWN")

    def attached(event, parent_session):
        task = asyncio.create_task(handle_attached(event, parent_session))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    client.register.Target.attachedToTarget(attached)
    root_autoattach = False
    try:
        await _bounded(state, lambda: client.send.Target.setAutoAttach(params={
            "autoAttach": True,
            "waitForDebuggerOnStart": True,
            "flatten": True,
        }))
        root_autoattach = True
        await _bounded(state, lambda: client.send.Target.setAutoAttach(params={
            "autoAttach": True,
            "waitForDebuggerOnStart": True,
            "flatten": True,
        }, session_id=owned_session))
    except Exception:
        if root_autoattach:
            for session_id in (owned_session, None):
                try:
                    await asyncio.wait_for(
                        client.send.Target.setAutoAttach(params={
                            "autoAttach": True,
                            "waitForDebuggerOnStart": False,
                            "flatten": True,
                        }, session_id=session_id), 0.5)
                except Exception:
                    pass
        if original is None:
            registry.unregister("Target.attachedToTarget")
        else:
            registry.register("Target.attachedToTarget", original)
        raise WorkerFailure("PROVIDER_DOWN") from None
    return guard


async def _stop_target_guard(guard):
    if guard is None:
        return
    client = guard["client"]
    registry = client._event_registry
    original = guard["original"]
    if original is None:
        registry.unregister("Target.attachedToTarget")
    else:
        registry.register("Target.attachedToTarget", original)
    for target_id in tuple(guard["blocked_targets"]):
        for _ in range(3):
            try:
                result = await client.send.Target.closeTarget(
                    params={"targetId": target_id})
                if result.get("success") is True:
                    guard["blocked_targets"].discard(target_id)
                    break
            except Exception:
                pass
    for session_id in (guard["owned_session"], None):
        try:
            await client.send.Target.setAutoAttach(params={
                "autoAttach": True,
                "waitForDebuggerOnStart": False,
                "flatten": True,
            }, session_id=session_id)
        except Exception:
            pass


def _install_navigation_guard(browser, owned_session, origins, denied, tasks):
    """Cancel renderer navigations that bypass Fetch, including custom schemes."""
    client = browser.cdp_client
    registry = client._event_registry
    methods = ("Page.frameRequestedNavigation", "Page.frameStartedNavigating")
    originals = {method: registry._handlers.get(method) for method in methods}

    def handler_for(method):
        original = originals[method]

        def navigation(event, event_session):
            target = event.get("url", "")
            if event_session == owned_session:
                try:
                    permitted = origin(target) in origins
                except (TypeError, ValueError):
                    permitted = False
                if not permitted:
                    denied.append("POLICY_DENIED")

                    async def cancel():
                        failed = False
                        try:
                            await client.send.Page.stopLoading(
                                session_id=owned_session)
                        except Exception:
                            failed = True
                        try:
                            await client.send.Runtime.terminateExecution(
                                session_id=owned_session)
                        except Exception:
                            failed = True
                        if failed:
                            denied.append("PROVIDER_DOWN")

                    task = asyncio.create_task(cancel())
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
            if original is not None:
                result = original(event, event_session)
                if asyncio.iscoroutine(result):
                    task = asyncio.create_task(result)
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)

        return navigation

    for method in methods:
        registry.register(method, handler_for(method))
    return {"registry": registry, "originals": originals}


def _stop_navigation_guard(guard):
    if guard is None:
        return
    for method, original in guard["originals"].items():
        if original is None:
            guard["registry"].unregister(method)
        else:
            guard["registry"].register(method, original)


async def _drain_tasks(tasks, state):
    while tasks:
        pending = tuple(tasks)
        await _bounded(state,
            lambda: asyncio.gather(*pending, return_exceptions=True))


async def _drain_cleanup_tasks(tasks, seconds=0.75):
    """Best-effort bounded drain while an untrusted page can still emit work."""
    deadline = monotonic() + seconds
    try:
        # Let the CDP reader dispatch a just-arrived denial even when no answer
        # task had been visible at exception-classification time.
        await asyncio.sleep(min(0.01, seconds))
        while tasks:
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            pending = tuple(tasks)
            await asyncio.wait(pending, timeout=remaining)
    except Exception:
        pass
    leftovers = tuple(tasks)
    for task in leftovers:
        task.cancel()
    if leftovers:
        try:
            await asyncio.wait_for(asyncio.gather(
                *leftovers, return_exceptions=True), 0.25)
        except Exception:
            pass
    return bool(leftovers)


async def _run(request):
    if version("browser-use") != SDK_VERSION:
        raise WorkerFailure("PROVIDER_UNAVAILABLE")
    endpoint = request.get("endpoint")
    if not _loopback(endpoint):
        raise WorkerFailure("POLICY_DENIED")
    policy = request.get("policy")
    intent = request.get("intent")
    origins = request.get("origins")
    if (type(policy) is not dict or type(intent) is not dict
            or not isinstance(origins, list) or not origins):
        raise WorkerFailure("POLICY_DENIED")
    try:
        if any(origin(value) != value for value in origins):
            raise ValueError()
        if origin(intent["url"]) not in origins:
            raise ValueError()
        target = urlsplit(intent["url"])
        contract = intent.get("contract")
        if contract not in {FIXTURE_ACTION_CONTRACT, RAW_ACTION_CONTRACT}:
            raise ValueError()
        fixture = contract == FIXTURE_ACTION_CONTRACT
        if (fixture and (target.scheme != "http"
                or target.hostname not in {"127.0.0.1", "localhost", "::1"}
                or any(urlsplit(value).scheme != "http"
                       or urlsplit(value).hostname
                          not in {"127.0.0.1", "localhost", "::1"}
                       for value in origins))):
            raise ValueError()
        if not fixture and (not _raw_origin_allowed(origin(intent["url"]))
                or any(not _raw_origin_allowed(value) for value in origins)):
            raise ValueError()
        timeout = policy["timeout_seconds"]
        action_timeout = policy["action_timeout_seconds"]
        settle = policy["settle_ms"]
        maximum_html = policy["max_bytes"]
        maximum_image = policy["max_image_bytes"]
        actions = intent["actions"]
        allowed_tools = set(policy["allowed_tools"])
        if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                or timeout <= 0 or type(action_timeout) not in (int, float)
                or not math.isfinite(action_timeout) or action_timeout <= 0
                or type(settle) is not int or settle < 0
                or type(maximum_html) is not int or maximum_html < 1
                or type(maximum_image) is not int or maximum_image < 1
                or not isinstance(actions, list)
                or any(type(action) is not dict
                       or action.get("tool") not in allowed_tools
                       for action in actions)):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise WorkerFailure("POLICY_DENIED") from None

    from browser_use import Browser
    state = {"deadline": monotonic() + timeout, "effect_started": False,
             "failure": None}
    packet = {"status": "failed", "failure": "PROVIDER_DOWN",
              "effect_started": False, "steps": [],
              "sdk_version": SDK_VERSION}
    browser = Browser(cdp_url=endpoint, is_local=False, use_cloud=False,
        keep_alive=True, permissions=[], enable_default_extensions=False,
        accept_downloads=False, auto_download_pdfs=False,
        captcha_solver=False, downloads_path=request["downloads_path"])
    page = None
    fetch_session = None
    target_guard = None
    navigation_guard = None
    before = None
    owned_target = None
    world = None
    denied = []
    guard_tasks = set()
    try:
        await _bounded(state, browser.start)
        page = await _bounded(state, browser.new_page)
        info = await _bounded(state, page.get_target_info)
        owned_target = info["targetId"]
        fetch_session = await page.session_id

        def scope_guard(event, event_session):
            request_data = event.get("request", {})
            target = request_data.get("url", "")
            resource = event.get("resourceType")
            request_method = str(request_data.get("method", "")).upper()
            try:
                permitted = (resource != "WebSocket"
                    and origin(target) in origins
                    and (state["effect_started"]
                         or request_method in SAFE_PREFLIGHT_METHODS))
                status = event.get("responseStatusCode")
                if type(status) is int and 300 <= status < 400:
                    locations = [header.get("value")
                        for header in event.get("responseHeaders", [])
                        if str(header.get("name", "")).lower() == "location"]
                    if locations or status in {301, 302, 303, 307, 308}:
                        permitted = (permitted and len(locations) == 1
                            and origin(urljoin(target, locations[0])) in origins)
            except (TypeError, ValueError):
                permitted = False
            if not permitted:
                denied.append("POLICY_DENIED")
            method = (browser.cdp_client.send.Fetch.continueRequest
                      if permitted else browser.cdp_client.send.Fetch.failRequest)
            params = ({"requestId": event["requestId"]}
                      if permitted else {"requestId": event["requestId"],
                                         "errorReason": "BlockedByClient"})

            async def answer():
                try:
                    await method(params=params,
                        session_id=event_session or fetch_session)
                except Exception:
                    denied.append("PROVIDER_DOWN")

            task = asyncio.create_task(answer())
            guard_tasks.add(task)
            task.add_done_callback(guard_tasks.discard)

        browser.cdp_client.register.Fetch.requestPaused(scope_guard)
        target_guard = await _target_guard(
            browser, owned_target, fetch_session, state, denied, guard_tasks)
        navigation_guard = _install_navigation_guard(
            browser, fetch_session, origins, denied, guard_tasks)
        await browser.cdp_client.send.Network.setBypassServiceWorker(
            params={"bypass": True}, session_id=fetch_session)
        await browser.cdp_client.send.Network.setBlockedURLs(
            params={"urls": ["ws://*", "wss://*", "mailto:*", "tel:*",
                             "sms:*", "intent:*", "market:*", "facetime:*",
                             "facetime-audio:*"]},
            session_id=fetch_session)
        await browser.cdp_client.send.Fetch.enable(
            params={"patterns": [
                {"urlPattern": "*", "requestStage": "Request"},
                {"urlPattern": "*", "requestStage": "Response"},
            ]},
            session_id=fetch_session)
        await browser.cdp_client.send.Page.setDownloadBehavior(
            params={"behavior": "deny"}, session_id=fetch_session)
        await browser.cdp_client.send.Page.addScriptToEvaluateOnNewDocument(
            params={"source": """
              Object.defineProperty(window,'open',{
                value:()=>null,writable:false,configurable:false});
              for (const name of ['SharedWorker','WebSocket','WebSocketStream',
                   'WebTransport','EventSource','RTCPeerConnection',
                   'webkitRTCPeerConnection']) {
                if (name in globalThis) Object.defineProperty(globalThis,name,{
                  value:class BlockedTarget {
                    constructor(){throw new DOMException('Blocked','SecurityError');}
                  },writable:false,configurable:false});
              }
              try { Object.defineProperty(navigator,'serviceWorker',{
                value:undefined,writable:false,configurable:false}); } catch (_) {}
              try { Object.defineProperty(Navigator.prototype,'sendBeacon',{
                value:()=>false,writable:false,configurable:false}); } catch (_) {}
              addEventListener('submit',event=>{
                if (!['','_self'].includes(String(event.target.target||''))) {
                  event.preventDefault(); event.stopImmediatePropagation();
                }
              },true);
            """},
            session_id=fetch_session)
        if not fixture:
            _mark_effect(request["effect_marker"])
            state["effect_started"] = True
            packet["effect_started"] = True
        await _bounded(state, lambda: page.goto(intent["url"]))
        world = await _ready(page, state)
        if denied or origin(await page.get_url()) not in origins:
            raise WorkerFailure("POLICY_DENIED")
        await _check_targets(browser, target_guard, state)
        before = await _capture(page, state, maximum_html, maximum_image)
        packet["before"] = before
        bindings = (await _preflight_actions(
            page, actions, world, state, fixture=fixture, origins=origins)
            if fixture else {})

        for index, action in enumerate(actions):
            tool, selector = action["tool"], action["selector"]
            if tool in {"fill", "click"} and not state["effect_started"]:
                _mark_effect(request["effect_marker"])
                state["effect_started"] = True
                packet["effect_started"] = True
            if tool == "fill":
                element = await _one_element(page, selector, state)
                if (fixture and getattr(element, "_backend_node_id", None)
                        != bindings[index]):
                    raise WorkerFailure("SCHEMA_CHANGED")
                if not fixture:
                    world = await _isolated_world(page, state)
                metadata = await _element_metadata(
                    element, world, state, action_timeout)
                _validate_fill(metadata, fixture=fixture)
                performed = await _element_call(
                    element, world, state, ATOMIC_FILL_FUNCTION,
                    metadata, action["value"], cap=action_timeout)
                if type(performed) is not dict or performed.get("ok") is not True:
                    raise WorkerFailure("SCHEMA_CHANGED")
            elif tool == "click":
                element = await _one_element(page, selector, state)
                if (fixture and getattr(element, "_backend_node_id", None)
                        != bindings[index]):
                    raise WorkerFailure("SCHEMA_CHANGED")
                if not fixture:
                    world = await _isolated_world(page, state)
                metadata = await _element_metadata(
                    element, world, state, action_timeout)
                _validate_click(metadata, fixture=fixture, origins=origins)
                performed = await _element_call(
                    element, world, state, ATOMIC_CLICK_FUNCTION,
                    metadata, cap=action_timeout, user_gesture=True)
                if type(performed) is not dict or performed.get("ok") is not True:
                    raise WorkerFailure("SCHEMA_CHANGED")
            elif tool == "wait_for":
                await _wait_for(page, selector, action["state"], state,
                                action_timeout)
            elif tool == "assert_text":
                element = await _one_element(page, selector, state)
                if not fixture:
                    world = await _isolated_world(page, state)
                observed = await _element_call(
                    element, world, state, ELEMENT_PROPERTY_FUNCTION,
                    "text", cap=action_timeout)
                if (type(observed) is not dict
                        or observed.get("ok") is not True
                        or type(observed.get("value")) is not str
                        or action["value"] not in observed["value"]):
                    raise WorkerFailure("SCHEMA_CHANGED")
            elif tool == "assert_value":
                element = await _one_element(page, selector, state)
                if not fixture:
                    world = await _isolated_world(page, state)
                observed = await _element_call(
                    element, world, state, ELEMENT_PROPERTY_FUNCTION,
                    "value", cap=action_timeout)
                if (type(observed) is not dict
                        or observed.get("ok") is not True
                        or observed.get("value") != action["value"]):
                    raise WorkerFailure("SCHEMA_CHANGED")
            else:
                raise WorkerFailure("POLICY_DENIED")
            if settle:
                await _bounded(state,
                    lambda: asyncio.sleep(settle / 1000), action_timeout)
            await _drain_tasks(guard_tasks, state)
            if denied:
                raise WorkerFailure("POLICY_DENIED")
            current_url = await _bounded(state, page.get_url)
            if origin(current_url) not in origins:
                raise WorkerFailure("POLICY_DENIED")
            await _check_targets(browser, target_guard, state)
            packet["steps"].append({"index": index, "tool": tool,
                                     "status": "completed"})
        await _drain_tasks(guard_tasks, state)
        if denied:
            raise WorkerFailure("POLICY_DENIED")
        await _check_targets(browser, target_guard, state)
        packet["after"] = await _capture(
            page, state, maximum_html, maximum_image)
        packet["url"] = packet["after"]["url"]
        packet["status"] = "completed"
        packet.pop("failure", None)
    except WorkerFailure as error:
        packet["failure"] = ("POLICY_DENIED"
            if "POLICY_DENIED" in denied else
            "PROVIDER_DOWN" if "PROVIDER_DOWN" in denied else error.code)
    except Exception as error:
        print("Action worker error type: " + type(error).__name__,
              file=sys.stderr)
        packet["failure"] = ("POLICY_DENIED"
            if "POLICY_DENIED" in denied else "PROVIDER_DOWN")
    finally:
        cleanup_exhausted = await _drain_cleanup_tasks(guard_tasks)
        if page is not None and "after" not in packet:
            try:
                packet["after"] = await _capture(
                    page, state, maximum_html, maximum_image)
                packet["url"] = packet["after"]["url"]
            except Exception:
                pass
        if page is not None and owned_target is not None:
            try:
                await browser.cdp_client.send.Target.closeTarget(
                    {"targetId": owned_target})
            except Exception:
                pass
        cleanup_exhausted = (await _drain_cleanup_tasks(guard_tasks)
                             or cleanup_exhausted)
        _stop_navigation_guard(navigation_guard)
        await _stop_target_guard(target_guard)
        containment_failed = (target_guard is not None
                              and bool(target_guard["blocked_targets"]))
        if (packet.get("status") == "failed"
                and "POLICY_DENIED" in denied):
            packet["failure"] = "POLICY_DENIED"
        if (packet.get("status") == "completed"
                and (denied or cleanup_exhausted or containment_failed)):
            packet["status"] = "failed"
            packet["failure"] = ("POLICY_DENIED" if denied
                                 else "PROVIDER_DOWN")
        try:
            await browser.stop()
        except Exception:
            pass
    return packet


def main():
    try:
        raw = sys.stdin.buffer.read()
        request = json.loads(raw)
        packet = asyncio.run(_run(request))
    except Exception as error:
        print("Action worker startup error type: " + type(error).__name__,
              file=sys.stderr)
        packet = {"status": "failed", "failure": "PROVIDER_DOWN",
                  "effect_started": False, "steps": [],
                  "sdk_version": SDK_VERSION}
    sys.stdout.write(json.dumps(packet, separators=(",", ":")))


if __name__ == "__main__":
    main()
