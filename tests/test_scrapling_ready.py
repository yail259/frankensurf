import pytest
from frankensurf import WebPolicy
from frankensurf.providers import ProviderRequest, ProviderServices, DEFAULT_PROVIDERS
from frankensurf.scrapling_ready import configure_navigation_readiness, ScraplingReadyProvider

async def test_readiness_overrides_navigation_and_load_wait_preserving_other_waits():
    calls=[]
    class Page:
        async def goto(self,url,**kwargs): calls.append(("goto",url,kwargs));return "response"
        async def wait_for_load_state(self,state=None,**kwargs):calls.append(("wait",state,kwargs))
    p=Page();await configure_navigation_readiness(p,"domcontentloaded")
    assert await p.goto("https://example.test/",wait_until="load",timeout=123)=="response"
    await p.wait_for_load_state("load",timeout=456)
    await p.wait_for_load_state("networkidle")
    assert calls==[("goto","https://example.test/",{"wait_until":"domcontentloaded","timeout":123}),("wait","domcontentloaded",{"timeout":456}),("wait","networkidle",{})]

@pytest.mark.parametrize("requested,expected",[(None,"domcontentloaded"),("load","load")])
async def test_provider_delegates_scoped_policy_and_respects_override(requested,expected):
    seen=[]
    async def isolated(url,policy,backend):seen.append((url,policy,backend));return {"url":url,"content":"body"}
    provider=ScraplingReadyProvider();policy=WebPolicy(scrapling_navigation_wait_until=requested)
    result=await provider.acquire(ProviderRequest("https://example.test/",policy),ProviderServices(None,None,isolated))
    assert result["content"]=="body" and seen[0][2]=="scrapling"
    assert seen[0][1].scrapling_navigation_wait_until==expected
    assert policy.scrapling_navigation_wait_until==requested
    from frankensurf.experimental import installed
    assert provider.manifest.route_scope_required is True
    assert provider.available(()) is installed(provider.manifest.id)
    assert DEFAULT_PROVIDERS.contains("scrapling_ready")

async def test_named_identity_cannot_invoke_readiness_provider():
    from frankensurf.runtime import WebFailure
    async def forbidden(*args):raise AssertionError("Public acquisition must not run")
    with pytest.raises(WebFailure) as exc:
        await DEFAULT_PROVIDERS.acquire("scrapling_ready",ProviderRequest("https://example.test/",WebPolicy(identity="personal")),ProviderServices(None,None,forbidden))
    assert exc.value.code=="IDENTITY_POLICY_DENIED"
