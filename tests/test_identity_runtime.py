"""Adversarial runtime tests: identity authority must precede cache and execution."""
from __future__ import annotations
import io
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image
from frankensurf import Runtime, WebPolicy
from frankensurf.identity import IdentityRegistry
from frankensurf.runtime import WebFailure


def registry(tmp_path, *, image_domains=("cdn.test",), auth_check=None):
    path=tmp_path/"registry.json"
    reg=IdentityRegistry(path)
    profile=tmp_path/"owned-profile"
    profile.mkdir(exist_ok=True)
    reg.enroll_executor("local-test",endpoint="http://127.0.0.1:9331",user_data_dir=str(profile),profile_directory="Default",profile_version=1)
    reg.enroll_identity("personal",executor_id="local-test",domains=["shop.test"],image_domains=list(image_domains),authority_mode="LOCAL_ONLY",auth_check=auth_check)
    return reg,path,profile


def observed(url,content='<title>Protected bike</title><p>Current detail</p>'):
    return {"url":url,"content":content,"raw":content.encode(),"content_type":"text/html","http_status":200,"headers":{}}


def forbid_http(request):
    raise AssertionError("Named identity must never reach independent HTTP")


async def stub_identity(web,reg, *, response=None):
    calls=[]
    async def context(resolved,policy):
        calls.append("context")
        return object()
    async def page(url,policy,resolved,context):
        calls.append("page")
        return observed(url) if response is None else response(url)
    async def health(context,resolved,policy):
        return "verified"  # Mock only the positive auth probe; live canary covers it.
    web._identity_context=context
    web._identity_health=health
    web._get_identity_browser=page
    return calls


async def test_unknown_identity_cannot_use_preseeded_cache(tmp_path):
    reg,path,_=registry(tmp_path)
    async with Runtime(tmp_path/"state",identity_registry=path,transport=httpx.MockTransport(forbid_http)) as web:
        def bad_cache(*args): raise AssertionError("Cache accessed before unknown identity authorization")
        web._cache_lookup=bad_cache
        result=await web.read("https://shop.test/detail",WebPolicy(identity="unknown",freshness="cached"))
    assert result["receipt"]["failure"]["code"]=="IDENTITY_UNKNOWN"


async def test_revoked_identity_denied_before_cached_content(tmp_path):
    reg,path,_=registry(tmp_path)
    async with Runtime(tmp_path/"state",identity_registry=path,transport=httpx.MockTransport(forbid_http)) as web:
        calls=await stub_identity(web,reg)
        first=await web.read("https://shop.test/detail",WebPolicy(identity="personal"))
        assert first["receipt"]["status"]=="observed"
        reg.revoke("personal")
        def bad_cache(*args): raise AssertionError("Revoked identity reached cache")
        web._cache_lookup=bad_cache
        rejected=await web.read("https://shop.test/detail",WebPolicy(identity="personal",freshness="cached"))
    assert rejected["receipt"]["failure"]["code"]=="IDENTITY_REVOKED"
    assert calls==["context","page"]


@pytest.mark.parametrize("provider",["http","steel","local"])
async def test_named_identity_never_falls_back_to_unbound_provider(tmp_path,provider):
    reg,path,_=registry(tmp_path)
    async with Runtime(tmp_path/"state",identity_registry=path,steel_api_url="http://127.0.0.1:3100",transport=httpx.MockTransport(forbid_http)) as web:
        async def unexpected(*args): raise AssertionError("Unbound browser reached")
        web._get_browser=unexpected
        result=await web.read("https://shop.test/detail",WebPolicy(identity="personal",provider=provider))
    assert result["receipt"]["failure"]["code"]=="IDENTITY_PROVIDER_DENIED"


async def test_local_identity_browser_disabled_before_cache(tmp_path):
    reg,path,_=registry(tmp_path)
    async with Runtime(tmp_path/"state",identity_registry=path,transport=httpx.MockTransport(forbid_http)) as web:
        def bad_cache(*args): raise AssertionError("Disabled local browser reached cache")
        web._cache_lookup=bad_cache
        result=await web.read("https://shop.test/detail",WebPolicy(identity="personal",allow_local_browser=False,freshness="cached"))
    assert result["receipt"]["failure"]["code"]=="IDENTITY_POLICY_DENIED"


