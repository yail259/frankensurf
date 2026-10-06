"""Core strategy for observing one owner-opened visible page without navigation.

The strategy does not invoke page-authored handlers or arbitrary application
JavaScript. It reads the rendered DOM through browser automation while the
browser's own activity remains outside its control. Only selected visible listing
regions are projected; raw HTML/application state is never read or exported.
"""
from __future__ import annotations

import base64
import hashlib
from html import escape
import json
from urllib.parse import urljoin, urlparse

from .identity import IdentityFailure
from .plugin_catalog import (
    _implementation_identity,
    _materialize_methods,
    _method_templates,
)


_CAPTURE_BINDING_SCHEMA = b"frankensurf.core-capture-strategy/v2\0"


class OwnerVisibleSnapshot:
    id = "owner_visible_snapshot"
    version = "2"

    def _page(self, context, resolved, url, registry):
        from .runtime import WebFailure
        registry.recheck(resolved)
        prefixes = resolved.snapshot_policy["path_prefixes"]
        permitted = registry.permits_url(resolved, url)
        if (not permitted
                or not any(urlparse(url).path.startswith(prefix)
                           for prefix in prefixes)):
            raise WebFailure(
                "IDENTITY_POLICY_DENIED",
                "Visible snapshot URL is outside the operator's page scope")
        pages = [page for page in context.pages
                 if not page.is_closed() and page.url == url]
        if len(pages) != 1:
            raise WebFailure(
                "IDENTITY_OPERATOR_HANDOFF_REQUIRED",
                "Open exactly one matching listing page in the enrolled local browser")
        return pages[0]

    @staticmethod
    async def _safe_region(page, root):
        """Never capture while a password field is on screen."""
        from .runtime import WebFailure
        for field in await page.locator("input[type='password']").all():
            if await field.is_visible():
                raise WebFailure(
                    "IDENTITY_POLICY_DENIED",
                    "Close authentication controls before a listing snapshot")

    @staticmethod
    def _safe_url(value, base):
        """Return a redacted export URL and the opaque executor-only URL."""
        if not value:
            return None
        parsed = urlparse(urljoin(base, value))
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password):
            return None
        from .runtime import _strip_credential_query
        exported = _strip_credential_query(parsed.geturl())
        return exported, parsed.geturl()

    @staticmethod
    async def _layout(context, page):
        session = await context.new_cdp_session(page)
        try:
            return session, await session.send("Page.getLayoutMetrics")
        except BaseException:
            await session.detach()
            raise

    @staticmethod
    def _viewport(metrics):
        viewport = (metrics.get("cssVisualViewport")
                    or metrics.get("visualViewport") or {})
        return {
            "x": viewport.get("pageX", 0),
            "y": viewport.get("pageY", 0),
            "width": viewport.get("clientWidth"),
            "height": viewport.get("clientHeight"),
        }

    async def _visible_projection(
            self, page, root, url, resolved, registry, policy):
        """Read one deterministic projection for before/after comparison."""
        text = await root.inner_text(
            timeout=policy.timeout_seconds * 1000)
        heading = root.locator("h1").first
        title = (await heading.inner_text()
                 if await heading.count() and await heading.is_visible()
                 else "Owner-visible listing region")

        images = []
        seen_images = set()
        private_images = {}
        for image in await root.locator("img").all():
            if not await image.is_visible():
                continue
            source = self._safe_url(
                await image.get_attribute("src"), url)
            if source is None:
                continue
            exported, executor_url = source
            try:
                registry.permits_url(
                    resolved, executor_url, image=True)
            except IdentityFailure:
                continue
            if exported not in seen_images:
                seen_images.add(exported)
                images.append(exported)
                if executor_url != exported:
                    private_images[exported] = executor_url

        links = []
        seen_links = set()
        for anchor in await root.locator("a[href]").all():
            if not await anchor.is_visible():
                continue
            destination = self._safe_url(
                await anchor.get_attribute("href"), url)
            if destination is None:
                continue
            exported, executor_url = destination
            try:
                registry.permits_url(resolved, executor_url)
            except IdentityFailure:
                continue
            label = " ".join((await anchor.inner_text()).split())
            item = (exported, label)
            if item not in seen_links:
                seen_links.add(item)
                links.append(item)

        return {
            "title": title,
            "text": text,
            "images": tuple(images),
            "private_images": tuple(private_images.items()),
            "links": tuple(links),
        }

    async def health(self, context, resolved, policy, registry):
        from .runtime import WebFailure
        check = resolved.auth_check
        if not check:
            return "not_configured"
        page = self._page(context, resolved, check["url"], registry)
        pages = tuple(context.pages)
        url = page.url
        if (check.get("login_selector")
                and await page.locator(
                    check["login_selector"]).first.is_visible()):
            raise WebFailure(
                "IDENTITY_REAUTH_REQUIRED",
                "Complete authentication manually in the enrolled browser")
        if not await page.locator(
                check["authenticated_selector"]).first.is_visible():
            raise WebFailure(
                "IDENTITY_REAUTH_REQUIRED",
                "A positive visible authentication signal is missing")
        if page.url != url or tuple(context.pages) != pages:
            raise WebFailure(
                "IDENTITY_CHANGED",
                "The owner browser changed during authentication observation")
        registry.recheck(resolved)
        return "verified_visible_signal"

    async def acquire(self, url, policy, resolved, context, registry):
        from .runtime import WebFailure
        page = self._page(context, resolved, url, registry)
        original_pages = tuple(context.pages)
        original_url = page.url
        root = None
        for selector in resolved.snapshot_policy["root_selectors"]:
            for candidate in await page.locator(selector).all():
                if await candidate.is_visible():
                    root = candidate
                    break
            if root is not None:
                break
        if root is None:
            raise WebFailure(
                "IDENTITY_OPERATOR_HANDOFF_REQUIRED",
                "The configured visible listing region is missing")

        await self._safe_region(page, root)
        session, initial_metrics = await self._layout(context, page)
        initial_viewport = self._viewport(initial_metrics)
        try:
            projection = await self._visible_projection(
                page, root, url, resolved, registry, policy)

            await self._safe_region(page, root)
            current_metrics = await session.send("Page.getLayoutMetrics")
            current_viewport = self._viewport(current_metrics)
            bounds = await root.bounding_box()
            if (not bounds or not current_viewport["width"]
                    or not current_viewport["height"]):
                raise WebFailure(
                    "IDENTITY_OPERATOR_HANDOFF_REQUIRED",
                    "The listing region has no visible viewport bounds")
            left = max(0, bounds["x"])
            top = max(0, bounds["y"])
            right = min(current_viewport["width"],
                        bounds["x"] + bounds["width"])
            bottom = min(current_viewport["height"],
                         bounds["y"] + bounds["height"])
            if right <= left or bottom <= top:
                raise WebFailure(
                    "IDENTITY_OPERATOR_HANDOFF_REQUIRED",
                    "Scroll the listing region into view manually")
            screenshot_result = await session.send(
                "Page.captureScreenshot", {
                    "format": "png",
                    "captureBeyondViewport": False,
                    "fromSurface": True,
                    "clip": {
                        "x": current_viewport["x"] + left,
                        "y": current_viewport["y"] + top,
                        "width": right - left,
                        "height": bottom - top,
                        "scale": 1,
                    },
                })
            screenshot = base64.b64decode(
                screenshot_result["data"], validate=True)
            final_metrics = await session.send("Page.getLayoutMetrics")
        finally:
            await session.detach()

        await self._safe_region(page, root)
        final_viewport = self._viewport(final_metrics)
        confirmed_projection = await self._visible_projection(
            page, root, url, resolved, registry, policy)
        final_bounds = await root.bounding_box()
        if page.url != original_url or confirmed_projection != projection:
            raise WebFailure(
                "IDENTITY_CHANGED",
                "The owner page changed during visible-region observation")
        registry.recheck(resolved)

        title = projection["title"]
        text = projection["text"]
        images = projection["images"]
        private_images = dict(projection["private_images"])
        links = projection["links"]

        content = (
            "<html><head><title>" + escape(title) +
            "</title></head><body><main><h1>" + escape(title) +
            "</h1><pre>" + escape(text) + "</pre>")
        content += "".join(
            '<img src="' + escape(source, quote=True) + '">'
            for source in images)
        content += "".join(
            '<a href="' + escape(destination, quote=True) + '">' +
            escape(label) + "</a>"
            for destination, label in links)
        content += "</main></body></html>"
        raw = content.encode()
        if len(raw) > policy.max_bytes:
            raise WebFailure(
                "LIMIT_EXCEEDED",
                "Visible projection exceeds the requested byte budget")
        return {
            "url": url,
            "content": content,
            "raw": raw,
            "content_type": "text/html",
            "http_status": None,
            "headers": {},
            "snapshot_screenshot_bytes": screenshot,
            # This opaque map is consumed only by Core while the lease is held.
            "identity_private_image_sources": private_images,
            "visible_snapshot": {
                "scope": "owner_opened_visible_region",
                "navigation_performed": False,
                "source_refresh_performed": False,
                "source_freshness": "unknown",
                "gallery_coverage": "partial_unverified_candidates",
            },
        }


