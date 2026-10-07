import os
from mcp.server.fastmcp import FastMCP
from .runtime import Runtime, WebPolicy

server = FastMCP("FrankenSurf")


def runtime():
    return Runtime(state_dir=os.getenv("FRANKENSURF_STATE", "state"),
                   identity_registry=os.getenv("FRANKENSURF_IDENTITIES"))


def _acquisition_overrides(acquisition_policy, named):
    # JSON key presence is the authority record: False, None and 0 are supplied.
    if acquisition_policy is not None and not isinstance(acquisition_policy, dict):
        raise ValueError("acquisition_policy must be an object")
    options = dict(acquisition_policy or {})
    if set(options) & set(named):
        raise ValueError("Do not duplicate explicit tool arguments in acquisition_policy")
    options.update({key: value for key, value in named.items() if value is not None})
    return options


@server.tool()
async def read(url: str, provider: str | None = None, render: bool | None = None,
               include_images: bool | None = None, freshness: str | None = None,
               identity: str | None = None, allow_handoff: bool | None = None,
               profile: str | None = None, try_harder_than: str | None = None,
               expect_terms: list[str] | None = None, card_images: bool | None = None,
               acquisition_policy: dict | None = None) -> dict:
    """Retrieve evidence. Omitted settings use runtime defaults and operator public routes.

    Direct null arguments mean omitted; acquisition_policy preserves explicit null
    operational settings. Content does not certify live or sold state.
    allow_handoff=True lets a person clear a CAPTCHA, sign-in or 2FA wall in a
    visible browser when every automatic route fails; the call waits for them.
    profile names a stored FrankenSurf login (see the profiles tool); the session
    itself never reaches the agent.
    try_harder_than takes the trace_id of an earlier read whose page wasn't what
    you needed: this read skips every tool that one tried and starts from the
    strongest remaining tool. receipt.if_not_right says what is left to try.
    expect_terms are words a search page's results should mention besides the
    URL's own query; results that mention none come back flagged
    completeness.off_query, with receipt.next_step, instead of escalating.
    card_images=True adds cards: each result link with its title and its own
    thumbnail URL, from the same page load.
    """
    options = _acquisition_overrides(acquisition_policy, {"provider": provider,
        "render": render, "include_images": include_images, "freshness": freshness,
        "identity": identity, "allow_handoff": allow_handoff, "profile": profile,
        "expect_terms": tuple(expect_terms) if expect_terms else None,
        "card_images": card_images})
    # Agents read text: ask servers for markdown first (T0). Callers can pass
    # acquisition_policy={"prefer_markdown": false} for raw HTML structure.
    options.setdefault("prefer_markdown", True)
    async with runtime() as web:
        result = await web.read(url, policy_overrides=options,
                                **({"retry_of": try_harder_than} if try_harder_than else {}))
        result.pop("content", None)
        return result


@server.tool()
async def extract(url: str, adapter: str | None = None, include_images: bool | None = None,
                  identity: str | None = None, provider: str | None = None,
                  freshness: str | None = None, acquisition_policy: dict | None = None,
                  workload_assertions: dict | None = None) -> dict:
    """Extract requested evidence with identity checks and operator public routes.

    Direct null arguments mean omitted; acquisition_policy preserves explicit null,
    false and zero operational settings. Omitting the adapter uses HTML.
    """
    options = _acquisition_overrides(acquisition_policy, {"identity": identity,
        "provider": provider, "freshness": freshness, "include_images": include_images})
    async with runtime() as web:
        kwargs = ({"workload_assertions": workload_assertions}
                  if workload_assertions is not None else {})
        result = await web.extract(
            url, adapter if adapter is not None else "html",
            policy_overrides=options, **kwargs)
        result.pop("content", None)
        return result


@server.tool()
async def do(intent: dict, identity: str, action_classes: list[str],
             provider: str | None = None,
             action_policy: dict | None = None) -> dict:
    """Execute one contract-bound authenticated local action with a typed receipt.

    ``action_classes`` is an explicit operation-policy grant separate from the
    class declared by the intent and the enrolled identity grant. Raw browser
    control requires the full four-class mutation ceiling, explicitly named
    provider/contract/origins and an effective tool set exactly matching the
    intent; its semantic result stays unknown.
    """
    if (not isinstance(action_classes, list) or not action_classes
            or any(not isinstance(value, str) for value in action_classes)):
        raise ValueError("action_classes must be a nonempty list")
    options = _acquisition_overrides(action_policy, {
        "identity": identity, "provider": provider,
        "action_classes": tuple(action_classes)})
    for field in ("provider_candidates", "browser_do_allowed_origins",
                  "browser_do_allowed_tools",
                  "browser_do_allowed_contracts"):
        if isinstance(options.get(field), list):
            options[field] = tuple(options[field])
    async with runtime() as web:
        return await web.do(intent, WebPolicy(**options))


@server.tool()
async def repair(trace_id: str, repair_policy: dict | None = None) -> dict:
    """Diagnose one retained public read failure and validate an inert proposal.

    The operation never uses named identity authority and never promotes its
    output. Owner promotion remains an explicit local operator API/CLI action.
    """
    from .repair import RepairPolicy
    options = dict(repair_policy or {})
    for field in (
            "allowed_failure_codes", "allowed_attributes",
            "allowed_route_providers"):
        if isinstance(options.get(field), list):
            options[field] = tuple(options[field])
    async with runtime() as web:
        return await web.repair(trace_id, RepairPolicy(**options))


