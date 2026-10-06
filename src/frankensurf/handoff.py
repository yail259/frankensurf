"""T5 human handoff: a person clears the wall, then the read resumes in place.

When a page needs a CAPTCHA, a 2FA code, a consent click or a sign-in, the
``handoff`` provider opens it in a visible browser and tells the owner. The
person does whatever the page asks; FrankenSurf only watches. Once the page
no longer shows a challenge or sign-in wall it captures that same tab and the
read continues with the usual content checks, receipt and evidence.

The browser is a persistent profile under
``~/.local/share/frankensurf/handoff-profile`` (``FRANKENSURF_HANDOFF_PROFILE``),
so cookies a person earns there (a solved challenge, a login) carry over to
later handoffs. With ``FRANKENSURF_HANDOFF_CDP_URL`` set, the page opens as a
new tab in that already-running browser instead, and only that tab is closed.

Core never clicks or types here: the person does. Handoff runs only when a
call asks for it (``provider="handoff"``, or ``allow_handoff=True`` to try it
after every automatic provider has failed). ``handoff_timeout_seconds`` bounds
the wait.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_PROFILE = Path("~/.local/share/frankensurf/handoff-profile")
POLL_SECONDS = 1.0


def _notify(url):
    message = ("FrankenSurf needs you: finish what the page asks in the browser window "
               "(challenge, sign-in, 2FA or consent). Reading resumes by itself: " + url)
    print(message, file=sys.stderr, flush=True)
    if shutil.which("notify-send"):
        try:
            subprocess.run(["notify-send", "FrankenSurf handoff", message],
                           timeout=5, check=False, capture_output=True)
        except (OSError, subprocess.SubprocessError):
            pass


def _has_display():
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


async def _page_state(page):
    try:
        title = await page.title()
        text = await page.locator("body").inner_text(timeout=2000)
    except Exception:
        return None
    return title, text, page.url


def cleared(state, requested_url, min_text_chars):
    """True when the tab shows the page itself rather than a wall."""
    from .runtime import _wall_after_parse
    if state is None:
        return False
    title, text, final_url = state
    return (len((text or "").strip()) >= min_text_chars
            and _wall_after_parse(title, text, requested_url, final_url) is None)


class HandoffProvider:
    def __init__(self):
        from .providers import ProviderManifest
        self.manifest = ProviderManifest("handoff", "1", rendering=True,
                                         requires_local_browser=True, navigation=True,
                                         route_scope_required=True)

    def available(self, configured):
        from .hosted_providers import _setting
        return bool(_setting("handoff_cdp_url")) or _has_display()

    async def acquire(self, request, services):
        from playwright.async_api import Error as PWError
        from playwright.async_api import TimeoutError as PWTimeout
        from playwright.async_api import async_playwright

        from .hosted_providers import _setting
        from .runtime import WebFailure, _navigation_failure
        policy = request.policy
        deadline = time.monotonic() + policy.timeout_seconds - 2
        cdp_url = _setting("handoff_cdp_url")
        if not cdp_url and not _has_display():
            raise WebFailure("PROVIDER_UNAVAILABLE", "Handoff needs a visible display or a CDP browser")
        try:
            async with async_playwright() as playwright:
                if cdp_url:
                    browser = await playwright.chromium.connect_over_cdp(cdp_url)
                    context = browser.contexts[0] if browser.contexts else await browser.new_context()
                    page = await context.new_page()
                    owned = page
                else:
                    profile = Path(_setting("handoff_profile") or DEFAULT_PROFILE).expanduser()
                    profile.mkdir(parents=True, exist_ok=True)
                    context = await playwright.chromium.launch_persistent_context(
                        str(profile), headless=False)
                    page = context.pages[0] if context.pages else await context.new_page()
                    owned = context
                try:
                    try:
                        response = await page.goto(request.url, wait_until="domcontentloaded",
                                                   timeout=min(policy.timeout_seconds, 60) * 1000)
                    except PWError as error:
                        # A blocked first response is exactly what a person can clear.
                        if not _navigation_failure(str(error)):
                            raise
                        response = None
                    await page.bring_to_front()
                    state = await _page_state(page)
                    if not cleared(state, request.url, policy.rendered_min_text_chars):
                        _notify(request.url)
                        while not cleared(state, request.url, policy.rendered_min_text_chars):
                            if time.monotonic() > deadline:
                                raise WebFailure("TIMEOUT", "Nobody completed the handoff in time",
                                                 response_url=page.url)
                            await page.wait_for_timeout(POLL_SECONDS * 1000)
                            state = await _page_state(page)
                        # Let the page finish whatever loads after the wall.
                        await page.wait_for_timeout(max(policy.settle_ms, 1500))
                        response = None
                    elif policy.settle_ms:
                        await page.wait_for_timeout(policy.settle_ms)
                    content = await page.content()
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
            raise WebFailure("TIMEOUT", "Handoff browser timed out") from None
        except PWError:
            raise WebFailure("PROVIDER_DOWN", "Handoff browser failed") from None
