"""End to end: a search box run by script, found by convention, typed into once."""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from frankensurf import Runtime, WebPolicy
from frankensurf.module_discovery import search_template

pytest.importorskip("playwright")

HOME = (b"<html><head><title>Shop</title></head><body>"
        b'<input type="email" name="newsletter" placeholder="Email">'
        b'<div><input type="search" id="box" placeholder="Search products"></div>'
        b"<script>document.getElementById('box').addEventListener('keydown', e => {"
        b"if (e.key === 'Enter') location.href = '/find?term=' + encodeURIComponent(e.target.value);});"
        b"</script></body></html>")
RESULTS = b"<html><head><title>Results</title></head><body><p>results</p></body></html>"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = RESULTS if self.path.startswith("/find") else HOME
        self.send_response(200)
        self.send_header("content-type", "text/html")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()


async def test_probe_types_into_the_search_box_and_reports_where_it_lands(tmp_path, site):
    async with Runtime(state_dir=tmp_path) as web:
        landed = await web._probe_search(site, "desk lamp", WebPolicy(timeout_seconds=20))
        refused = await web._probe_search(site, "desk lamp", WebPolicy(allow_local_browser=False))
    if landed is None:
        pytest.skip("local Chromium unavailable")
    assert landed == site + "find?term=desk%20lamp"
    assert search_template(landed)[0] == site + "find?term={query}"
    assert refused is None
