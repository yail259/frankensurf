import httpx
import pytest

from frankensurf.runtime import Runtime, WebPolicy

PAGE = "<html><head><title>Tides</title></head><body>" + "<p>Tide tables for the harbour.</p>" * 20 + "</body></html>"


async def test_a_plain_fetch_gives_up_sooner_than_a_browser_would(tmp_path):
    seen = []

    def handle(request):
        seen.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})

    async with Runtime(tmp_path, transport=httpx.MockTransport(handle)) as web:
        await web.read("https://tides.example.com/", WebPolicy(provider="http"))
        await web.read("https://tides.example.com/a", WebPolicy(provider="http", http_timeout_seconds=40))

    # A stalled plain fetch is a site holding bots, not a slow page; a longer
    # overall timeout still caps it.
    assert seen == [12, 25]


def test_the_plain_fetch_timeout_must_be_positive():
    with pytest.raises(ValueError, match="http_timeout_seconds"):
        WebPolicy(http_timeout_seconds=0)
