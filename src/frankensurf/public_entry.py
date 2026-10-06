"""Opt-in ephemeral same-origin browser entry navigation."""
from dataclasses import replace
from urllib.parse import urlparse


def valid_entry(entry, target=None):
    try:
        p=urlparse(entry)
        valid=p.scheme=="https" and bool(p.hostname) and not p.username and not p.password and not p.fragment
        return bool(valid and (target is None or p.netloc==urlparse(target).netloc))
    except (TypeError,ValueError):
        return False


class EntryFailure(Exception):
    def __init__(self,code): self.code=code


async def navigate_entry(page,request,status_failure,report):
    entry=request["public_entry_url"]
    if not valid_entry(entry,request["url"]): raise EntryFailure("POLICY_DENIED")
    report("public_entry_navigation")
    response=await page.goto(entry,wait_until=request.get("scrapling_navigation_wait_until") or "domcontentloaded",timeout=request["timeout_seconds"]*1000)
    if not valid_entry(page.url,request["url"]): raise EntryFailure("POLICY_DENIED")
    if response is None: raise EntryFailure("PROVIDER_DOWN")
    failure=status_failure(response.status)
    if failure and (failure in request["terminal_failures"] or failure not in request["public_entry_continue_failures"]):
        raise EntryFailure(failure)
    report("public_entry_settle")
    if request["settle_ms"]: await page.wait_for_timeout(request["settle_ms"])


class CamoufoxEntryProvider:
    def __init__(self):
        from .providers import ProviderManifest
        self.manifest=ProviderManifest("camoufox_entry","1",rendering=True,requires_local_browser=True,navigation=True,route_scope_required=True)
    def available(self,configured):
        from .experimental import installed
        return installed(self.manifest.id)
    async def acquire(self,request,services):
        from .runtime import WebFailure
        if request.policy.identity: raise WebFailure("POLICY_DENIED","Public entry provider cannot execute named identities")
        entry=request.policy.public_entry_url
        if not valid_entry(entry,request.url): raise WebFailure("POLICY_DENIED","Public entry requires an explicit same-origin HTTPS entry URL")
        if request.policy.navigation_page != 1:
            raise WebFailure("POLICY_DENIED","Public entry provider uses direct target navigation")
        return await services.isolated(request.url,request.policy,"camoufox")
