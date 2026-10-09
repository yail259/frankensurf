"""Safe browser actions, end to end on a local server: consent, load more, reveal."""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from frankensurf import Runtime, WebPolicy

pytest.importorskip("playwright")

STORY = "".join(f"<p>Paragraph {n} of the ferry story, long enough to read like a news sentence.</p>" for n in range(8))
CONSENT = f"""<html><body>
<div id="cookie-consent" role="dialog"><p>We use cookies for analytics and ads.</p>
  <button onclick="document.title='ACCEPTED'">Accept all</button>
  <button onclick="document.getElementById('cookie-consent').remove();document.getElementById('story').style.display='block'">Reject all</button>
</div>
<article id="story" style="display:none"><h1>Ferry</h1>{STORY}</article></body></html>"""
MORE = """<html><body><ul id="list"></ul><button id="more">Load more</button>
<button onclick="document.title='CART'">Add to cart</button>
<script>
let n = 0; const list = document.getElementById('list');
function add(k) { for (let i = 0; i < k; i++) { const li = document.createElement('li');
  li.innerHTML = '<a href="/p/item-' + n + '">Item ' + n + ' $' + (10 + n) + '</a>'; list.appendChild(li); n++; } }
add(4); document.getElementById('more').onclick = () => add(4);
</script></body></html>"""
REVEAL = """<html><body><h1>Island cruise</h1>
<p>Day 1 Sydney. Day 2 At Sea. Day 3 Noumea. Day 4 Lifou. Day 5 Mystery Island. Day 6 At Sea.</p>
<p>How many guests in this cabin? Select 1-4 guests for your first cabin to see prices.</p>
<select id="guests"><option value="">Choose guests</option><option value="2">2 guests</option></select>
<div id="price"></div>
<script>document.getElementById('guests').onchange = e => {
  document.getElementById('price').textContent = 'From $1,299 per person for ' + e.target.value + ' guests'; };</script>
</body></html>"""
PAGES = {"/consent": CONSENT, "/more": MORE, "/cruise/island-cruise-10-nights": REVEAL}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path.split("?")[0], "<html><body>x</body></html>").encode()
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
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


async def read(web, url, actions):
    return await web.read(url, WebPolicy(provider="local", render=True, settle_ms=200, interactions=actions))


async def test_consent_is_rejected_never_accepted(tmp_path, site):
    async with Runtime(tmp_path) as web:
        result = await read(web, site + "/consent", ("dismiss_consent",))
    if result["receipt"]["status"] != "observed":
        pytest.skip("local Chromium unavailable")
    assert result["receipt"]["interactions"] == [{"action": "dismiss_consent", "label": "Reject all", "ok": True}]
    assert "Paragraph 7" in result["text"] and result["title"] != "ACCEPTED"


async def test_load_more_grows_the_list_and_never_adds_to_cart(tmp_path, site):
    async with Runtime(tmp_path) as web:
        result = await read(web, site + "/more", ("load_more",))
    if result["receipt"]["status"] != "observed":
        pytest.skip("local Chromium unavailable")
    pressed = [item for item in result["receipt"]["interactions"] if item["action"] == "load_more"]
    assert len(pressed) == 3 and "Item 15" in result["text"]
    assert result["title"] != "CART"


async def test_escalation_reveals_a_price_behind_a_choice(tmp_path, site):
    async with Runtime(tmp_path) as web:
        web.completeness_enabled = True
        result = await web.read(site + "/cruise/island-cruise-10-nights", policy_overrides={
            "provider_candidates": None, "origin_min_interval_seconds": 0, "second_opinion_text_chars": 0,
            "origin_route_hint_ttl_seconds": 0})
    if result["receipt"]["status"] != "observed":
        pytest.skip("local Chromium unavailable")
    steps = result["receipt"]["completeness"]["escalations"]
    assert any(step["provider"] == "interact" and step["complete"] for step in steps)
    assert "$1,299" in result["text"]
    assert result["receipt"]["interactions"][0]["action"] == "reveal"
