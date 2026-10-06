import sys
from types import SimpleNamespace
import pytest
from frankensurf.provider_worker import _direct_camoufox


class Page:
    url = "https://example.test/item?id=1"
    async def goto(self, *args, **kwargs): return SimpleNamespace(status=403)
    async def content(self): return "<h1>Exact recovered item</h1>"
    async def screenshot(self, **kwargs): return b"image"
    async def wait_for_timeout(self, value): pass


async def test_direct_target_block_replaces_entry_stage_and_retains_evidence(monkeypatch):
    import frankensurf.provider_worker as worker
    monkeypatch.setattr(worker, "_CURRENT_STAGE", "public_entry_settle")
    result = await worker._direct_camoufox(Page(), Page.url, {
        "settle_ms": 0, "retain_public_failure_evidence": True, "max_bytes": 1000}, 10, 1000)
    assert result["failure"] == "BLOCKED"
    assert result["failure_stage"] == "navigation_readiness"
    assert result["content"] == "<h1>Exact recovered item</h1>"


@pytest.mark.parametrize("operation,expected", [
    ("goto", "navigation_readiness"), ("selector", "selector_readiness"),
    ("settle", "document_settle"), ("representation", "response_capture"),
    ("screenshot", "screenshot_capture")])
async def test_direct_operation_exception_reports_actual_stage(monkeypatch, operation, expected):
    import frankensurf.provider_worker as worker
    async def fail(*args, **kwargs): raise TimeoutError("test deadline")
    class Ready(Page):
        async def goto(self, *args, **kwargs): return SimpleNamespace(status=200)
        def locator(self, selector): return SimpleNamespace(first=SimpleNamespace(wait_for=fail))
    page = Ready()
    if operation == "goto": page.goto = fail
    if operation == "settle": page.wait_for_timeout = fail
    if operation == "screenshot": page.screenshot = fail
    async def representation(*args): return "body", "text/html"
    monkeypatch.setattr(worker, "_representation", fail if operation == "representation" else representation)
    monkeypatch.setattr(worker, "_CURRENT_STAGE", "public_entry_settle")
    with pytest.raises(TimeoutError):
        await worker._direct_camoufox(page, Page.url, {
            "settle_ms": 1 if operation == "settle" else 0,
            "wait_selector": "h1" if operation == "selector" else None,
            "wait_state": "attached"}, 10, 1000)
    assert worker._CURRENT_STAGE == expected