_OWNER_VISIBLE_CAPTURE_ID = OwnerVisibleSnapshot.id
_OWNER_VISIBLE_CAPTURE_VERSION = OwnerVisibleSnapshot.version
_OWNER_VISIBLE_CAPTURE_METHODS = _method_templates(OwnerVisibleSnapshot)


def _materialize_owner_visible_capture_strategy(
        methods=_OWNER_VISIBLE_CAPTURE_METHODS,
        materialize=_materialize_methods):
    """Create an execution object with independent functions and globals."""
    namespace = {
        "__module__": __name__,
        "__slots__": (),
        "id": _OWNER_VISIBLE_CAPTURE_ID,
        "version": _OWNER_VISIBLE_CAPTURE_VERSION,
        **materialize(methods, OwnerVisibleSnapshot),
    }
    pinned_type = type(
        "_RuntimeOwnerVisibleSnapshot", (object,), namespace)
    return pinned_type()


def _owner_visible_capture_binding(strategy,
                                   implementation=_implementation_identity):
    payload = {
        "id": strategy.id,
        "version": strategy.version,
        "implementation": implementation(strategy),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode()
    return hashlib.sha256(_CAPTURE_BINDING_SCHEMA + encoded).hexdigest()


_CANONICAL_OWNER_VISIBLE_CAPTURE = (
    _materialize_owner_visible_capture_strategy())
_OWNER_VISIBLE_CAPTURE_BINDING_ID = _owner_visible_capture_binding(
    _CANONICAL_OWNER_VISIBLE_CAPTURE)
del _CANONICAL_OWNER_VISIBLE_CAPTURE


def owner_visible_capture_binding_id(
        strategy=None, binding=_owner_visible_capture_binding,
        canonical=_OWNER_VISIBLE_CAPTURE_BINDING_ID):
    """Bind the exact frozen executable strategy and configuration."""
    return canonical if strategy is None else binding(strategy)


def bind_owner_visible_capture_strategy(
        materialize=_materialize_owner_visible_capture_strategy,
        binding=_owner_visible_capture_binding,
        expected=_OWNER_VISIBLE_CAPTURE_BINDING_ID):
    """Pin independent Core strategy code and configuration for one Runtime."""
    strategy = materialize()
    if binding(strategy) != expected:
        raise ValueError("Core capture strategy template changed")
    return strategy
