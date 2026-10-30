"""block_private_network: servers reading URLs they did not choose refuse internal addresses."""
import httpx
import pytest

from frankensurf import Runtime, WebPolicy
from frankensurf.runtime import _private_host

PAGE = "<html><title>Hello</title><body>" + "<p>Public page text.</p>" * 40 + "</body></html>"


@pytest.mark.parametrize("host,private", [
    ("127.0.0.1", True), ("10.0.0.5", True), ("192.168.1.1", True), ("169.254.169.254", True),
    ("[::1]", True), ("fd00::1", True), ("100.64.0.1", True), ("0.0.0.0", True),
    ("localhost", True), ("printer.local", True), ("db.internal", True), ("intranet", True),
    ("8.8.8.8", False), ("example.com", False), ("2606:4700::1111", False)])
def test_private_hosts(host, private):
    assert _private_host(host) is private


async def test_guard_refuses_internal_targets_and_redirects_into_them(tmp_path):
    def handle(request):
        if request.url.host == "redirect.example":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})
    policy = WebPolicy(provider="http", block_private_network=True)
    async with Runtime(tmp_path, transport=httpx.MockTransport(handle)) as web:
        internal = await web.read("http://10.0.0.5/admin", policy)
        metadata = await web.read("http://redirect.example/go", policy)
        allowed = await web.read("http://127.0.0.1:9/local", WebPolicy(provider="http"))
    assert internal["receipt"]["failure"]["code"] == "POLICY_DENIED"
    assert metadata["receipt"]["status"] == "failed"
    assert allowed["receipt"]["status"] == "observed"  # off by default: local dev servers still read


async def test_environment_switch_turns_the_guard_on_for_every_read(tmp_path, monkeypatch):
    monkeypatch.setenv("FRANKENSURF_BLOCK_PRIVATE_NETWORK", "1")
    handle = lambda request: httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})
    async with Runtime(tmp_path, transport=httpx.MockTransport(handle)) as web:
        result = await web.read("http://localhost:8080/", WebPolicy(provider="http"))
    assert result["receipt"]["failure"]["code"] == "POLICY_DENIED"


async def test_browser_guard_blocks_private_http_and_websocket_before_connecting():
    from types import SimpleNamespace
    from frankensurf.runtime import _guard_browser_context

    class Context:
        async def route(self, pattern, callback): self.http = callback
        async def route_web_socket(self, pattern, callback): self.websocket = callback

    class Route:
        def __init__(self, url):
            self.url = url
            self.request = SimpleNamespace(url=url)
            self.action = None
        async def abort(self): self.action = "abort"
        async def continue_(self): self.action = "continue"
        async def close(self): self.action = "close"
        def connect_to_server(self): self.action = "connect"

    context = Context()
    await _guard_browser_context(context)
    for url, expected in [("http://127.0.0.1/admin", "abort"), ("https://1.1.1.1/", "continue")]:
        route = Route(url)
        await context.http(route)
        assert route.action == expected
    for url, expected in [("ws://127.0.0.1/socket", "close"), ("wss://1.1.1.1/socket", "connect")]:
        route = Route(url)
        await context.websocket(route)
        assert route.action == expected


async def test_browser_guard_rejects_unresolved_host(monkeypatch):
    import asyncio
    from frankensurf.runtime import _refuse_private, WebFailure
    async def unresolved(*args, **kwargs): raise OSError("unresolved")
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", unresolved)
    with pytest.raises(WebFailure) as error:
        await _refuse_private("https://unresolved.example/", fail_closed=True)
    assert error.value.code == "POLICY_DENIED"


async def test_guarded_steel_reused_context_is_rejected_before_connecting(tmp_path, monkeypatch):
    from frankensurf.runtime import WebFailure
    async with Runtime(tmp_path) as web:
        async def unexpected(*args): raise AssertionError("must not connect to Steel")
        monkeypatch.setattr(web, "_browser", unexpected)
        with pytest.raises(WebFailure) as error:
            await web._get_browser("https://example.com/", WebPolicy(block_private_network=True), "steel")
    assert error.value.code == "POLICY_DENIED"
