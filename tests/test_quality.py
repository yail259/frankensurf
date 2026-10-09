"""Article pages are judged on their article; every read gets receipt.quality."""
import json

import httpx

from frankensurf import Runtime, WebPolicy
from frankensurf.completeness import article_like, assess, quality

MENU = "".join(f'<li><a href="/section/{n}">Section {n} news and features</a></li>' for n in range(120))
STORY = "".join(f"<p>Paragraph {n} of the story about the harbour ferry, long enough to read like a real "
                f"sentence in a news article.</p>" for n in range(8))
LD = json.dumps({"@context": "https://schema.org", "@type": "NewsArticle", "headline": "Ferry"})


def page(body, ld=True):
    head = f'<script type="application/ld+json">{LD}</script>' if ld else ""
    return f"<html><head><title>Ferry</title>{head}</head><body><nav><ul>{MENU}</ul></nav>{body}</body></html>"


def result(html):
    from frankensurf.runtime import _parse_builtin_content
    parsed = _parse_builtin_content(html, "text/html", "https://news.example.com/x", None)
    return {"content": html, "content_type": "text/html", **parsed}


def test_article_pages_are_recognised_by_markup_or_path():
    assert article_like("https://x.example/anything", result(page("<p>x</p>")))
    assert article_like("https://x.example/news/2026/10/ferry-service-launches-today", result(page("", ld=False)))
    assert not article_like("https://x.example/news/", result(page("", ld=False)))
    assert not article_like("https://x.example/shop/lamps", result(page("", ld=False)))


def test_a_long_page_of_menus_is_not_an_article():
    url = "https://news.example.com/2026/10/ferry-service-launches-today"
    shell = assess(url, result(page("<div id='app'>Loading story</div>")))
    assert shell["complete"] is False  # caught as menus-only before the article rule
    captions = "".join(f"<span>Photo {n}: harbour view, credit agency</span>" for n in range(60))
    gallery = assess(url, result(page(f"<div class='gallery'>{captions}</div><div>Loading story</div>")))
    assert gallery["kind"] == "article" and gallery["complete"] is False
    assert gallery["text_chars"] > 1500 and gallery["article"]["paragraphs"] < 2
    full = assess(url, result(page(f"<article><h1>Ferry</h1>{STORY}</article>")))
    assert full["complete"] and full["article"]["paragraphs"] >= 2
    assert quality(full)["grade"] == "good" and quality(shell)["grade"] == "poor"


def test_quality_grades_flags():
    assert quality({"complete": True, "needs_interaction": "Select guests to see prices"})["grade"] == "partial"
    assert quality({"complete": False, "placeholder": True})["flags"] == ["placeholder"]
    assert "archived" in quality({"complete": True}, {"archived": {"archived_at": "x"}})["flags"]


async def test_every_page_read_carries_a_quality_grade(tmp_path):
    html = page(f"<article><h1>Ferry</h1>{STORY}</article>")
    handle = lambda request: httpx.Response(200, text=html, headers={"content-type": "text/html"})
    async with Runtime(tmp_path, transport=httpx.MockTransport(handle)) as web:
        read = await web.read("https://news.example.com/2026/10/ferry-service-launches-today",
                              WebPolicy(provider="http"))
    grade = read["receipt"]["quality"]
    assert grade["grade"] == "good" and grade["kind"] == "article" and grade["article"]["chars"] >= 600


def test_a_paywall_teaser_is_flagged_and_graded_poor():
    teaser = "<p>The first paragraph of the story about the harbour ferry, long enough to read like a sentence.</p>"
    html = page(f"<article><h1>Ferry</h1>{teaser}<p>Unlock this story and more with your subscription.</p>"
                f"<p>Full Digital Access $3.50 a week, $14 min. cost, charged every 4 weeks.</p></article>")
    verdict = assess("https://news.example.com/news/ferry-service-launches-today", result(html))
    assert verdict["complete"] is False and verdict["paywall"] and verdict["reason"].startswith("paywalled")
    grade = quality(verdict)
    assert grade["grade"] == "poor" and "paywall" in grade["flags"]


def test_an_article_quoting_dollar_figures_is_still_judged_as_an_article():
    money = STORY.replace("harbour ferry", "$6.2 billion harbour ferry")
    verdict = assess("https://news.example.com/companies/fortescue-sues-20261008-p613wu",
                     result(page(f"<article><h1>Ferry</h1>{money}</article>")))
    assert verdict["kind"] == "article" and verdict["prices"] > 0 and verdict["complete"]


def test_template_words_inside_links_are_not_placeholders():
    text = "Long article text about mining. " * 400 + " ".join(
        f"[ad](https://ads.example/x?a=undefined&b=NaN&n={n})" for n in range(5))
    verdict = assess("https://news.example.com/page", {"content": "<p>x</p>", "text": text,
                                                      "content_type": "text/markdown"})
    assert not verdict.get("placeholder")
