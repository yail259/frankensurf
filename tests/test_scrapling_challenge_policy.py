import sys
from types import SimpleNamespace
import pytest
from frankensurf import WebPolicy
from frankensurf.provider_worker import acquire


@pytest.mark.parametrize("value", [1, "true", None])
def test_solver_policy_requires_boolean(value):
    with pytest.raises(ValueError): WebPolicy(scrapling_solve_cloudflare=value)


@pytest.mark.parametrize("enabled", [False, True])
async def test_worker_passes_solver_policy_to_installed_provider(monkeypatch, enabled):
    captured = {}
    class Session:
        def __init__(self, **kwargs): captured.update(kwargs)
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def fetch(self, url):
            return SimpleNamespace(html_content="<h1>Public content</h1>", status=200, url=url)
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", SimpleNamespace(AsyncStealthySession=Session))
    monkeypatch.setattr("frankensurf.provider_worker._scrapling_safety", lambda session: None)
    result = await acquire({"url": "https://example.test/page", "provider": "scrapling",
        "timeout_seconds": 60, "max_bytes": 1000, "settle_ms": 0,
        "scrapling_solve_cloudflare": enabled})
    assert result["status"] == "ok"
    assert captured["solve_cloudflare"] is enabled


async def test_provider_timeout_retains_public_page_before_session_closes(monkeypatch):
    events = []
    class Page:
        url = "https://example.test/page"
        async def close(self): pass
        async def content(self):
            events.append("capture")
            return "<title>Challenge still present</title>"
    class Session:
        def __init__(self, **kwargs): self.setup = kwargs["page_setup"]
        async def __aenter__(self): return self
        async def __aexit__(self, *args): events.append("closed")
        async def fetch(self, url):
            await self.setup(Page())
            raise TimeoutError("private-provider-string")
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", SimpleNamespace(AsyncStealthySession=Session))
    monkeypatch.setattr("frankensurf.provider_worker._scrapling_safety", lambda session: None)
    result = await acquire({"url": Page.url, "provider": "scrapling", "timeout_seconds": 90,
        "max_bytes": 1000, "settle_ms": 0, "scrapling_solve_cloudflare": True,
        "retain_public_failure_evidence": True})
    assert result["failure"] == "TIMEOUT"
    assert result["content"] == "<title>Challenge still present</title>"
    assert events == ["capture", "closed"]
    assert "private-provider-string" not in str(result)


@pytest.mark.parametrize("retain,maximum,expected_body", [(True, 1000, True), (False, 1000, False), (True, 5, False)])
async def test_upstream_close_keeps_failure_body_and_original_failure(monkeypatch, retain, maximum, expected_body):
    events = []
    class Page:
        url = "https://example.test/page"
        closed = False
        async def content(self):
            if self.closed: raise RuntimeError("already closed")
            events.append("capture")
            return "<title>Denied before cleanup</title>"
        async def close(self):
            self.closed = True
            events.append("closed")
    class Session:
        def __init__(self, **kwargs): self.setup = kwargs["page_setup"]
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def fetch(self, url):
            page = Page()
            await self.setup(page)
            await page.close()
            raise TimeoutError("private error")
    monkeypatch.setitem(sys.modules, "scrapling.fetchers", SimpleNamespace(AsyncStealthySession=Session))
    monkeypatch.setattr("frankensurf.provider_worker._scrapling_safety", lambda session: None)
    result = await acquire({"url": Page.url, "provider": "scrapling", "timeout_seconds": 90,
        "max_bytes": maximum, "settle_ms": 0, "retain_public_failure_evidence": retain})
    assert result["failure"] == "TIMEOUT"
    assert ("content" in result) is expected_body
    if expected_body: assert result["content"] == "<title>Denied before cleanup</title>"
    assert events == (["capture", "closed"] if retain else ["closed"])
    assert "private error" not in str(result)