@pytest.mark.parametrize("failure",["IDENTITY_EXECUTOR_OFFLINE","IDENTITY_PROFILE_MISMATCH"])
async def test_cached_identity_still_probes_executor_and_profile(tmp_path,failure):
    reg,path,_=registry(tmp_path)
    async with Runtime(tmp_path/"state",identity_registry=path,transport=httpx.MockTransport(forbid_http)) as web:
        await stub_identity(web,reg)
        first=await web.read("https://shop.test/detail",WebPolicy(identity="personal"))
        assert first["receipt"]["status"]=="observed"
        async def denied(*args): raise WebFailure(failure,"Controlled probe failure")
        web._identity_context=denied
        result=await web.read("https://shop.test/detail",WebPolicy(identity="personal",freshness="cached"))
    assert not result["receipt"]["cache_hit"]
    assert result["receipt"]["failure"]["code"]==failure
    assert not result["receipt"]["attempts"]


async def test_domain_scope_authorized_before_cache_and_executor(tmp_path):
    reg,path,_=registry(tmp_path)
    async with Runtime(tmp_path/"state",identity_registry=path,transport=httpx.MockTransport(forbid_http)) as web:
        def bad_cache(*args): raise AssertionError("Disallowed domain reached cache")
        web._cache_lookup=bad_cache
        async def denied(*args): raise AssertionError("Disallowed domain reached executor")
        web._identity_context=denied
        result=await web.read("https://outside.test/detail",WebPolicy(identity="personal",freshness="cached"))
    assert result["receipt"]["failure"]["code"]=="IDENTITY_DOMAIN_DENIED"


async def test_profile_generation_change_invalidates_identity_cache(tmp_path):
    reg,path,profile=registry(tmp_path)
    async with Runtime(tmp_path/"state",identity_registry=path,transport=httpx.MockTransport(forbid_http)) as web:
        calls=await stub_identity(web,reg)
        first=await web.read("https://shop.test/detail",WebPolicy(identity="personal"))
        assert first["receipt"]["status"]=="observed"
        reg.enroll_executor("local-test",endpoint="http://127.0.0.1:9331",user_data_dir=str(profile),profile_directory="Default",profile_version=2)
        second=await web.read("https://shop.test/detail",WebPolicy(identity="personal",freshness="cached"))
    assert second["receipt"]["status"]=="observed"
    assert not second["receipt"]["cache_hit"]
    assert calls.count("page")==2


async def test_revocation_during_navigation_does_not_return_or_cache_observation(tmp_path):
    reg,path,_=registry(tmp_path)
    async with Runtime(tmp_path/"state",identity_registry=path,transport=httpx.MockTransport(forbid_http)) as web:
        await stub_identity(web,reg)
        async def fenced(url,policy,resolved,context):
            reg.revoke("personal")
            return observed(url)
        web._get_identity_browser=fenced
        result=await web.read("https://shop.test/detail",WebPolicy(identity="personal"))
    assert result["receipt"]["status"]=="failed"
    assert result["receipt"]["failure"]["code"] in {"IDENTITY_REVOKED","IDENTITY_CHANGED"}
    assert not list((tmp_path/"state/cache").glob("*.json"))
    assert result["content"]==""


class ImageResponse:
    def __init__(self,url,data,status=200,headers=None):
        self.url=url
        self.status=status
        self.headers=headers or {"content-type":"image/png"}
        self._data=data
    async def body(self): return self._data
    async def dispose(self): pass


class ContextRequest:
    def __init__(self,redirect=None):
        picture=io.BytesIO()
        Image.new("RGB",(31,17),"blue").save(picture,"PNG")
        self.image=picture.getvalue()
        self.calls=[]
        self.redirect=redirect
    async def get(self,url,**kwargs):
        self.calls.append((url,kwargs))
        if self.redirect: return ImageResponse(url,b"",302,{"location":self.redirect})
        return ImageResponse(url,self.image)


