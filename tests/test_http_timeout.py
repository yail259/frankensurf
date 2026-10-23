import httpx
import pytest

from frankensurf.runtime import Runtime, WebPolicy

PAGE = "<html><head><title>Tides</title></head><body>" + "<p>Tide tables for the harbour.</p>" * 20 + "</body></html>"
QUIET = {"second_opinion_text_chars": 0, "hedge_after_seconds": 0, "completeness_escalation": False,
         "origin_min_interval_seconds": 0}


async def test_a_plain_fetch_on_the_routers_climb_gives_up_sooner(tmp_path):
    seen = []

    def handle(request):
        seen.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})

    async with Runtime(tmp_path, transport=httpx.MockTransport(handle)) as web:
        await web.read("https://tides.example.com/", policy_overrides=QUIET)
        await web.read("https://tides.example.com/a", policy_overrides={**QUIET, "http_timeout_seconds": 40})
        await web.read("https://tides.example.com/b", WebPolicy(provider="http"))

    # A stalled plain fetch is a site holding bots, not a slow page, so the climb
    # moves on; the overall timeout still caps it, and a pinned provider keeps it.
    assert seen == [12, 25, 25]


def test_the_plain_fetch_timeout_must_be_positive():
    with pytest.raises(ValueError, match="http_timeout_seconds"):
        WebPolicy(http_timeout_seconds=0)
