"""watch_sites: new pages from feeds and sitemaps, conditional polls, deduplication (mocked sites)."""
from datetime import datetime, timedelta, timezone

import httpx

from frankensurf import Runtime
from frankensurf.site_feeds import find_feeds, parse_feed, parse_sitemap, pick_child_sitemaps, story_key

NOW = datetime.now(timezone.utc)


def rfc(days_ago):
    from email.utils import format_datetime
    return format_datetime(NOW - timedelta(days=days_ago))


def iso(days_ago):
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


HOME = ('<html><head><link rel="alternate" type="application/rss+xml" href="/feed.xml"></head>'
        '<body><p>Home</p></body></html>')
RSS = ('<rss><channel>' + "".join(
    f'<item><title>Story number {n} about the harbour ferry service</title>'
    f'<link>https://news.example.com/2026/ferry-{n}?utm_source=rss</link><pubDate>{rfc(n)}</pubDate></item>'
    for n in range(5)) + '</channel></rss>')
SITEMAP_INDEX = ('<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                 '<sitemap><loc>https://blog.example.org/sitemap-pages.xml</loc><lastmod>2024-01-01</lastmod></sitemap>'
                 f'<sitemap><loc>https://blog.example.org/sitemap-posts.xml</loc><lastmod>{iso(0)}</lastmod></sitemap>'
                 '</sitemapindex>')
SITEMAP_POSTS = ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(
    f'<url><loc>https://blog.example.org/posts/post-{n}</loc><lastmod>{iso(n)}</lastmod></url>' for n in range(4))
    + '<url><loc>https://blog.example.org/posts/old</loc><lastmod>2020-01-01</lastmod></url></urlset>')


def test_parsers():
    assert find_feeds(HOME, "https://news.example.com/") == ["https://news.example.com/feed.xml"]
    entries = parse_feed(RSS)
    assert len(entries) == 5 and entries[0]["published"].startswith(NOW.strftime("%Y-%m-%d"))
    atom = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>A</title>'
            '<link href="https://x.example/a"/><updated>2026-10-01T00:00:00Z</updated></entry></feed>')
    assert parse_feed(atom) == [{"url": "https://x.example/a", "title": "A", "published": "2026-10-01T00:00:00Z"}]
    assert parse_feed('{"version": "https://jsonfeed.org/version/1.1", "items": [{"url": "https://x.example/b"}]}')[0]["url"]
    assert parse_feed("<html>not a feed</html>") is None
    index = parse_sitemap(SITEMAP_INDEX)
    assert pick_child_sitemaps(index["sitemaps"])[0] == "https://blog.example.org/sitemap-posts.xml"
    assert len(parse_sitemap(SITEMAP_POSTS)["pages"]) == 5
    assert story_key("Story number 1 about the ferry") == story_key("Story Number 1 about the ferry!")


async def test_watch_sites_finds_new_pages_and_polls_cheaply(tmp_path):
    hits = []

    def handle(request):
        url = str(request.url)
        hits.append((url, request.headers.get("if-none-match")))
        if url == "https://news.example.com/":
            return httpx.Response(200, text=HOME, headers={"content-type": "text/html"})
        if url == "https://news.example.com/feed.xml":
            if request.headers.get("if-none-match") == '"v1"':
                return httpx.Response(304)
            return httpx.Response(200, text=RSS, headers={"content-type": "application/rss+xml", "etag": '"v1"'})
        if url == "https://blog.example.org/robots.txt":
            return httpx.Response(200, text="User-agent: *\nSitemap: https://blog.example.org/sitemap_index.xml\n")
        if url == "https://blog.example.org/sitemap_index.xml":
            return httpx.Response(200, text=SITEMAP_INDEX, headers={"content-type": "application/xml"})
        if url == "https://blog.example.org/sitemap-posts.xml":
            return httpx.Response(200, text=SITEMAP_POSTS, headers={"content-type": "application/xml"})
        if url == "https://blog.example.org/":
            return httpx.Response(200, text="<html><body><p>Blog</p></body></html>", headers={"content-type": "text/html"})
        return httpx.Response(404)
    sites = ["https://news.example.com/", "https://blog.example.org/"]
    overrides = {"origin_min_interval_seconds": 0, "origin_route_hint_ttl_seconds": 0}
    async with Runtime(tmp_path, transport=httpx.MockTransport(handle)) as web:
        web._domain_delay = 0
        first = await web.watch_sites(sites, since=iso(2.5), policy_overrides=overrides)
        again = await web.watch_sites(sites, policy_overrides=overrides)
    urls = [entry["url"] for entry in first["new"]]
    assert sum("news.example.com" in url for url in urls) == 3  # days 0, 1, 2
    assert sum("blog.example.org/posts/post-" in url for url in urls) == 3
    assert not any(url.endswith("/old") for url in urls)
    sites_report = {row["site"]: row for row in first["sites"]}
    assert sites_report["https://news.example.com"]["feeds"] == ["https://news.example.com/feed.xml"]
    assert again["new"] == [] and again["unchanged_feeds"] >= 1
    assert ("https://news.example.com/feed.xml", '"v1"') in hits


async def test_the_same_story_on_two_sites_is_reported_once(tmp_path):
    feed = ('<rss><channel><item><title>Harbour ferry service launches across the city today</title>'
            '<link>{}</link><pubDate>' + rfc(0) + '</pubDate></item></channel></rss>')

    def handle(request):
        host = request.url.host
        if request.url.path == "/":
            return httpx.Response(200, text=HOME, headers={"content-type": "text/html"})
        if request.url.path == "/feed.xml":
            return httpx.Response(200, text=feed.format(f"https://{host}/story"), headers={"content-type": "application/rss+xml"})
        return httpx.Response(404)
    async with Runtime(tmp_path, transport=httpx.MockTransport(handle)) as web:
        web._domain_delay = 0
        result = await web.watch_sites(["https://a.example/", "https://b.example/"],
                                       policy_overrides={"origin_min_interval_seconds": 0})
    assert len(result["new"]) == 1


async def test_the_first_poll_is_a_baseline_even_past_the_cap(tmp_path):
    def handle(request):
        url = str(request.url)
        if url == "https://news.example.com/":
            return httpx.Response(200, text=HOME, headers={"content-type": "text/html"})
        if url == "https://news.example.com/feed.xml":
            return httpx.Response(200, text=RSS, headers={"content-type": "application/rss+xml"})
        return httpx.Response(404)
    overrides = {"origin_min_interval_seconds": 0, "origin_route_hint_ttl_seconds": 0}
    async with Runtime(tmp_path, transport=httpx.MockTransport(handle)) as web:
        web._domain_delay = 0
        first = await web.watch_sites(["https://news.example.com/"], since=iso(2.5), max_new_per_site=2,
                                      policy_overrides=overrides)
        again = await web.watch_sites(["https://news.example.com/"], policy_overrides=overrides)
    assert len(first["new"]) == 2 and first["sites"][0]["more"] == 1
    assert again["new"] == []
