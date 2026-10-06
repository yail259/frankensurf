from types import SimpleNamespace
import pytest
from frankensurf import WebPolicy
from frankensurf.public_entry import navigate_entry,EntryFailure,CamoufoxEntryProvider
from frankensurf.runtime import WebFailure

class Page:
    def __init__(self,status=403,redirect=None):self.status=status;self.redirect=redirect;self.events=[]
    async def goto(self,url,**kwargs):
        self.url=self.redirect or url;self.events.append(("goto",url,kwargs));return SimpleNamespace(status=self.status)
    async def wait_for_timeout(self,value):self.events.append(("settle",value))

def request(**kw):return dict(url="https://example.com/item",public_entry_url="https://example.com/",timeout_seconds=40,settle_ms=8000,terminal_failures=("AUTH_REQUIRED",),public_entry_continue_failures=("BLOCKED",),**kw)
def status(code):return {403:"BLOCKED",401:"AUTH_REQUIRED"}.get(code)

async def test_entry_uses_caller_readiness_and_budget_before_target():
    page=Page();stages=[]
    await navigate_entry(page,request(),status,stages.append)
    assert page.events==[("goto","https://example.com/",{"wait_until":"domcontentloaded","timeout":40000}),("settle",8000)]
    assert stages==["public_entry_navigation","public_entry_settle"]

@pytest.mark.parametrize("code,allowed,expected",[(403,(),"BLOCKED"),(401,("AUTH_REQUIRED",),"AUTH_REQUIRED")])
async def test_entry_failure_policy_and_terminal_authority(code,allowed,expected):
    req=request();req["public_entry_continue_failures"]=allowed
    with pytest.raises(EntryFailure) as e:await navigate_entry(Page(code),req,status,lambda _:None)
    assert e.value.code==expected

async def test_cross_origin_redirect_rejected():
    with pytest.raises(EntryFailure) as e:await navigate_entry(Page(200,"https://other.com/"),request(),status,lambda _:None)
    assert e.value.code=="POLICY_DENIED"

@pytest.mark.parametrize("url",["http://example.com/","https://user:password@example.com/","https://example.com/#fragment"])
def test_invalid_entry_policy(url):
    with pytest.raises(ValueError):WebPolicy(public_entry_url=url)

async def test_plugin_forwards_explicit_policy_and_stays_opt_in():
    calls=[]
    class Services:
        async def isolated(self,url,policy,provider):calls.append((url,policy,provider));return {"ok":True}
    policy=WebPolicy(public_entry_url="https://example.com/",settle_ms=8000,timeout_seconds=80)
    result=await CamoufoxEntryProvider().acquire(SimpleNamespace(url="https://example.com/item",policy=policy),Services())
    assert result=={"ok":True} and calls[0][2]=="camoufox"
    assert calls[0][1].public_entry_url==policy.public_entry_url and calls[0][1].timeout_seconds==80
    from frankensurf.experimental import installed
    provider = CamoufoxEntryProvider()
    assert provider.manifest.route_scope_required is True
    assert provider.available({}) is installed(provider.manifest.id)

@pytest.mark.parametrize("policy",[WebPolicy(),WebPolicy(public_entry_url="https://other.com/"),WebPolicy(identity="owner",public_entry_url="https://example.com/")])
async def test_plugin_rejects_missing_entry_cross_origin_or_identity(policy):
    with pytest.raises(WebFailure):await CamoufoxEntryProvider().acquire(SimpleNamespace(url="https://example.com/item",policy=policy),None)