@server.tool()
async def paginate(url: str, adapter: str,
                   continuation_adapter: str | None = None,
                   identity: str | None = None, provider: str | None = None,
                   freshness: str | None = None, max_pages: int | None = None,
                   acquisition_policy: dict | None = None) -> dict:
    """Traverse pages with the requested leaf schemas and one aggregate receipt.

    Omitted settings permit version-bound bundled or operator routes. Direct null
    arguments mean omitted; acquisition_policy preserves explicit null, false and
    zero operational settings. Public read authority is unchanged.
    """
    options = _acquisition_overrides(acquisition_policy, {"identity": identity,
        "provider": provider, "freshness": freshness, "max_pages": max_pages})
    async with runtime() as web:
        result = await web.paginate(url, adapter,
            continuation_adapter=continuation_adapter, policy_overrides=options)
        result.pop("content", None)
        for page in result.get("pages", []):
            if isinstance(page, dict):
                page.pop("content", None)
        return result


@server.tool()
async def batch(urls: list[str], provider: str | None = None, adapter: str | None = None,
                identity: str | None = None, include_images: bool = False,
                freshness: str = "now") -> list[dict]:
    """Bounded batch retrieval with per-result receipts and named identity policy enforcement."""
    async with runtime() as web:
        results = await web.batch(urls, WebPolicy(provider=provider, identity=identity,
            include_images=include_images, freshness=freshness), adapter=adapter)
        for result in results:
            result.pop("content", None)
        return results


@server.tool()
async def search(query: str, source: str | None = None, limit: int = 10,
                 identity: str | None = None, provider: str | None = None,
                 acquisition_policy: dict | None = None) -> dict:
    """Discover indexed candidates through installed sources with exact attribution.

    Omit source to permit policy-bounded fallback. An explicit source is never
    substituted. Search source candidate, allow and preference fields can be set
    in acquisition_policy without exposing service credentials.
    """
    config = ({"base_url": os.environ["FRANKENSURF_SEARCH_URL"]}
              if os.getenv("FRANKENSURF_SEARCH_URL") and source in (None, "searxng") else None)
    options = _acquisition_overrides(acquisition_policy,
        {"identity": identity, "provider": provider})
    for field in ("provider_candidates", "search_source_candidates", "search_source_allow",
                  "search_source_prefer", "search_terminal_failures"):
        if isinstance(options.get(field), list):
            options[field] = tuple(options[field])
    if ("provider" not in options and "provider_candidates" not in options
            and not options.get("identity")):
        options["provider"] = "http"
    async with runtime() as web:
        return await web.search(query, source=source, limit=limit, engine_config=config,
                                policy=WebPolicy(**options))


@server.tool()
async def images(urls: list[str], identity: str | None = None,
                 max_images: int = 50, provider: str | None = None) -> list[dict]:
    """Download and decode exact image URLs under public or named identity policy; return private evidence references."""
    async with runtime() as web:
        return await web.download_images(urls, WebPolicy(identity=identity,
            max_images=max_images, provider=provider))


@server.tool()
async def identity_status(identity: str | None = None) -> dict:
    """Inspect safe identity/executor health and authority metadata. Enrollment is operator CLI only."""
    from .identity import IdentityFailure
    try:
        return runtime().identity_status(identity)
    except IdentityFailure as exc:
        return {"status": "failed", "failure": {"code": exc.code, "message": exc.message}}


@server.tool()
async def import_evidence(content: str, url: str, observed_at: str, content_type: str = "text/html", image_urls: list[str] | None = None) -> dict:
    """Import actual supplied operator browser evidence. Does not verify automated access or live availability."""
    async with runtime() as web:
        result = web.import_evidence(content, url, observed_at, content_type=content_type, image_urls=image_urls)
        result.pop("content", None)
        return result


@server.tool()
async def import_image_evidence(path: str, url: str, observed_at: str,
                                max_image_bytes: int = 20 * 1024 * 1024) -> dict:
    """Import a caller-selected public browser image file. Source binding is operator-supplied, never identity attestation."""
    from pathlib import Path
    from .runtime import WebFailure
    try:
        policy = WebPolicy(max_image_bytes=max_image_bytes)
        with Path(path).expanduser().open("rb") as source:
            raw = source.read(policy.max_image_bytes + 1)
        async with runtime() as web:
            return web.import_image_evidence(raw, url, observed_at, policy)
    except WebFailure as exc:
        return {"status": "failed", "failure": {"code": exc.code, "message": exc.message}}
    except (OSError, ValueError):
        return {"status": "failed", "failure": {"code": "INVALID_EVIDENCE", "message": "Image file or observation metadata is invalid"}}


@server.tool()
async def trace(trace_id: str) -> dict:
    """The full record of one earlier read: every tool tried, failures, timings,
    completeness steps and evidence references. Page content is not included."""
    async with runtime() as web:
        result = web.trace(trace_id)
    result.pop("content", None)
    return result


@server.tool()
async def profiles() -> list[dict]:
    """List stored FrankenSurf profiles: names, sites, sharing level. Never sessions."""
    from .profiles import ProfileStore
    return ProfileStore().list()


def main():
    server.run()
