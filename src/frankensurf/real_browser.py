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
each page opens in a fresh private window of a real browser you started yourself
(with remote debugging), which is closed afterwards: no cookies go in or out.

It never clicks or types. A challenge that clears by itself is waited out, up to
``real_browser_wait_seconds``. When the call also allows a handoff, that same
window is where a person clears a wall that does not clear by itself, and the
profile keeps what they earn for later reads. One page at a time per profile: a
second read while the profile is busy skips the real browser.
"""
from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

DEFAULT_PROFILE = Path("~/.local/share/frankensurf/real-browser-profile")
POLL_SECONDS = 1.0
# Profiles in use by this process. A plain set, not an asyncio.Lock: a busy
# profile is skipped, never waited for, so nothing binds to one event loop.
_BUSY: set[str] = set()


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


class _Busy(Exception):
    """Another read (in this process or another) has the profile."""


@contextmanager
def _claim(profile: Path):
    """Hold the profile for this read, across processes, or raise _Busy."""
    key = str(profile.resolve())
    if key in _BUSY:
        raise _Busy(key)
    profile.mkdir(parents=True, exist_ok=True)
    handle = open(profile / ".frankensurf-lock", "a+")
    try:
        try:
            try:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except ImportError:  # Windows: msvcrt locks a byte range instead.
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise _Busy(key) from None
        _BUSY.add(key)
        try:
            yield
        finally:
            _BUSY.discard(key)
    finally:
        handle.close()


async def _page_state(page):
    try:
        title = await page.title()
        text = await page.locator("body").inner_text(timeout=2000)
    except Exception:
        return None
    return title, text, page.url


def _wall_code(state, requested_url):
    """The wall the tab shows (CAPTCHA, BLOCKED, ...), or None once it shows the page.
    No state (the tab is between documents, as a clearing challenge reloads)
    counts as not yet clear."""
    from .runtime import _wall_after_parse
    if state is None:
        return "PROVIDER_DOWN"
    title, text, final_url = state
    return _wall_after_parse(title, text, requested_url, final_url)


class RealBrowserProvider:
    def __init__(self):
        from .providers import ProviderManifest
        # Route scope: never part of an ordinary read. A read joins it only when
        # the call (or FRANKENSURF_REAL_BROWSER) allows it, or names it. No
        # numbered navigation: it opens the page it is given and never clicks.
        self.manifest = ProviderManifest("real_browser", "1", rendering=True,
                                         requires_local_browser=True, navigation=False,
                                         route_scope_required=True)

    def available(self, configured):
        from .hosted_providers import _setting
        return bool(_setting("real_browser_cdp")) or (executable() is not None and _has_display())

    async def acquire(self, request, services):
        from .hosted_providers import _setting
        from .runtime import WebFailure
        cdp_url = _setting("real_browser_cdp")
        browser_path = executable()
        if not cdp_url and (browser_path is None or not _has_display()):
            raise WebFailure("PROVIDER_UNAVAILABLE",
                             "No real Chrome or Edge with a display, and no FRANKENSURF_REAL_BROWSER_CDP")
        if cdp_url:
            return await self._read(request, cdp_url=cdp_url)
        profile = Path(_setting("real_browser_profile") or DEFAULT_PROFILE).expanduser()
        try:
            with _claim(profile):
                return await self._read(request, profile=profile, browser_path=browser_path)
        except _Busy:
            raise WebFailure("PROVIDER_UNAVAILABLE",
                             "The real browser's profile is busy with another page") from None

    async def _read(self, request, *, cdp_url=None, profile=None, browser_path=None):
        from playwright.async_api import Error as PWError
        from playwright.async_api import TimeoutError as PWTimeout
        from playwright.async_api import async_playwright

        from .runtime import WebFailure, _navigation_failure, _rendered_html
        policy = request.policy
        # The clock starts once the profile is ours, so a queue never eats the wait.
        deadline = time.monotonic() + policy.timeout_seconds - 2
        try:
            async with async_playwright() as playwright:
                if cdp_url:
                    # A fresh private window in the attached browser: none of its
                    # cookies or sign-ins reach a public read.
                    browser = await playwright.chromium.connect_over_cdp(cdp_url)
                    owned = await browser.new_context()
                    page = await owned.new_page()
                else:
                    # A real browser as a person runs it: its own window and
                    # profile, without the automation switch.
                    owned = await playwright.chromium.launch_persistent_context(
                        str(profile), executable_path=str(browser_path), headless=False,
                        ignore_default_args=["--enable-automation"],
                        args=["--no-first-run", "--no-default-browser-check"])
                    page = owned.pages[0] if owned.pages else await owned.new_page()
                try:
                    # The latest main-frame document decides the status: a
                    # challenge served as 403 that clears itself ends in a 200.
                    latest = {}

                    def navigated(response):
                        try:
                            if response.request.is_navigation_request() and response.frame == page.main_frame:
                                latest["response"] = response
                        except Exception:
                            pass
                    page.on("response", navigated)
                    try:
                        await page.goto(request.url, wait_until="domcontentloaded",
                                        timeout=max(5, deadline - time.monotonic()) * 1000)
                    except PWError as error:
                        code = _navigation_failure(str(error))
                        if not code:
                            raise
                        raise WebFailure(code, "The page refused the real browser",
                                         response_url=request.url) from None
                    code = await self._wait_out(page, request.url,
                                                min(deadline, time.monotonic() + policy.real_browser_wait_seconds))
                    if code and not page.url.startswith("chrome-error://") and policy.allow_handoff:
                        # The call allows a person: this window is the handoff, and
                        # the profile keeps what they earn for later reads.
                        from .handoff import _notify
                        await page.bring_to_front()
                        _notify(request.url)
                        code = await self._wait_out(page, request.url, deadline)
                        if code:
                            raise WebFailure("TIMEOUT", "Nobody cleared the wall in the real browser in time",
                                             response_url=page.url)
                    if code:
                        raise WebFailure(code, "The real browser met the wall too", response_url=page.url)
                    readiness = None
                    if policy.content_ready_selector is not None:
                        try:
                            await page.wait_for_selector(
                                policy.content_ready_selector, state="attached",
                                timeout=max(0.0, min(policy.content_ready_timeout_seconds,
                                                     deadline - time.monotonic() - 3)) * 1000)
                            status = "satisfied"
                        except PWTimeout:
                            status = "timed_out"
                        readiness = {"status": status, "timeout_seconds": policy.content_ready_timeout_seconds}
                    await page.wait_for_timeout(max(policy.settle_ms, 1500))
                    content = await _rendered_html(page)
                    raw = content.encode()
                    if len(raw) > policy.max_bytes:
                        raise WebFailure("LIMIT_EXCEEDED", "Browser response byte limit exceeded")
                    response = latest.get("response")
                    return {"url": page.url, "content": content, "raw": raw,
                            "content_type": "text/html; rendered=1",
                            "http_status": response.status if response else 200, "headers": {},
                            **({"content_readiness": readiness} if readiness is not None else {})}
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

    @staticmethod
    async def _wait_out(page, url, until):
        """Poll until the tab shows the page or `until` passes; return the wall left, or None.
        The browser's own error page is not waited on: nothing will clear it."""
        code = _wall_code(await _page_state(page), url)
        while code and not page.url.startswith("chrome-error://") and time.monotonic() < until:
            await page.wait_for_timeout(POLL_SECONDS * 1000)
            code = _wall_code(await _page_state(page), url)
        return code
