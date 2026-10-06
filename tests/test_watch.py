import httpx

from frankensurf import Runtime, WebPolicy

URL = "https://classifieds.example.com/search?q=ipod"


def page(*ids):
    links = "".join(f'<a href="/item/{i}">iPod {i}</a>' for i in ids)
    return ("<html><head><title>Results</title></head><body><h1>Results</h1>"
            + "<p>" + "listing text " * 20 + "</p>" + links
            + '<a href="/help">Help</a><a href="https://elsewhere.example.org/item/9">Ad</a></body></html>')


def transport(state):
    def handle(request):
        return httpx.Response(200, text=state["html"], headers={"content-type": "text/html"})
    return httpx.MockTransport(handle)


async def test_watch_reports_only_new_matching_same_site_links(tmp_path):
    state = {"html": page(1, 2)}
    policy = WebPolicy(provider="http")
    async with Runtime(tmp_path, transport=transport(state)) as web:
        first = await web.watch(URL, link_pattern=r"/item/\d+$", policy=policy)
        state["html"] = page(3, 1, 2)
        second = await web.watch(URL, link_pattern=r"/item/\d+$", policy=policy)
        third = await web.watch(URL, link_pattern=r"/item/\d+$", policy=policy)
    assert first["first_poll"] is True
    assert [i["url"] for i in first["new"]] == ["https://classifieds.example.com/item/1",
                                                "https://classifieds.example.com/item/2"]
    assert second["first_poll"] is False
    assert second["new"] == [{"url": "https://classifieds.example.com/item/3", "text": "iPod 3"}]
    assert third["new"] == [] and third["seen_count"] == 3


async def test_watch_without_pattern_keeps_same_site_links_only(tmp_path):
    state = {"html": page(1)}
    async with Runtime(tmp_path, transport=transport(state)) as web:
        result = await web.watch(URL, policy=WebPolicy(provider="http"))
    urls = {i["url"] for i in result["new"]}
    assert "https://classifieds.example.com/help" in urls
    assert not any("elsewhere.example.org" in u for u in urls)


async def test_failed_poll_does_not_touch_watch_state(tmp_path):
    def handle(request):
        return httpx.Response(429, text="slow down")
    async with Runtime(tmp_path, transport=httpx.MockTransport(handle)) as web:
        result = await web.watch(URL, policy=WebPolicy(provider="http"))
    assert result["status"] == "failed" and result["failure"]["code"] == "RATE_LIMITED"
    assert not (tmp_path / "watches").exists()
