"""Result cards: each item link paired with its own thumbnail, from one load."""
import pytest

from frankensurf import providers
from frankensurf.completeness import cards
from frankensurf.providers import ProviderManifest, ProviderRegistry
from frankensurf.runtime import Runtime, WebPolicy, WebFailure

BASE = "https://market.example.com/search?q=lamp"
GRID = """<html><body>
<a href="/help">Help</a>
<div class="card"><a href="/item/brass-desk-lamp-0001/1000001"><img src="/t/1.jpg" alt="Brass lamp"></a>
  <a href="/item/brass-desk-lamp-0001/1000001">Brass desk lamp</a></div>
<div class="card"><img data-src="/t/2-lazy.jpg" src="data:image/gif;base64,R0lGOD">
  <a href="/item/2000002" aria-label="Floor lamp"></a></div>
<div class="card"><picture><source srcset="/t/3-320.webp 320w, /t/3-960.webp 960w"></picture>
  <a href="/item/3000003">Table lamp</a></div>
<div class="card"><div style="background-image: url('/t/4.jpg')"></div><a href="/item/4000004">Lamp four</a></div>
<div class="nav"><a href="/books/text-books/cXA-p1234567.html"><img src="/images/assets/logos/site-logo.png">Textbooks</a></div>
<div class="row"><a href="/item/5000005">No image five</a><a href="/item/6000006">No image six</a>
  <img src="/t/shared.jpg"></div>
</body></html>"""


def test_cards_pair_each_link_with_its_own_image():
    found = {card["url"].rsplit("/", 1)[-1]: card for card in cards(GRID, BASE)}
    assert set(found) == {"1000001", "2000002", "3000003", "4000004", "5000005", "6000006", "cXA-p1234567.html"}
    # A site logo is chrome, never a thumbnail.
    assert found["cXA-p1234567.html"]["image"] is None
    assert found["1000001"]["image"] == "https://market.example.com/t/1.jpg"
    assert found["1000001"]["title"] == "Brass desk lamp"
    assert found["2000002"]["image"] == "https://market.example.com/t/2-lazy.jpg"
    assert found["2000002"]["title"] == "Floor lamp"
    assert found["3000003"]["image"] == "https://market.example.com/t/3-960.webp"
    assert found["4000004"]["image"] == "https://market.example.com/t/4.jpg"
    # Two links share a container: neither may claim its image.
    assert found["5000005"]["image"] is None and found["6000006"]["image"] is None


def test_item_list_entries_come_first_and_menus_never_crowd_out_results():
    menu = "".join(f'<a href="/s-category/c{31000 + n}">Category {n}</a>' for n in range(30))
    structured = {"jsonld": [{"@type": "ItemList", "itemListElement": [
        {"@type": "ListItem", "position": 1, "url": "https://market.example.com/listing/lamp/1345000304"},
        {"@type": "ListItem", "position": 2, "item": {"@id": "/listing/lamp/1344993335", "name": "Lamp two",
                                                       "image": ["/t/two.jpg"]}}]}]}
    found = cards("<html><body>" + menu + GRID + "</body></html>", BASE, limit=4, structured=structured)
    assert [card["url"].rsplit("/", 1)[-1] for card in found] == ["1345000304", "1344993335", "1000001", "2000002"]
    assert found[1] == {"url": "https://market.example.com/listing/lamp/1344993335", "title": "Lamp two",
                        "image": "https://market.example.com/t/two.jpg"}


def test_card_policy_validation():
    assert WebPolicy(card_images=True, scroll_screens=4).scroll_screens == 4
    with pytest.raises(ValueError):
        WebPolicy(scroll_screens=21)
    with pytest.raises(ValueError):
        WebPolicy(expect_terms=("",))


async def test_a_read_with_card_images_returns_cards(tmp_path, monkeypatch):
    class Plugin:
        def __init__(self):
            self.manifest = ProviderManifest("cheap", "1")

        def available(self, configured):
            return True

        async def acquire(self, request, services):
            return {"url": request.url, "content": GRID, "content_type": "text/html", "http_status": 200}
    registry = ProviderRegistry()
    registry.register(Plugin())
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    async with Runtime(tmp_path) as web:
        result = await web.read(BASE, policy_overrides={"card_images": True, "origin_min_interval_seconds": 0,
                                                        "second_opinion_text_chars": 0})
        plain = await web.read(BASE, policy_overrides={"origin_min_interval_seconds": 0, "freshness": "now",
                                                       "second_opinion_text_chars": 0})
    assert len(result["cards"]) == 7
    assert "cards" not in plain
