import io
import httpx
import pytest
from PIL import Image
from frankensurf import Runtime, WebPolicy

async def test_transient_image_error_recovers_with_retained_attempts(tmp_path):
    image=io.BytesIO();Image.new("RGB",(12,8),"blue").save(image,"PNG")
    calls=[]
    def handle(request):
        calls.append(str(request.url))
        if len(calls)==1:return httpx.Response(503,text="temporarily unavailable")
        return httpx.Response(200,content=image.getvalue(),headers={"content-type":"image/png"})
    async with Runtime(tmp_path,transport=httpx.MockTransport(handle),domain_delay=0) as web:
        r=(await web.download_images(["https://cdn.test/image.png"],WebPolicy(image_max_attempts=2,image_retry_delay_seconds=0)))[0]
    assert r["status"]=="decoded"
    assert calls==["https://cdn.test/image.png"]*2
    assert [a["status"] for a in r["attempts"]]==["failed","decoded"]
    assert r["attempts"][0]["failure"]=="PROVIDER_DOWN"

async def test_image_retry_budget_can_disable_recovery(tmp_path):
    calls=[]
    def handle(request):calls.append(request.url);return httpx.Response(503)
    async with Runtime(tmp_path,transport=httpx.MockTransport(handle),domain_delay=0) as web:
        r=(await web.download_images(["https://cdn.test/image.png"],WebPolicy(image_max_attempts=1,image_retry_delay_seconds=0)))[0]
    assert r["status"]=="failed" and len(calls)==1

@pytest.mark.parametrize("status,failure",[(404,"NOT_FOUND"),(401,"AUTH_REQUIRED")])
async def test_terminal_image_failure_is_not_retried(tmp_path,status,failure):
    calls=[]
    def handle(request):calls.append(request.url);return httpx.Response(status)
    async with Runtime(tmp_path,transport=httpx.MockTransport(handle),domain_delay=0) as web:
        r=(await web.download_images(["https://cdn.test/image.png"],WebPolicy(image_max_attempts=3,image_retry_delay_seconds=0)))[0]
    assert r["failure"]==failure and len(calls)==1


async def test_exhausted_transient_failure_retains_every_attempt(tmp_path):
    calls=[]
    def handle(request):calls.append(request.url);return httpx.Response(503)
    async with Runtime(tmp_path,transport=httpx.MockTransport(handle),domain_delay=0) as web:
        r=(await web.download_images(["https://cdn.test/image.png"],WebPolicy(image_max_attempts=3,image_retry_delay_seconds=0)))[0]
    assert r["failure"]=="PROVIDER_DOWN" and len(calls)==3
    assert len(r["attempts"])==3
    assert all(a["http_status"]==503 for a in r["attempts"])

@pytest.mark.parametrize("policy",[{"image_max_attempts":0},{"image_max_attempts":True},{"image_retry_delay_seconds":-1},{"image_retry_delay_seconds":float("nan")},{"image_retry_failures":["TIMEOUT"]}])
def test_invalid_image_retry_policy_rejected(policy):
    with pytest.raises(ValueError):WebPolicy(**policy)
