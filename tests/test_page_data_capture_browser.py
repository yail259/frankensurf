"""End to end: a rendered page fetches JSON from its own API and FrankenSurf returns it."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from frankensurf import Runtime, WebPolicy

pytest.importorskip("playwright")

PAGE = (b"<html><head><title>Shop</title>"
        b'<script id="__NEXT_DATA__" type="application/json">{"props":{"sku":"A1"}}</script></head>'
        b'<body><h1 id="t">loading</h1><p>' + b"filler " * 40 + b"</p>"
        b"<script>fetch(\'/api/item\').then(r=>r.json()).then(d=>{document.getElementById(\'t\').textContent=d.title;});</script>"
        b"</body></html>")


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/item":
            body, ctype = json.dumps({"title": "Widget", "price": 42}).encode(), "application/json"
        else:
            body, ctype = PAGE, "text/html"
        self.send_response(200)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/item"
    server.shutdown()


async def test_rendered_read_returns_frontend_json_and_embedded_data(tmp_path, site):
    async with Runtime(state_dir=tmp_path) as web:
        result = await web.read(site, WebPolicy(provider="local", render=True, capture_json_responses=True,
                                                settle_ms=800))
    if result["receipt"]["status"] != "observed":
        pytest.skip("local Chromium unavailable: %s" % result["receipt"].get("failure"))
    items = result["captured_json"]["items"]
    assert [item["data"] for item in items] == [{"title": "Widget", "price": 42}]
    assert result["structured"]["embedded_json"][0]["data"] == {"props": {"sku": "A1"}}


async def test_capture_is_absent_unless_requested(tmp_path, site):
    async with Runtime(state_dir=tmp_path) as web:
        result = await web.read(site, WebPolicy(provider="local", render=True, settle_ms=200))
    if result["receipt"]["status"] != "observed":
        pytest.skip("local Chromium unavailable")
    assert "captured_json" not in result


async def test_scrapling_route_also_returns_frontend_json(tmp_path, site):
    async with Runtime(state_dir=tmp_path) as web:
        result = await web.read(site, WebPolicy(provider="scrapling", capture_json_responses=True, settle_ms=800))
    if result["receipt"]["status"] != "observed":
        pytest.skip("Scrapling worker unavailable: %s" % result["receipt"].get("failure"))
    assert [item["data"] for item in result["captured_json"]["items"]] == [{"title": "Widget", "price": 42}]
