"""Wrong pages that look right: off-query results and a site's own error page."""
import pytest

from frankensurf import providers
from frankensurf.completeness import assess, query_terms, relevance
from frankensurf.providers import ProviderManifest, ProviderRegistry
from frankensurf.runtime import Runtime, WebFailure, _site_error

SEARCH = "https://auction.example.com/search?q=camera"
CHROME = "<p>Home Auctions Help Sign in Watchlist Locations Contact us</p>" * 8
FEED = ("<html><title>Auctions</title><body>" + CHROME
        + "".join(f'<a href="/lot/aged-red-wine-dozen-{n}/LOT{n:06d}">Aged red wine, dozen {n}</a> $90'
                  for n in range(30)) + "</body></html>")
CAMERAS = ("<html><title>Search: camera</title><body>" + CHROME
           + "".join(f'<a href="/lot/mirrorless-camera-body-{n}/LOT{n:06d}">Mirrorless camera body {n}</a> $900'
                     for n in range(30)) + "</body></html>")
# Image-only cards: the query lives in the text around the links, not in them.
IMAGE_CARDS = ("<html><body>" + CHROME
               + "".join(f'<div><a href="/lot/{n:08d}"><img src="/t/{n}.jpg"></a><h3>Camera {n}</h3></div>'
                         for n in range(12)) + "</body></html>")


def page(content, text=None):
    return {"content": content, "content_type": "text/html",
            "text": text if text is not None else content.replace("<", " <")}


def test_query_terms_come_from_the_url_and_the_caller():
    assert query_terms(SEARCH) == ["camera"]
    assert query_terms("https://x.example/AuctionLots.aspx?kw=Desk+Lamps&page=2") == ["desk", "lamp"]
    # The path names the search when the query string does not (/s/cameras).
    assert query_terms("https://x.example/s/cameras", ["boxes", "the", "42"]) == ["camera", "box"]
    assert query_terms("https://x.example/") == []


def test_a_default_feed_is_off_query_and_real_results_are_not():
    feed = assess(SEARCH, page(FEED, "Auctions " + "Aged red wine, dozen $90 " * 30 + " camera"))
    assert feed["off_query"] and feed["complete"] is False
    assert "camera" in feed["reason"]
    assert feed["query"]["relevant_items"] == 0
    results = assess(SEARCH, page(CAMERAS, "Mirrorless camera body $900 " * 30))
    assert results["complete"] and not results.get("off_query")
    assert results["query"]["relevant_items"] == 30


def test_image_only_cards_count_text_mentions():
    found = relevance(SEARCH, page(IMAGE_CARDS, " ".join(f"Camera {n}" for n in range(12))))
    assert found["relevant_items"] == 0 and found["mentions"] == 12 and not found["off_query"]


def test_a_page_still_loading_escalates_instead_of_being_off_query():
    shell = assess(SEARCH, page("<html><body>" + CHROME + "</body></html>"))
    assert shell["complete"] is False and not shell.get("off_query")


def test_without_a_known_query_relevance_is_not_checked():
    verdict = assess("https://auction.example.com/search", page(FEED))
    assert verdict["complete"] and "query" not in verdict


def plugin(identifier, content, final_url=None, **manifest):
    class Plugin:
        calls = 0

        def __init__(self):
            self.manifest = ProviderManifest(identifier, "1", **manifest)

        def available(self, configured):
            return True

        async def acquire(self, request, services):
            type(self).calls += 1
            return {"url": final_url or request.url, "content": content,
                    "content_type": "text/html", "http_status": 200}
    return Plugin()


@pytest.fixture
def install(monkeypatch):
    monkeypatch.setattr(Runtime, "completeness_enabled", True)

    def install(*items):
        registry = ProviderRegistry()
        for item in items:
            registry.register(item)
        monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    return install


POLICY = {"origin_route_hint_ttl_seconds": 0, "origin_min_interval_seconds": 0,
          "second_opinion_text_chars": 0}


async def test_off_query_results_are_flagged_not_escalated(tmp_path, install):
    cheap = plugin("cheap", FEED)
    strong = plugin("strong", CAMERAS, rendering=True)
    install(cheap, strong)
    async with Runtime(tmp_path) as web:
        result = await web.read(SEARCH, policy_overrides={**POLICY, "completeness_ladder": ["strong"]})
    receipt = result["receipt"]
    assert receipt["status"] == "observed" and receipt["method"] == "cheap"
    assert receipt["completeness"]["off_query"] is True
    assert receipt["next_step"]["reason"] == "off_query"
    assert "escalations" not in receipt["completeness"]
    assert [attempt["provider"] for attempt in receipt["attempts"]] == ["cheap"]


async def test_expect_terms_catch_a_wrong_page_without_a_query_parameter(tmp_path, install):
    install(plugin("cheap", FEED))
    async with Runtime(tmp_path) as web:
        result = await web.read("https://auction.example.com/search/all", policy_overrides={
            **POLICY, "completeness_ladder": [], "expect_terms": ["camera"]})
    assert result["receipt"]["completeness"]["off_query"] is True


def test_site_error_pages_are_recognised():
    assert _site_error("Lots", "x", "https://a.example/search.aspx?q=1", "https://a.example/Error.aspx?code=500")
    assert _site_error("Page Not Found | Example Auctions", "Sorry.", "https://a.example/x", None)
    assert _site_error("Example", "The page you requested could not be found.", "https://a.example/x", None)
    # An error title under a long cookie banner and footer is still an error page.
    assert _site_error("Example Auctioneers and Valuers - Error", "cookie footer " * 600,
                       "https://a.example/search.aspx?kw=camera", None)
    # Not errors: a real page, a page whose own URL says error, very long pages.
    assert not _site_error("Cameras", "Mirrorless camera", "https://a.example/s?q=camera", None)
    assert not _site_error("Error codes", "x", "https://a.example/docs/error/", "https://a.example/docs/error/")
    assert not _site_error("Error", "word " * 5000, "https://a.example/x", None)
    assert not _site_error("Docs", "an unexpected error has occurred " + "word " * 1000, "https://a.example/x", None)


async def test_a_site_error_page_stops_the_climb(tmp_path, install):
    error = "<html><title>Error</title><body><p>An error occurred.</p></body></html>"
    first = plugin("http", error, final_url="https://auction.example.com/Error.aspx", rendering=False)
    confirm = plugin("confirm", error, final_url="https://auction.example.com/Error.aspx", rendering=True)
    later = plugin("later", CAMERAS, rendering=True)
    install(first, confirm, later)
    async with Runtime(tmp_path) as web:
        result = await web.read(SEARCH, policy_overrides={
            **POLICY, "provider_candidates": ["http", "confirm", "later"], "fake_wall_confirmers": ["confirm"],
            "completeness_ladder": ["later"]})
    receipt = result["receipt"]
    assert receipt["status"] == "failed" and receipt["failure"]["code"] == "NOT_FOUND"
    # The plain fetch's error page gets one confirming read, then the climb stops.
    assert [(attempt["provider"], attempt.get("failure")) for attempt in receipt["attempts"]] == [("http", "NOT_FOUND"), ("confirm", "NOT_FOUND")]
