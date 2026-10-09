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
