"""Providers added in v0.15.0: request shapes, response parsing and failure mapping."""
import json

import pytest

from frankensurf import experimental, hosted_providers, managed_browsers
from frankensurf.hosted_providers import (ApifyRagBrowserProvider, CloudflareBrowserRunProvider,
                                          FastCrwProvider, ParallelTransport, SkyvernProvider,
                                          TavilyTransport, ZyteProvider, search_url)
from frankensurf.managed_browsers import AnchorProvider, KernelProvider
from frankensurf.providers import DEFAULT_PROVIDERS, ProviderRequest
from frankensurf.runtime import WebFailure, WebPolicy
from frankensurf.search_plugins import DEFAULT_SEARCHES

URL = "https://shop.example.com/item/1"
HTML = "<html><body>" + "<p>Item with price $90</p>" * 20 + "</body></html>"


@pytest.fixture
def api(monkeypatch):
    """Replace the hosted HTTP call; each test queues (status, headers, body) replies."""
    calls, replies = [], []

    async def call(method, url, policy, *, headers=None, params=None, json_body=None):
        calls.append({"method": method, "url": url, "headers": headers or {}, "params": params,
                      "json": json_body, "timeout": policy.timeout_seconds})
        status, response_headers, body = replies.pop(0)
        return status, response_headers, body if isinstance(body, bytes) else json.dumps(body).encode()

    monkeypatch.setattr(hosted_providers, "_call", call)
    monkeypatch.setattr(managed_browsers, "_call", call)
    return calls, replies


def request(**policy):
    return ProviderRequest(URL, WebPolicy(allow_paid_fallbacks=True, **policy))


async def test_kernel_and_anchor_open_render_and_release(api, monkeypatch):
    calls, replies = api
    rendered = []

    async def render(endpoint, url, policy, service):
        rendered.append(endpoint)
        return {"url": url, "content": HTML, "raw": HTML.encode(), "content_type": "text/html",
                "http_status": 200, "headers": {}}
    monkeypatch.setattr(managed_browsers, "_render", render)
    monkeypatch.setenv("FRANKENSURF_KERNEL_API_KEY", "k")
    monkeypatch.setenv("FRANKENSURF_ANCHOR_API_KEY", "a")
    replies += [(200, {}, {"session_id": "s1", "cdp_ws_url": "wss://kernel/s1"}), (204, {}, b"")]
    await KernelProvider().acquire(request(), None)
    replies += [(200, {}, {"data": {"id": "a1", "cdp_url": "wss://anchor/a1"}}), (200, {}, b"{}")]
    await AnchorProvider().acquire(request(), None)
    assert rendered == ["wss://kernel/s1", "wss://anchor/a1"]
    assert calls[0]["headers"]["Authorization"] == "Bearer k" and calls[0]["json"]["stealth"] is True
    assert (calls[1]["method"], calls[1]["url"]) == ("DELETE", "https://api.onkernel.com/browsers/s1")
    assert calls[2]["headers"]["anchor-api-key"] == "a"
    assert (calls[3]["method"], calls[3]["url"]) == ("DELETE", "https://api.anchorbrowser.io/v1/sessions/a1")


