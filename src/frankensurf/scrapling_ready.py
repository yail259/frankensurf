"""Explicit anonymous Scrapling provider with configurable navigation readiness."""
from dataclasses import replace

async def configure_navigation_readiness(page, mode, report=None):
    if mode not in ("load", "domcontentloaded", "networkidle", "commit"):
        raise ValueError("Unsupported navigation readiness")
    original_goto = page.goto
    original_wait = page.wait_for_load_state
    async def goto(url, **kwargs):
        kwargs["wait_until"] = mode
        if report: report("navigation_readiness")
        result = await original_goto(url, **kwargs)
        if report: report("response_capture")
        return result
    async def wait(state=None, **kwargs):
        if mode == "commit" and state in (None, "load", "domcontentloaded"):
            if report: report("response_capture")
            return None
        if report: report("navigation_readiness")
        result = await original_wait(mode if state in (None, "load") else state, **kwargs)
        if report: report("response_capture")
        return result
    page.goto = goto
    page.wait_for_load_state = wait

class ScraplingReadyProvider:
    def __init__(self):
        from .providers import ProviderManifest
        self.manifest = ProviderManifest("scrapling_ready", "1", rendering=True, requires_local_browser=True, route_scope_required=True)
    def available(self, configured):
        from .experimental import installed
        return installed(self.manifest.id)
    async def acquire(self, request, services):
        mode = request.policy.scrapling_navigation_wait_until or "domcontentloaded"
        policy = replace(request.policy, scrapling_navigation_wait_until=mode)
        return await services.isolated(request.url, policy, "scrapling")
