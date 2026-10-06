import pytest
from frankensurf.provider_worker import _page_failure
class Page:
    url="https://www.bikesales.com.au/bikes/used/"
    calls=0
    async def content(self):
        self.calls+=1
        return "<html>Access denied</html>"
@pytest.mark.asyncio
async def test_opt_in_retains_failed_page_without_upgrading_failure():
    p=Page();r=await _page_failure(p,{"retain_public_failure_evidence":True,"max_bytes":100},"BLOCKED",403,"category_navigation")
    assert r["status"]=="failed" and r["failure"]=="BLOCKED" and r["http_status"]==403
    assert r["failure_stage"]=="category_navigation" and r["content"]=="<html>Access denied</html>"
@pytest.mark.asyncio
async def test_opt_out_does_not_read_failed_page():
    p=Page();r=await _page_failure(p,{"retain_public_failure_evidence":False,"max_bytes":100},"CONTENT_MISMATCH",stage="exact_listing_search")
    assert p.calls==0 and "content" not in r
@pytest.mark.asyncio
async def test_failed_page_evidence_respects_byte_budget():
    r=await _page_failure(Page(),{"retain_public_failure_evidence":True,"max_bytes":1},"BLOCKED",403)
    assert "content" not in r and r["failure"]=="BLOCKED"
@pytest.mark.asyncio
async def test_unreadable_page_preserves_original_failure():
    class Broken(Page):
        async def content(self):raise RuntimeError("closed")
    r=await _page_failure(Broken(),{"retain_public_failure_evidence":True,"max_bytes":100},"BLOCKED",403)
    assert "content" not in r and r["failure"]=="BLOCKED"