class ImageContext:
    def __init__(self,redirect=None): self.request=ContextRequest(redirect)


async def test_authenticated_photos_use_selected_browser_context_not_http(tmp_path):
    reg,path,_=registry(tmp_path)
    context=ImageContext()
    async with Runtime(tmp_path/"state",identity_registry=path,transport=httpx.MockTransport(forbid_http),domain_delay=0) as web:
        async def selected(*args): return context
        web._identity_context=selected
        result=await web.download_images(["https://cdn.test/image"],WebPolicy(identity="personal"))
    assert result[0]["status"]=="decoded"
    assert result[0]["width"]==31
    assert result[0]["height"]==17
    assert context.request.calls[0][0]=="https://cdn.test/image"
    assert context.request.calls[0][1].get("max_redirects")==0
    assert Path(result[0]["path"]).read_bytes()==context.request.image


async def test_authenticated_image_redirect_never_contacts_disallowed_domain(tmp_path):
    reg,path,_=registry(tmp_path)
    context=ImageContext("https://outside.test/private")
    async with Runtime(tmp_path/"state",identity_registry=path,transport=httpx.MockTransport(forbid_http),domain_delay=0) as web:
        async def selected(*args): return context
        web._identity_context=selected
        result=await web.download_images(["https://cdn.test/image"],WebPolicy(identity="personal"))
    assert result[0]["status"]=="failed"
    assert result[0]["failure"] in {"IDENTITY_POLICY_DENIED","IDENTITY_DOMAIN_DENIED"}
    assert len(context.request.calls)==1


async def test_unscoped_image_denied_before_context_access(tmp_path):
    reg,path,_=registry(tmp_path)
    async with Runtime(tmp_path/"state",identity_registry=path,transport=httpx.MockTransport(forbid_http)) as web:
        async def denied(*args): raise AssertionError("Unscoped photo reached browser")
        web._identity_context=denied
        result=await web.download_images(["https://outside.test/image"],WebPolicy(identity="personal"))
    assert result[0]["failure"]=="IDENTITY_DOMAIN_DENIED"


class GuardSession:
    def __init__(self): self.calls=[];self.listeners={}
    def on(self,name,callback): self.listeners[name]=callback
    async def send(self,name,args=None):
        self.calls.append((name,args))
        if name=="Page.getFrameTree": return {"frameTree":{"frame":{"id":"main"}}}
        return {}


class GuardPage:
    def __init__(self): self.handlers={}
    def on(self,name,callback): self.handlers[name]=callback
    async def close(self): pass


class GuardContext:
    def __init__(self): self.session=GuardSession();self.page=GuardPage()
    async def new_page(self): return self.page
    async def new_cdp_session(self,page):
        assert page is self.page
        return self.session


async def test_browser_scope_guard_checks_every_top_level_redirect_before_transmission(tmp_path):
    reg,path,_=registry(tmp_path)
    context=GuardContext()
    resolved=reg.resolve("personal","https://shop.test/detail")
    async with Runtime(tmp_path/"state",identity_registry=path,transport=httpx.MockTransport(forbid_http)) as web:
        page,denied=await web._identity_page(context,resolved)
        paused=context.session.listeners["Fetch.requestPaused"]
        await paused({"requestId":"initial","resourceType":"Document","frameId":"main","request":{"url":"https://shop.test/redirect"}})
        await paused({"requestId":"redirect","resourceType":"Document","frameId":"main","request":{"url":"https://outside.test/private"}})
    assert ("Fetch.continueRequest",{"requestId":"initial"}) in context.session.calls
    assert ("Fetch.failRequest",{"requestId":"redirect","errorReason":"BlockedByClient"}) in context.session.calls
    assert not any(name=="Fetch.continueRequest" and args.get("requestId")=="redirect" for name,args in context.session.calls)
    assert denied==["IDENTITY_DOMAIN_DENIED"]


