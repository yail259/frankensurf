import asyncio
import httpx
import pytest
from frankensurf import Runtime, WebPolicy
from frankensurf.identity import IdentityRegistry


def registry(tmp_path):
    reg=IdentityRegistry(tmp_path/"registry.json")
    reg.enroll_executor("test",endpoint="http://127.0.0.1:9331",user_data_dir=str(tmp_path/"profile"))
    reg.enroll_identity("personal",executor_id="test",domains=["shop.test"])
    return reg


async def test_unprobed_authentication_cannot_reuse_private_cache(tmp_path):
    reg=registry(tmp_path)
    calls=[]
    async with Runtime(tmp_path/"state",identity_registry=reg.path) as web:
        async def selected(*args): return object()
        async def current(url,*args):
            calls.append(url)
            content="<title>Current</title><p>Observation %d</p>"%len(calls)
            return {"url":url,"content":content,"raw":content.encode(),"content_type":"text/html","http_status":200,"headers":{}}
        web._identity_context=selected
        web._get_identity_browser=current
        first=await web.read("https://shop.test/detail",WebPolicy(identity="personal"))
        second=await web.read("https://shop.test/detail",WebPolicy(identity="personal",freshness="cached"))
    assert len(calls)==2
    assert second["receipt"]["authentication"]=="not_configured"
    assert not second["receipt"]["cache_hit"]
    assert "Observation 2" in second["text"]


async def test_unresponsive_profile_probe_times_out_and_releases_lease(tmp_path):
    reg=registry(tmp_path)
    async with Runtime(tmp_path/"state",identity_registry=reg.path) as web:
        async def unresponsive(*args): await asyncio.sleep(60)
        web._identity_context=unresponsive
        result=await asyncio.wait_for(web.read("https://shop.test/detail",WebPolicy(identity="personal",timeout_seconds=.02)),1)
    assert result["receipt"]["failure"]["code"]=="IDENTITY_EXECUTOR_OFFLINE"
    assert not result["receipt"]["attempts"]
    resolved=reg.resolve("personal","https://shop.test/detail")
    with reg.lease(resolved): pass


async def test_best_effort_identity_cleanup_cannot_hold_lease_forever(tmp_path):
    async with Runtime(tmp_path/"state") as web:
        await asyncio.wait_for(web._identity_cleanup(asyncio.sleep(60)),1)


async def test_trace_cannot_cross_reenrolled_profile_generation(tmp_path):
    from frankensurf.identity import IdentityFailure
    reg=registry(tmp_path)
    async with Runtime(tmp_path/"state",identity_registry=reg.path) as web:
        async def selected(*args): return object()
        async def current(url,*args):
            content="<title>Private old profile</title>"
            return {"url":url,"content":content,"raw":content.encode(),"content_type":"text/html","http_status":200,"headers":{}}
        web._identity_context=selected
        web._get_identity_browser=current
        first=await web.read("https://shop.test/detail",WebPolicy(identity="personal"))
        trace_id=first["receipt"]["trace_id"]
        assert web.trace(trace_id)["receipt"]["identity"]=="personal"
        reg.enroll_executor("test",endpoint="http://127.0.0.1:9331",user_data_dir=str(tmp_path/"other-profile"))
        with pytest.raises(IdentityFailure) as failure: web.trace(trace_id)
        assert failure.value.code=="IDENTITY_CHANGED"


class BrowserJsonResponse:
    headers={"content-type":"text/javascript; charset=utf-8"}
    def __init__(self,raw): self.raw=raw
    async def body(self): return self.raw


class BrowserJsonPage:
    async def content(self): return "<html><body>JSON viewer wrapper</body></html>"


async def test_actual_javascript_never_misrepresented_as_json(tmp_path):
    async with Runtime(tmp_path/"state") as web:
        content,data,kind=await web._browser_representation(BrowserJsonPage(),BrowserJsonResponse(b'window.purchase()'),WebPolicy())
    assert kind=="text/html; rendered=1"
    assert content=="<html><body>JSON viewer wrapper</body></html>"