async def test_cloudflare_browser_run_reads_wrapped_and_raw_html(api, monkeypatch):
    calls, replies = api
    monkeypatch.setenv("FRANKENSURF_CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setenv("FRANKENSURF_CLOUDFLARE_ACCOUNT_ID", "acct")
    replies += [(200, {"content-type": "application/json"}, {"success": True, "result": HTML}),
                (200, {"content-type": "text/html"}, HTML.encode())]
    assert (await CloudflareBrowserRunProvider().acquire(request(), None))["content"] == HTML
    assert (await CloudflareBrowserRunProvider().acquire(request(), None))["content"] == HTML
    assert calls[0]["url"] == "https://api.cloudflare.com/client/v4/accounts/acct/browser-run/content"
    assert calls[0]["json"]["url"] == URL
    replies.append((200, {"content-type": "application/json"}, {"success": False, "errors": [{}]}))
    with pytest.raises(WebFailure) as failure:
        await CloudflareBrowserRunProvider().acquire(request(), None)
    assert failure.value.code == "PROVIDER_DOWN"


async def test_zyte_maps_bans_and_target_status(api, monkeypatch):
    calls, replies = api
    monkeypatch.setenv("FRANKENSURF_ZYTE_API_KEY", "z")
    replies += [(200, {}, {"url": URL, "statusCode": 200, "browserHtml": HTML}),
                (520, {}, {"type": "/download/temporary-error"}),
                (200, {}, {"url": URL, "statusCode": 404, "browserHtml": "gone"})]
    page = await ZyteProvider().acquire(request(), None)
    assert page["content"] == HTML and calls[0]["json"] == {"url": URL, "browserHtml": True}
    assert calls[0]["headers"]["Authorization"] == "Basic ejo="
    for code in ("BLOCKED", "NOT_FOUND"):
        with pytest.raises(WebFailure) as failure:
            await ZyteProvider().acquire(request(), None)
        assert failure.value.code == code
    assert "zyte" in hosted_providers.UNBLOCKERS


async def test_apify_rag_browser_returns_the_requested_format(api, monkeypatch):
    calls, replies = api
    monkeypatch.setenv("FRANKENSURF_APIFY_API_TOKEN", "ap")
    item = {"metadata": {"url": URL}, "crawl": {"httpStatusCode": 200}, "html": HTML, "markdown": "# Item"}
    replies += [(200, {}, [item]), (200, {}, [item]), (200, {}, [])]
    assert (await ApifyRagBrowserProvider().acquire(request(), None))["content"] == HTML
    markdown = await ApifyRagBrowserProvider().acquire(request(prefer_markdown=True), None)
    assert markdown["content"] == "# Item" and calls[1]["params"]["outputFormats"] == "markdown"
    with pytest.raises(WebFailure) as failure:
        await ApifyRagBrowserProvider().acquire(request(), None)
    assert failure.value.code == "SCHEMA_CHANGED"


async def test_fastcrw_cloud_needs_a_key_and_self_hosted_does_not(api, monkeypatch):
    calls, replies = api
    for name in ("FRANKENSURF_FASTCRW_API_KEY", "FRANKENSURF_FASTCRW_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(hosted_providers, "_env_file", lambda: {})
    monkeypatch.setattr(hosted_providers, "_services_file", lambda: {})
    assert FastCrwProvider().available(()) is False
    monkeypatch.setenv("FRANKENSURF_FASTCRW_URL", "http://127.0.0.1:3310")
    assert FastCrwProvider().available(()) is True
    replies.append((200, {}, {"success": True, "data": {"rawHtml": HTML, "metadata": {"statusCode": 200}}}))
    page = await FastCrwProvider().acquire(request(), None)
    assert page["content"] == HTML and calls[0]["url"] == "http://127.0.0.1:3310/v1/scrape"
    assert "Authorization" not in calls[0]["headers"]


async def test_skyvern_polls_until_complete_and_maps_failures(api, monkeypatch):
    calls, replies = api
    monkeypatch.setenv("FRANKENSURF_SKYVERN_API_KEY", "sk")

    async def no_wait(_):
        return None
    monkeypatch.setattr("asyncio.sleep", no_wait)
    replies += [(200, {}, {"run_id": "tsk_1", "status": "created"}),
                (200, {}, {"run_id": "tsk_1", "status": "running"}),
                (200, {}, {"run_id": "tsk_1", "status": "completed", "output": {"title": "Item", "price": "$90"}})]
    page = await SkyvernProvider().acquire(request(), None)
    assert json.loads(page["content"]) == {"title": "Item", "price": "$90"}
    assert calls[0]["url"] == "https://api.skyvern.com/v1/run/tasks" and calls[0]["json"]["url"] == URL
    assert calls[1]["url"] == "https://api.skyvern.com/v1/runs/tsk_1"
    replies += [(200, {}, {"run_id": "tsk_2"}), (200, {}, {"status": "failed"})]
    with pytest.raises(WebFailure) as failure:
        await SkyvernProvider().acquire(request(), None)
    assert failure.value.code == "PROVIDER_DOWN"
    assert DEFAULT_PROVIDERS.inspect() and "skyvern" not in DEFAULT_PROVIDERS.candidates(
        WebPolicy(allow_paid_fallbacks=True))
    assert WebPolicy().agent_provider_timeout_seconds == 300.0


async def test_tavily_and_parallel_search_send_the_query_and_normalize_results(api, monkeypatch):
    calls, replies = api
    monkeypatch.setenv("FRANKENSURF_TAVILY_API_KEY", "tv")
    monkeypatch.setenv("FRANKENSURF_PARALLEL_API_KEY", "pa")
    replies += [(200, {}, {"results": []}), (200, {}, {"results": []})]
    tavily = await TavilyTransport().acquire(ProviderRequest(search_url("tavily_api", "sony a7", 5), WebPolicy()), None)
    await ParallelTransport().acquire(ProviderRequest(search_url("parallel_api", "sony a7", 5), WebPolicy()), None)
    assert calls[0]["json"] == {"query": "sony a7", "max_results": 5}
    assert calls[0]["headers"]["Authorization"] == "Bearer tv" and tavily["cost_usd"] == 0.008
    assert calls[1]["json"]["search_queries"] == ["sony a7"] and calls[1]["headers"]["x-api-key"] == "pa"
    plugins = {item["id"] for item in DEFAULT_SEARCHES.inspect()}
    assert {"tavily", "parallel"} <= plugins
    from frankensurf.search_plugins import HostedSearchPlugin
    rows = HostedSearchPlugin("parallel", "parallel_api", "parallel_api_key")._normalize(
        {"results": [{"url": "https://a.example", "title": "A", "excerpts": ["one", "two"]}]}, 5)
    assert rows == [{"url": "https://a.example", "title": "A", "snippet": "one two",
                     "engines": ["parallel"], "indexed_date": None}]


def test_patchright_and_nodriver_are_isolated_local_browsers(monkeypatch, tmp_path):
    for name in ("patchright", "nodriver"):
        manifest = next(item for item in DEFAULT_PROVIDERS.inspect() if item["id"] == name)
        assert manifest["rendering"] and manifest["requires_local_browser"]
        assert name in experimental.PROVIDERS
    site = tmp_path / "venv/lib/python3.14/site-packages"
    (site / "nodriver").mkdir(parents=True)
    (tmp_path / "venv/bin").mkdir()
    (tmp_path / "venv/bin/python").write_text("")
    monkeypatch.setenv("FRANKENSURF_PROVIDER_PYTHON", str(tmp_path / "venv/bin/python"))
    assert experimental.installed("nodriver") and not experimental.installed("patchright")
