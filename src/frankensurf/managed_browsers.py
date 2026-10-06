"""T2 managed browsers: hosted Chromium sessions driven over CDP.

Each plugin wraps one mature hosted browser service: Browserbase, Steel Cloud,
Hyperbrowser, Browserless, Kernel and Anchor. A read opens one session, renders the page with
Playwright over the service's CDP websocket, captures the HTML and releases the
session. Core keeps routing, pacing, policy, content checks, receipts and
evidence, exactly as for the hosted extraction plugins.

Keys come from the same settings as the other hosted plugins
(``FRANKENSURF_<SERVICE>_API_KEY``, ``~/.config/frankensurf/.env`` or
``services.json``). Connection URLs carry the key, so errors never include
them. Sessions are billed by time; cost is the measured session length times
``<service>_usd_per_hour``, rounded up to the service's billing minimum.
"""
from __future__ import annotations

import json
import time
from urllib.parse import quote

from .hosted_providers import (
    _call,
    _failure,
    _Keyed,
    _manifest,
    _rate,
    _service_status,
    _setting,
    _target_status,
    _within_cap,
)


class _ManagedBrowser(_Keyed):
    service = ""
    # Approximate list prices (Oct 2026). Set <id>_usd_per_hour to your plan.
    usd_per_hour = 0.0
    billing_minimum_seconds = 60

    def __init__(self, identifier):
        self.manifest = _manifest(identifier, "1", rendering=True, paid=True, cost_bounded=True)

    async def open_session(self, policy):
        """Return (cdp_endpoint, release) where release() ends the session."""
        raise NotImplementedError

    def _worst_case(self, policy):
        seconds = max(policy.timeout_seconds, self.billing_minimum_seconds)
        return seconds / 3600 * self._hourly()

    def _hourly(self):
        return _rate(self.manifest.id + "_usd_per_hour", self.usd_per_hour)

    def _cost(self, seconds):
        billed = max(seconds, self.billing_minimum_seconds)
        return round(billed / 3600 * self._hourly(), 6)

    async def acquire(self, request, services):
        policy = request.policy
        _within_cap(policy, self._worst_case(policy), self.service)
        started = time.monotonic()
        endpoint, release = await self.open_session(policy)
        try:
            page = await _render(endpoint, request.url, policy, self.service)
        finally:
            try:
                await release()
            except Exception:
                pass
        cost = self._cost(time.monotonic() - started)
        error = _target_status(page["http_status"], page["url"], cost)
        if error:
            raise error
        page["cost_usd"] = cost
        return page


async def _render(endpoint, url, policy, service):
    """Navigate one page in a remote browser and return Core's page shape."""
    from playwright.async_api import Error as PWError
    from playwright.async_api import TimeoutError as PWTimeout
    from playwright.async_api import async_playwright
    timeout_ms = policy.timeout_seconds * 1000
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(endpoint, timeout=timeout_ms)
            from .profiles import ACTIVE
            profile = ACTIVE.get() if getattr(policy, "profile", None) else None
            try:
                if profile is not None:
                    context = await browser.new_context(**profile.context_options())
                else:
                    context = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await context.new_page()
                response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                if policy.wait_selector:
                    await page.locator(policy.wait_selector).first.wait_for(
                        state=policy.wait_state, timeout=timeout_ms)
                if policy.settle_ms:
                    await page.wait_for_timeout(policy.settle_ms)
                content_type = response.headers.get("content-type", "") if response else ""
                if "json" in content_type.lower():
                    raw = await response.body()
                    content = raw.decode("utf-8-sig", errors="replace")
                else:
                    content = await page.content()
                    raw = content.encode()
                    content_type = "text/html; rendered=1"
                if len(raw) > policy.max_bytes:
                    raise _failure("LIMIT_EXCEEDED", "Browser response byte limit exceeded")
                if profile is not None and profile.merge(await context.storage_state()):
                    profile.changed = True
                return {"url": page.url, "content": content, "raw": raw,
                        "content_type": content_type,
                        "http_status": response.status if response else None, "headers": {}}
            finally:
                try:
                    await browser.close()
                except PWError:
                    pass
    except PWTimeout:
        raise _failure("TIMEOUT", service + " navigation timed out") from None
    except PWError as error:
        # The endpoint carries the key; never repeat Playwright's message.
        from .runtime import _navigation_failure
        code = _navigation_failure(str(error))
        if code:
            raise _failure(code, "Target refused the page", response_url=url) from None
        raise _failure("PROVIDER_DOWN", service + " browser connection failed") from None


async def _session_call(method, url, policy, service, *, headers, body=None):
    status, _, raw = await _call(method, url, policy, headers=headers, json_body=body)
    error = _service_status(status, service)
    if error:
        raise error
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        raise _failure("SCHEMA_CHANGED", service + " returned an unexpected body") from None


class BrowserbaseProvider(_ManagedBrowser):
    """Browserbase sessions (managed Chromium with stealth and session replay)."""
    service = "Browserbase"
    required_settings = ("browserbase_api_key",)
    usd_per_hour = 0.12
    API = "https://api.browserbase.com/v1/sessions"

    def __init__(self):
        super().__init__("browserbase")

    async def open_session(self, policy):
        key = self._required("browserbase_api_key")
        headers = {"X-BB-API-Key": key, "Content-Type": "application/json"}
        body = {}
        project = _setting("browserbase_project_id")
        if project:
            body["projectId"] = project
        session = await _session_call("POST", self.API, policy, self.service,
                                      headers=headers, body=body)
        try:
            session_id, endpoint = session["id"], session["connectUrl"]
        except (KeyError, TypeError):
            raise _failure("SCHEMA_CHANGED", "Browserbase returned an unexpected session") from None

        async def release():
            await _call("POST", self.API + "/" + quote(session_id, safe=""), policy,
                        headers=headers, json_body={**body, "status": "REQUEST_RELEASE"})
        return endpoint, release


