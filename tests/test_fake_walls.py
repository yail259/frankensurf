"""Public reads confirm walls that sites fake for bots: sign-in redirects and 404s."""
import pytest

from frankensurf import providers
from frankensurf.completeness import item_links
from frankensurf.providers import ProviderManifest, ProviderRegistry
from frankensurf.runtime import Runtime, WebFailure

PAGE = "<html><title>News</title><body>" + "<p>World news story text.</p>" * 40 + "</body></html>"


def plugin(identifier, failure=None):
    class Plugin:
        calls = 0

        def __init__(self):
            self.manifest = ProviderManifest(identifier, "1")

        async def acquire(self, request, services):
            type(self).calls += 1
            if failure:
                raise WebFailure(failure, "fixture")
            return {"url": request.url, "content": PAGE, "content_type": "text/html", "http_status": 200}
    return Plugin()


@pytest.fixture
def install(monkeypatch):
    def apply(*items):
        registry = ProviderRegistry()
        for item in items:
            registry.register(item)
        monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    return apply


POLICY = {"origin_route_hint_ttl_seconds": 0, "second_opinion_text_chars": 0}


async def test_a_bot_sign_in_redirect_does_not_stop_a_public_read(tmp_path, install):
    install(plugin("redirected", "AUTH_REQUIRED"), plugin("browser"))
    async with Runtime(tmp_path) as web:
        result = await web.read("https://news.example/world", policy_overrides=POLICY)
        again = await web.read("https://news.example/world/2", policy_overrides=POLICY)
    assert [a["provider"] for a in result["receipt"]["attempts"]] == ["redirected", "browser"]
    assert result["receipt"]["status"] == "observed"
    assert again["receipt"]["status"] == "observed"  # no site-wide stop after a public sign-in wall


async def test_repeated_sign_in_walls_are_a_real_login_wall(tmp_path, install):
    items = [plugin(f"p{n}", "AUTH_REQUIRED") for n in range(7)]
    install(*items)
    async with Runtime(tmp_path) as web:
        result = await web.read("https://members.example/inbox", policy_overrides={
            **POLICY, "auth_wall_confirmations": 3, "escalate_after_walls": 0})
    assert result["receipt"]["failure"]["code"] == "AUTH_REQUIRED"
    assert len(result["receipt"]["attempts"]) == 3 and type(items[3]).calls == 0


async def test_a_suspected_fake_wall_is_confirmed_by_a_different_fetcher(tmp_path, install):
    install(plugin("http", "NOT_FOUND"), plugin("local", "NOT_FOUND"), plugin("reader"))
    async with Runtime(tmp_path) as web:
        result = await web.read("https://shop.example/item/1", policy_overrides={
            **POLICY, "fake_wall_confirmers": ["reader"]})
    assert [a["provider"] for a in result["receipt"]["attempts"]] == ["http", "reader"]
    assert result["receipt"]["status"] == "observed"


async def test_a_plain_fetch_404_is_confirmed_once(tmp_path, install):
    install(plugin("http", "NOT_FOUND"), plugin("browser", "NOT_FOUND"), plugin("third"))
    async with Runtime(tmp_path) as web:
        result = await web.read("https://shop.example/item/1", policy_overrides=POLICY)
    assert [a["provider"] for a in result["receipt"]["attempts"]] == ["http", "browser"]
    assert result["receipt"]["failure"]["code"] == "NOT_FOUND"


def test_markdown_links_count_as_item_links():
    markdown = "".join(f"[Lamp {n}](https://shop.example.com/product/brass-lamp-{n}/SKU{n:06d}) $49\n"
                       for n in range(12))
    assert item_links(markdown, "https://shop.example.com/search?q=lamp") == 12


def test_bot_pages_and_sign_in_prompts_are_walls():
    from frankensurf.runtime import _wall_after_parse
    long_text = 'Results ' * 600
    assert _wall_after_parse('What is a bot?', 'If you are seeing this page, KAYAK thinks you are a "bot".',
                             'https://www.kayak.com/hotels/Tokyo', 'https://www.kayak.com/help/bots.html') == 'CAPTCHA'
    assert _wall_after_parse('Shop', long_text, 'https://shop.example/search?q=x',
                             'https://shop.example/captcha?next=/search') == 'CAPTCHA'
    assert _wall_after_parse('Mercado Libre', '¡Hola! Para continuar, ingresa a tu cuenta. Soy nuevo',
                             'https://listado.mercadolibre.com.mx/bicicleta', None) == 'AUTH_REQUIRED'
    # A long real page that mentions signing in somewhere is not a wall.
    assert _wall_after_parse('Shop', long_text + ' Please sign in to save items.',
                             'https://shop.example/search?q=x', None) is None
    assert _wall_after_parse('Bots in games', long_text, 'https://news.example/robots/bots',
                             'https://news.example/robots/bots') is None


async def test_a_narrow_search_pass_gets_one_strong_second_opinion(tmp_path, monkeypatch):
    from frankensurf import providers as module
    from frankensurf.providers import ProviderManifest, ProviderRegistry
    from frankensurf.runtime import Runtime
    chrome = '<p>Home Shop Help</p>' * 30
    def links(n):
        return ''.join(f'<a href="/product/brass-desk-lamp-{i}/SKU{i:06d}">Lamp {i}</a>' for i in range(n))
    narrow = '<html><title>Search</title><body>' + chrome + links(11) + '</body></html>'
    full = '<html><title>Search</title><body>' + chrome + links(40) + '</body></html>'
    class Plugin:
        def __init__(self, identifier, content, paid=False):
            self.manifest = ProviderManifest(identifier, '1', paid=paid)
            self.content, self.calls = content, 0
        def available(self, configured):
            return True
        async def acquire(self, request, services):
            self.calls += 1
            return {'url': request.url, 'content': self.content, 'content_type': 'text/html', 'http_status': 200}
    cheap, free_helper, strong = Plugin('cheap', narrow), Plugin('helper', narrow), Plugin('strong', full, paid=True)
    registry = ProviderRegistry()
    for item in (cheap, free_helper, strong):
        registry.register(item)
    monkeypatch.setattr(module, 'DEFAULT_PROVIDERS', registry)
    monkeypatch.setattr(Runtime, 'completeness_enabled', True)
    async with Runtime(tmp_path) as web:
        result = await web.read('https://shop.example.com/search?q=lamp', policy_overrides={
            'origin_route_hint_ttl_seconds': 0, 'second_opinion_text_chars': 0, 'allow_paid_fallbacks': True,
            'completeness_ladder': ['helper', 'strong']})
    record = result['receipt']['completeness']
    assert record['borderline'] is True and [s['provider'] for s in record['escalations']] == ['strong']
    assert result['receipt']['method'] == 'strong' and free_helper.calls == 0
