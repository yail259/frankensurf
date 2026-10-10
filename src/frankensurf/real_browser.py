"""Your real browser as a fallback for walls (opt-in).

With ``allow_real_browser`` (or ``FRANKENSURF_REAL_BROWSER=1`` for every read), a
read that keeps meeting walls tries the Google Chrome installed on this machine,
or Microsoft Edge when there is no Chrome, in a visible window. A real browser's
own fingerprint gets past walls that recognise automated ones.

It uses FrankenSurf's own persistent profile under
``~/.local/share/frankensurf/real-browser-profile``
(``FRANKENSURF_REAL_BROWSER_PROFILE``), never your personal profile: your cookies
and sign-ins stay yours. Cookies the profile earns (a challenge it passed) carry
over to later reads, as in a person's browser. ``FRANKENSURF_REAL_BROWSER_PATH``
picks another Chromium-based browser. With ``FRANKENSURF_REAL_BROWSER_CDP`` set,
the page opens as a new tab in a real browser you started yourself (with remote
debugging and a profile of its own), and only that tab is closed.

It never clicks or types. A challenge that clears by itself is waited out, up to
``real_browser_wait_seconds``; one that needs a person fails as a wall, so a
handoff can follow.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

DEFAULT_PROFILE = Path("~/.local/share/frankensurf/real-browser-profile")
POLL_SECONDS = 1.0
# One page at a time: a persistent profile can be open in only one browser.
_LOCK = asyncio.Lock()


def _candidates():
    """Where Chrome, then Edge, install themselves on this system."""
    if sys.platform == "darwin":
        return [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")]
    if sys.platform == "win32":
        roots = [os.environ.get(name) for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        roots = [Path(root) for root in roots if root]
        return ([root / "Google/Chrome/Application/chrome.exe" for root in roots]
                + [root / "Microsoft/Edge/Application/msedge.exe" for root in roots])
    return [Path(path) for path in ("/opt/google/chrome/chrome", "/usr/bin/google-chrome-stable",
                                    "/usr/bin/google-chrome", "/opt/microsoft/msedge/msedge",
                                    "/usr/bin/microsoft-edge-stable", "/usr/bin/microsoft-edge")]


def executable():
    """The real browser to launch, or None."""
    from .hosted_providers import _setting
    configured = _setting("real_browser_path")
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_file() else None
    return next((path for path in _candidates() if path.is_file()), None)


def _has_display():
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def enabled_by_environment():
    from .hosted_providers import _setting
    return (_setting("real_browser") or "").lower() in ("1", "true", "yes")


async def _page_state(page):
    try:
        title = await page.title()
        text = await page.locator("body").inner_text(timeout=2000)
    except Exception:
        return None
    return title, text, page.url


class RealBrowserProvider:
    def __init__(self):
        from .providers import ProviderManifest
        # Route scope: never part of an ordinary read. A read joins it only when
        # the call (or FRANKENSURF_REAL_BROWSER) allows it, or names it.
        self.manifest = ProviderManifest("real_browser", "1", rendering=True,
                                         requires_local_browser=True, navigation=True,
                                         route_scope_required=True)

    def available(self, configured):
        from .hosted_providers import _setting
        return bool(_setting("real_browser_cdp")) or (executable() is not None and _has_display())

    async def acquire(self, request, services):
        from playwright.async_api import Error as PWError
        from playwright.async_api import TimeoutError as PWTimeout
        from playwright.async_api import async_playwright

        from .hosted_providers import _setting
        from .runtime import WebFailure, _navigation_failure, _rendered_html
        policy = request.policy
        cdp_url = _setting("real_browser_cdp")
        browser_path = executable()
        if not cdp_url and (browser_path is None or not _has_display()):
            raise WebFailure("PROVIDER_UNAVAILABLE",
                             "No real Chrome or Edge with a display, and no FRANKENSURF_REAL_BROWSER_CDP")
        deadline = time.monotonic() + policy.timeout_seconds - 2
        async with _LOCK:
            try:
                async with async_playwright() as playwright:
                    if cdp_url:
                        browser = await playwright.chromium.connect_over_cdp(cdp_url)
                        context = browser.contexts[0] if browser.contexts else await browser.new_context()
                        page = await context.new_page()
                        owned = page
                    else:
                        profile = Path(_setting("real_browser_profile") or DEFAULT_PROFILE).expanduser()
                        profile.mkdir(parents=True, exist_ok=True)
                        # A real browser as a person runs it: its own window and
                        # profile, without the automation switch.
                        context = await playwright.chromium.launch_persistent_context(
                            str(profile), executable_path=str(browser_path), headless=False,
                            ignore_default_args=["--enable-automation"],
                            args=["--no-first-run", "--no-default-browser-check"])
                        page = context.pages[0] if context.pages else await context.new_page()
                        owned = context
                    try:
                        try:
                            response = await page.goto(request.url, wait_until="domcontentloaded",
                                                       timeout=max(5, deadline - time.monotonic()) * 1000)
                        except PWError as error:
                            if not _navigation_failure(str(error)):
                                raise
                            response = None
                        # A challenge that clears by itself gets time; nothing is clicked.
                        wait_until = min(deadline, time.monotonic() + policy.real_browser_wait_seconds)
                        state = await _page_state(page)
                        code = _wall_code(state, request.url)
                        while code and time.monotonic() < wait_until:
                            await page.wait_for_timeout(POLL_SECONDS * 1000)
                            state = await _page_state(page)
                            code = _wall_code(state, request.url)
                        if code:
                            raise WebFailure(code, "The real browser met the wall too", response_url=page.url)
                        await page.wait_for_timeout(max(policy.settle_ms, 1500))
                        content = await _rendered_html(page)
                        raw = content.encode()
                        if len(raw) > policy.max_bytes:
                            raise WebFailure("LIMIT_EXCEEDED", "Browser response byte limit exceeded")
                        return {"url": page.url, "content": content, "raw": raw,
                                "content_type": "text/html; rendered=1",
                                "http_status": response.status if response else 200, "headers": {}}
                    finally:
                        try:
                            await owned.close()
                        except PWError:
                            pass
            except WebFailure:
                raise
            except PWTimeout:
                raise WebFailure("TIMEOUT", "The real browser timed out") from None
            except PWError:
                raise WebFailure("PROVIDER_DOWN", "The real browser failed") from None


def _wall_code(state, requested_url):
    """The wall the tab shows (CAPTCHA, BLOCKED, ...), or None once it shows the page."""
    from .runtime import _wall_after_parse
    if state is None:
        return "PROVIDER_DOWN"
    title, text, final_url = state
    return _wall_after_parse(title, text, requested_url, final_url)