class SteelCloudProvider(_ManagedBrowser):
    """Steel Cloud sessions. ``steel_cloud_url`` points it at any Steel API."""
    service = "Steel"
    required_settings = ("steel_api_key",)
    usd_per_hour = 0.10
    API = "https://api.steel.dev"
    CONNECT = "wss://connect.steel.dev"

    def __init__(self):
        super().__init__("steel_cloud")

    async def open_session(self, policy):
        key = self._required("steel_api_key")
        api = (_setting("steel_cloud_url") or self.API).rstrip("/")
        headers = {"steel-api-key": key, "Content-Type": "application/json"}
        session = await _session_call("POST", api + "/v1/sessions", policy, self.service,
                                      headers=headers, body={"blockAds": True})
        try:
            session_id = session["id"]
        except (KeyError, TypeError):
            raise _failure("SCHEMA_CHANGED", "Steel returned an unexpected session") from None
        custom = _setting("steel_cloud_url")
        connect = (custom.replace("http://", "ws://").replace("https://", "wss://").rstrip("/") + "/"
                   if custom else self.CONNECT)
        endpoint = session.get("websocketUrl") or (connect + "?sessionId=" + quote(session_id, safe=""))
        if "apiKey=" not in endpoint and endpoint.startswith("wss://connect.steel.dev"):
            endpoint += ("&" if "?" in endpoint else "?") + "apiKey=" + quote(key, safe="")

        async def release():
            await _call("POST", api + "/v1/sessions/" + quote(session_id, safe="") + "/release",
                        policy, headers=headers)
        return endpoint, release


class HyperbrowserProvider(_ManagedBrowser):
    """Hyperbrowser sessions (concurrency-first managed browsers)."""
    service = "Hyperbrowser"
    required_settings = ("hyperbrowser_api_key",)
    usd_per_hour = 0.10
    API = "https://api.hyperbrowser.ai/api/session"

    def __init__(self):
        super().__init__("hyperbrowser")

    async def open_session(self, policy):
        headers = {"x-api-key": self._required("hyperbrowser_api_key"),
                   "Content-Type": "application/json"}
        session = await _session_call("POST", self.API, policy, self.service,
                                      headers=headers, body={})
        try:
            session_id, endpoint = session["id"], session["wsEndpoint"]
        except (KeyError, TypeError):
            raise _failure("SCHEMA_CHANGED", "Hyperbrowser returned an unexpected session") from None

        async def release():
            await _call("PUT", self.API + "/" + quote(session_id, safe="") + "/stop",
                        policy, headers=headers)
        return endpoint, release


class BrowserlessProvider(_ManagedBrowser):
    """Browserless hosted Chromium. The connection itself is the session."""
    service = "Browserless"
    required_settings = ("browserless_api_key",)
    # Browserless bills units of up to 30 seconds; this is the per-hour
    # equivalent of the Prototyping plan's overage unit price.
    usd_per_hour = 0.24
    billing_minimum_seconds = 30
    CONNECT = "wss://production-sfo.browserless.io"

    def __init__(self):
        super().__init__("browserless")

    async def open_session(self, policy):
        base = (_setting("browserless_url") or self.CONNECT).rstrip("/")
        endpoint = base + "?token=" + quote(self._required("browserless_api_key"), safe="")

        async def release():
            return None
        return endpoint, release


class KernelProvider(_ManagedBrowser):
    """Kernel browsers (fast cold starts, optional stealth mode)."""
    service = "Kernel"
    required_settings = ("kernel_api_key",)
    usd_per_hour = 0.10
    API = "https://api.onkernel.com/browsers"

    def __init__(self):
        super().__init__("kernel")

    async def open_session(self, policy):
        headers = {"Authorization": "Bearer " + self._required("kernel_api_key"),
                   "Content-Type": "application/json"}
        session = await _session_call("POST", self.API, policy, self.service, headers=headers,
                                      body={"headless": True, "stealth": True,
                                            "timeout_seconds": max(60, int(policy.timeout_seconds) + 30)})
        try:
            session_id, endpoint = session["session_id"], session["cdp_ws_url"]
        except (KeyError, TypeError):
            raise _failure("SCHEMA_CHANGED", "Kernel returned an unexpected session") from None

        async def release():
            await _call("DELETE", self.API + "/" + quote(session_id, safe=""), policy, headers=headers)
        return endpoint, release


class AnchorProvider(_ManagedBrowser):
    """Anchor Browser sessions (managed auth, hardened Chromium)."""
    service = "Anchor"
    required_settings = ("anchor_api_key",)
    usd_per_hour = 0.10
    API = "https://api.anchorbrowser.io/v1/sessions"

    def __init__(self):
        super().__init__("anchor")

    async def open_session(self, policy):
        headers = {"anchor-api-key": self._required("anchor_api_key"),
                   "Content-Type": "application/json"}
        session = await _session_call("POST", self.API, policy, self.service, headers=headers, body={})
        try:
            data = session["data"]
            session_id, endpoint = data["id"], data["cdp_url"]
        except (KeyError, TypeError):
            raise _failure("SCHEMA_CHANGED", "Anchor returned an unexpected session") from None

        async def release():
            await _call("DELETE", self.API + "/" + quote(session_id, safe=""), policy, headers=headers)
        return endpoint, release


MANAGED_BROWSERS = (BrowserbaseProvider, SteelCloudProvider, HyperbrowserProvider,
                    BrowserlessProvider, KernelProvider, AnchorProvider)

