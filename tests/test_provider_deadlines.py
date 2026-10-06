import asyncio
import pytest
from frankensurf import Runtime, WebPolicy
from frankensurf import providers
from frankensurf.providers import ProviderRegistry, ProviderManifest

async def test_deadline_returns_timeout_and_routes_to_next_plugin(tmp_path, monkeypatch):
    cancelled = asyncio.Event()
    class Stalled:
        manifest = ProviderManifest("stalled", "1")
        async def acquire(self, request, services):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
    class Working:
        manifest = ProviderManifest("working", "1")
        async def acquire(self, request, services):
            return {"url":request.url,"content":"Exact listing", "content_type":"text/plain", "http_status":200}
    registry = ProviderRegistry(); registry.register(Stalled()); registry.register(Working())
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    async with Runtime(tmp_path) as web:
        result = await asyncio.wait_for(web.read("https://catalogue.test/item", WebPolicy(
            provider_candidates=("stalled", "working"), timeout_seconds=0.02,
            provider_deadline_grace_seconds=0, provider_cleanup_grace_seconds=0.02)), 1)
    assert cancelled.is_set()
    assert result["receipt"]["status"] == "observed"
    assert result["receipt"]["attempts"][0]["failure"] == "TIMEOUT"
    assert result["receipt"]["attempts"][1]["provider"] == "working"

async def test_stalled_cleanup_cannot_hold_receipt_open(tmp_path, monkeypatch):
    release = asyncio.Event()
    class Stalled:
        manifest = ProviderManifest("stalled", "1")
        async def acquire(self, request, services):
            try:
                await asyncio.Event().wait()
            finally:
                await release.wait()
    registry = ProviderRegistry(); registry.register(Stalled())
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    runtime_registry = None
    try:
        async with Runtime(tmp_path) as web:
            runtime_registry = web.providers
            result = await asyncio.wait_for(web.read("https://catalogue.test/item", WebPolicy(
                provider="stalled", timeout_seconds=0.02, provider_deadline_grace_seconds=0,
                provider_cleanup_grace_seconds=0.02)), 1)
        assert result["receipt"]["failure"]["code"] == "TIMEOUT"
        assert runtime_registry._pending_cleanup
    finally:
        release.set()
        if runtime_registry is not None:
            await asyncio.gather(*runtime_registry._pending_cleanup,
                                 return_exceptions=True)
    await asyncio.sleep(0)
    assert not runtime_registry._pending_cleanup

async def test_deadline_retains_worker_stage_without_cross_request_leakage(tmp_path, monkeypatch):
    from frankensurf.experimental import _consume_progress
    stages = {"first": "continuation_marker", "second": "category_navigation"}
    class Stalled:
        def __init__(self, name): self.manifest = ProviderManifest(name, "1")
        async def acquire(self, request, services):
            _consume_progress(("@stage " + stages[self.manifest.id] + "\n").encode())
            await asyncio.Event().wait()
    registry = ProviderRegistry()
    for name in stages: registry.register(Stalled(name))
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    async def read(name):
        async with Runtime(tmp_path / name) as web:
            return await web.read("https://catalogue.test/item", WebPolicy(
                provider=name, timeout_seconds=0.02, provider_deadline_grace_seconds=0,
                provider_cleanup_grace_seconds=0.02))
    results = await asyncio.gather(*(read(name) for name in stages))
    for name, result in zip(stages, results):
        failure = result["receipt"]["failure"]
        assert failure["code"] == "TIMEOUT"
        assert failure["message"] == "Public provider acquisition deadline exceeded at " + stages[name]

async def test_deadline_rejects_unknown_progress_and_never_promotes_late_result(tmp_path, monkeypatch):
    from frankensurf.experimental import _consume_progress
    class Late:
        manifest = ProviderManifest("late", "1")
        async def acquire(self, request, services):
            _consume_progress(b"@stage secret-token\n")
            try: await asyncio.Event().wait()
            except asyncio.CancelledError:
                return {"url": request.url, "content": "Late content", "content_type": "text/plain", "http_status": 200}
    registry = ProviderRegistry(); registry.register(Late())
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    async with Runtime(tmp_path) as web:
        result = await web.read("https://catalogue.test/item", WebPolicy(
            provider="late", timeout_seconds=0.02, provider_deadline_grace_seconds=0,
            provider_cleanup_grace_seconds=0.02))
    assert result["receipt"]["failure"] == {"code": "TIMEOUT", "message": "Public provider acquisition deadline exceeded"}
    assert result["content"] == "" and not list((tmp_path / "cache").iterdir())
