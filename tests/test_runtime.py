import io
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from PIL import Image
from frankensurf import Runtime, WebPolicy
from frankensurf.runtime import WebFailure, parse_content


async def test_terminal_block_does_not_escalate_or_fetch_next_same_domain(tmp_path):
    calls = []
    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(429)
    async with Runtime(tmp_path, transport=httpx.MockTransport(handle), domain_delay=0) as web:
        async def unexpected(*args): raise AssertionError("Should not bypass rate block")
        web._get_browser = unexpected
        results = await web.batch(["https://blocked.test/1", "https://blocked.test/2"], WebPolicy(terminal_failures=("RATE_LIMITED",), context_stop_failures=("RATE_LIMITED",)))
    assert calls == ["https://blocked.test/1"]
    assert [r["receipt"]["failure"]["code"] for r in results] == ["RATE_LIMITED", "RATE_LIMITED"]
    assert len(results[0]["receipt"]["attempts"]) == 1
    assert results[1]["receipt"]["attempts"] == []


async def test_cache_never_claims_new_observation_and_now_bypasses(tmp_path):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, text="<title>Fresh</title><p>Body</p>", headers={"content-type":"text/html"})
    async with Runtime(tmp_path, transport=httpx.MockTransport(handle)) as web:
        original = await web.read("https://shop.test", WebPolicy(provider="http"))
        cached = await web.read("https://shop.test", WebPolicy(provider="http", freshness="hour"))
        refreshed = await web.read("https://shop.test", WebPolicy(provider="http"))
    assert len(calls) == 2
    assert cached["receipt"]["cache_hit"] is True
    assert cached["receipt"]["observed_at"] == original["receipt"]["observed_at"]
    assert cached["receipt"]["source_trace_id"] == original["receipt"]["trace_id"]
    assert refreshed["receipt"]["cache_hit"] is False


async def test_expired_cache_is_not_reused(tmp_path):
    calls=[]
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"a":1})
    async with Runtime(tmp_path, transport=httpx.MockTransport(handle)) as web:
        await web.read("https://shop.test", WebPolicy(provider="http"))
        for file in (tmp_path/"cache").glob("*"):
            result=json.loads(file.read_text())
            result["receipt"]["observed_at"]=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
            file.write_text(json.dumps(result))
        result=await web.read("https://shop.test", WebPolicy(provider="http",freshness="hour"))
    assert len(calls)==2
    assert not result["receipt"]["cache_hit"]


async def test_partial_batch_preserves_order_and_fails_invalid_url(tmp_path):
    def handle(request):
        return httpx.Response(404) if request.url.host=="missing.test" else httpx.Response(200,json={"ok":True})
    async with Runtime(tmp_path,transport=httpx.MockTransport(handle),domain_delay=0) as web:
        results=await web.batch(["https://good.test", "https://missing.test", "file:///secret"], WebPolicy(provider="http"))
    assert [r["receipt"]["status"] for r in results]==["observed","failed","failed"]
    assert results[1]["receipt"]["failure"]["code"]=="NOT_FOUND"
    assert results[2]["receipt"]["failure"]["code"]=="INVALID_URL"


async def test_explicit_provider_honors_authority(tmp_path):
    with pytest.raises(ValueError): Runtime(tmp_path,steel_api_url="ftp://steel.example")
    assert Runtime(tmp_path,steel_api_url="https://steel.example:3000").steel_api_url
    async with Runtime(tmp_path,transport=httpx.MockTransport(lambda _:httpx.Response(200,text="ok"))) as web:
        result=await web.read("https://shop.test",WebPolicy(provider="http",identity="facebook-personal"))
    assert result["receipt"]["failure"]["code"]=="IDENTITY_UNKNOWN"


def test_enquiry_recaptcha_is_not_an_access_challenge():
    from frankensurf.runtime import _challenge
    assert not _challenge('<title>Bike for sale</title><p>Bike details</p><div class="g-recaptcha">Enquiry form</div>')
    assert _challenge('<title>Just a moment...</title>')