async def test_identity_reads_run_page_scripts_like_a_normal_tab(tmp_path):
    reg,path,_=registry(tmp_path)
    context=GuardContext()
    resolved=reg.resolve("personal","https://shop.test/detail")
    async with Runtime(tmp_path/"state",identity_registry=path,transport=httpx.MockTransport(forbid_http)) as web:
        await web._identity_page(context,resolved)
    names=[name for name,_ in context.session.calls]
    assert "Emulation.setScriptExecutionDisabled" not in names
    assert "Network.setBypassServiceWorker" not in names
    assert ("Fetch.enable",{"patterns":[{"urlPattern":"*","resourceType":"Document","requestStage":"Request"}]}) in context.session.calls
    assert "popup" in context.page.handlers


async def test_revocation_fences_already_created_browser_page_requests(tmp_path):
    reg,path,_=registry(tmp_path)
    context=GuardContext()
    resolved=reg.resolve("personal","https://shop.test/detail")
    async with Runtime(tmp_path/"state",identity_registry=path,transport=httpx.MockTransport(forbid_http)) as web:
        page,denied=await web._identity_page(context,resolved)
        reg.revoke("personal")
        await context.session.listeners["Fetch.requestPaused"]({"requestId":"revoked","resourceType":"Document","frameId":"main","request":{"url":"https://shop.test/private"}})
    assert ("Fetch.failRequest",{"requestId":"revoked","errorReason":"BlockedByClient"}) in context.session.calls
    assert denied==["IDENTITY_REVOKED"]


async def test_page_subresources_and_frames_load_from_any_domain(tmp_path):
    reg,path,_=registry(tmp_path)
    context=GuardContext()
    resolved=reg.resolve("personal","https://shop.test/detail")
    async with Runtime(tmp_path/"state",identity_registry=path,transport=httpx.MockTransport(forbid_http)) as web:
        page,denied=await web._identity_page(context,resolved)
        paused=context.session.listeners["Fetch.requestPaused"]
        for kind in ("Image","Script","XHR","Fetch","WebSocket"):
            await paused({"requestId":kind,"resourceType":kind,"frameId":"main","request":{"url":"https://outside.test/resource","method":"POST"}})
        await paused({"requestId":"frame","resourceType":"Document","frameId":"child","request":{"url":"https://outside.test/embed"}})
    for kind in ("Image","Script","XHR","Fetch","WebSocket","frame"):
        assert ("Fetch.continueRequest",{"requestId":kind}) in context.session.calls
    assert denied==[]


async def test_in_scope_top_level_post_is_allowed(tmp_path):
    reg,path,_=registry(tmp_path)
    context=GuardContext()
    resolved=reg.resolve("personal","https://shop.test/detail")
    async with Runtime(tmp_path/"state",identity_registry=path,transport=httpx.MockTransport(forbid_http)) as web:
        page,denied=await web._identity_page(context,resolved)
        await context.session.listeners["Fetch.requestPaused"]({"requestId":"search","resourceType":"Document","frameId":"main","request":{"url":"https://shop.test/search","method":"POST"}})
    assert ("Fetch.continueRequest",{"requestId":"search"}) in context.session.calls
    assert denied==[]


def test_identity_output_urls_keep_cdn_signatures_and_drop_credentials():
    from frankensurf.runtime import _output_url
    url = "https://scontent.xx.fbcdn.net/v/t45/p.jpg?stp=dst-jpg&oh=00_AbC&oe=6700&access_token=secret&fb_dtsg=x#frag"
    assert _output_url(url, named_identity=True) == "https://scontent.xx.fbcdn.net/v/t45/p.jpg?stp=dst-jpg&oh=00_AbC&oe=6700"
    assert _output_url("https://www.facebook.com/marketplace/sydney/search?query=sony%20a7", named_identity=True) == "https://www.facebook.com/marketplace/sydney/search?query=sony%20a7"
    assert _output_url(url) == url
