import time

import httpx
import pytest

from frankensurf import Runtime, WebPolicy
from frankensurf.runtime import Runtime as RuntimeClass

URL = "https://www.example.com/page"


@pytest.fixture(autouse=True)
def _pacing_on(monkeypatch):
    monkeypatch.setattr(RuntimeClass, "pacing_enabled", True)


def _transport(state):
    def handle(request):
        state["calls"] += 1
        return httpx.Response(state.get("status", 200), text="<html><h1>ok</h1><p>" + "x" * 200 + "</p></html>")
    return httpx.MockTransport(handle)


@pytest.mark.asyncio
async def test_reads_to_one_origin_are_spaced(tmp_path):
    state = {"calls": 0}
    policy = WebPolicy(provider="http", origin_min_interval_seconds=0.4)
    async with Runtime(tmp_path, transport=_transport(state)) as web:
        started = time.monotonic()
        await web.read(URL, policy)
        await web.read(URL + "?b=1", policy)
        elapsed = time.monotonic() - started
    assert state["calls"] == 2 and elapsed >= 0.35


@pytest.mark.asyncio
async def test_block_pauses_origin_across_runtimes_without_new_requests(tmp_path):
    state = {"calls": 0, "status": 403}
    policy = WebPolicy(provider="http", origin_min_interval_seconds=0, origin_cooldown_seconds=600)
    async with Runtime(tmp_path, transport=_transport(state)) as web:
        first = await web.read(URL, policy)
    assert first["receipt"]["failure"]["code"] == "BLOCKED" and state["calls"] == 1
    state["status"] = 200
    async with Runtime(tmp_path, transport=_transport(state)) as web:
        second = await web.read(URL, policy)
    assert second["receipt"]["failure"]["code"] == "BLOCKED" and state["calls"] == 1
    assert "cooling down" in second["receipt"]["failure"]["message"]


@pytest.mark.asyncio
async def test_cooldown_is_per_origin_and_can_be_disabled(tmp_path):
    state = {"calls": 0, "status": 429}
    policy = WebPolicy(provider="http", origin_min_interval_seconds=0, origin_cooldown_seconds=600)
    async with Runtime(tmp_path, transport=_transport(state)) as web:
        await web.read(URL, policy)
        state["status"] = 200
        other = await web.read("https://other.example.org/", policy)
        off = await web.read(URL, WebPolicy(provider="http", origin_min_interval_seconds=0, origin_cooldown_seconds=0))
    assert other["receipt"]["status"] == "observed"
    assert off["receipt"]["status"] == "observed" and state["calls"] == 3


def test_pacing_policy_rejects_negative_values():
    with pytest.raises(ValueError):
        WebPolicy(origin_min_interval_seconds=-1)
    with pytest.raises(ValueError):
        WebPolicy(origin_cooldown_failures=("",))


@pytest.mark.asyncio
async def test_explicit_unblocker_read_is_not_held_by_cooldown(tmp_path, monkeypatch):
    from frankensurf import hosted_providers
    state = {"calls": 0, "status": 403}
    policy = WebPolicy(provider="http", origin_min_interval_seconds=0, origin_cooldown_seconds=600)
    async with Runtime(tmp_path, transport=_transport(state)) as web:
        await web.read(URL, policy)
    unblocked = []
    monkeypatch.setenv("FRANKENSURF_ZENROWS_API_KEY", "k")
    monkeypatch.setattr(hosted_providers, "TRANSPORT", httpx.MockTransport(
        lambda request: unblocked.append(request) or httpx.Response(
            200, text="<html><h1>ok</h1><p>" + "x" * 200 + "</p></html>", headers={"content-type": "text/html"})))
    async with Runtime(tmp_path, transport=_transport(state)) as web:
        direct = await web.read(URL, policy)
        via = await web.read(URL, WebPolicy(provider="zenrows", allow_paid_fallbacks=True,
                                            origin_min_interval_seconds=0, origin_cooldown_seconds=600))
    assert "cooling down" in direct["receipt"]["failure"]["message"]
    assert via["receipt"]["status"] == "observed" and len(unblocked) == 1