async def test_batch_images_finishes_without_nested_semaphore_deadlock(tmp_path):
    picture=io.BytesIO()
    Image.new("RGB",(12,13)).save(picture,"PNG")
    def handle(request):
        if request.url.path=="/photo": return httpx.Response(200,content=picture.getvalue(),headers={"content-type":"image/png"})
        return httpx.Response(200,json={"id":1,"images":["https://shop.test/photo"]})
    import asyncio
    async with Runtime(tmp_path,transport=httpx.MockTransport(handle),domain_delay=0) as web:
        result=await asyncio.wait_for(web.batch(["https://shop.test/product"],WebPolicy(provider="http",include_images=True)),2)
    assert result[0]["images"][0]["status"]=="decoded"


async def test_unresolved_html_auto_escalates_but_explicit_http_does_not(tmp_path):
    calls=[]
    def handle(request):
        return httpx.Response(200,text='<title>Auction</title><p>{{lot.title}}</p>',headers={"content-type":"text/html"})
    async with Runtime(tmp_path,transport=httpx.MockTransport(handle)) as web:
        async def rendered(url,policy,provider):
            calls.append(provider)
            return {"url":url,"content":"<title>Auction</title><p>Closed 2024. Vintage camera lot, sold with original box and strap.</p>","raw":b"closed2024","content_type":"text/html", "http_status":200,"headers":{}}
        web._get_browser=rendered
        result=await web.read("https://shop.test/lot")
        explicit=await web.read("https://shop.test/lot",WebPolicy(provider="http"))
    assert calls==["local"]
    assert result["receipt"]["attempts"][0]["failure"]=="VISUAL_REQUIRED"
    assert result["receipt"]["method"]=="local"
    assert explicit["receipt"]["method"]=="http"
    assert explicit["field_status"]["availability"]=="unknown"


async def test_capability_outcomes_exclude_cache_and_keep_sample_sizes(tmp_path):
    def handle(request):
        return httpx.Response(200,json={"ok":True})
    async with Runtime(tmp_path,transport=httpx.MockTransport(handle)) as web:
        await web.read("https://shop.test",WebPolicy(provider="http"))
        await web.read("https://shop.test",WebPolicy(provider="http",freshness="cached"))
        graph=web.capabilities("shop.test")
    assert len(graph)==1
    assert graph[0]["samples"]==1
    assert graph[0]["observed"]==1
    assert "semantic" in graph[0]["caveat"]


async def test_independent_image_batches_share_instance_concurrency_limit(tmp_path):
    import asyncio
    picture=io.BytesIO()
    Image.new("RGB",(12,13)).save(picture,"PNG")
    active=peak=0
    async def handle(request):
        nonlocal active,peak
        active+=1
        peak=max(peak,active)
        await asyncio.sleep(0.01)
        active-=1
        return httpx.Response(200,content=picture.getvalue(),headers={"content-type":"image/png"})
    async with Runtime(tmp_path,transport=httpx.MockTransport(handle),concurrency=2,domain_delay=0) as web:
        groups=await asyncio.gather(web.download_images(["https://a.test/1","https://b.test/2","https://c.test/3"]),web.download_images(["https://d.test/4","https://e.test/5","https://f.test/6"]))
    assert peak<=2
    assert sum(image["status"]=="decoded" for group in groups for image in group)==6


async def test_generic_empty_shell_threshold_is_policy(tmp_path):
    async with Runtime(tmp_path,transport=httpx.MockTransport(lambda _:httpx.Response(200,text='<script>window.ready=1</script><p>ok</p>',headers={"content-type":"text/html"}))) as web:
        result=await web.read("https://shop.test/page",WebPolicy(html_shell_min_text_chars=0))
    assert result["receipt"]["method"] == "http"

@pytest.mark.parametrize("value",[-1,True,1.5])
def test_invalid_shell_threshold_rejected(value):
    with pytest.raises(ValueError):WebPolicy(html_shell_min_text_chars=value)
