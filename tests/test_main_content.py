"""Main content: the article without menus, footers and banners (synthetic pages)."""
import httpx

from frankensurf import Runtime, WebPolicy
from frankensurf import main_content as mc
from frankensurf.completeness import assess

PARAGRAPHS = "".join(f"<p>Paragraph {n} of the story about the new harbour ferry, with enough words to read "
                     f"like an article sentence.</p>" for n in range(8))
MENU = "".join(f'<li><a href="/section/{n}">Section {n}</a></li>' for n in range(60))
COOKIE = ('<div id="cookie-banner"><p>We use cookies to improve your experience. Accept all cookies or manage '
          'preferences in privacy settings.</p></div>')


def no_trafilatura(monkeypatch):
    monkeypatch.setattr(mc, "_trafilatura", lambda content, url: None)


def test_article_is_kept_and_menus_dropped(monkeypatch):
    no_trafilatura(monkeypatch)
    page = (f"<html><body>{COOKIE}<header><ul>{MENU}</ul></header><nav><ul>{MENU}</ul></nav>"
            f"<article><h1>Ferry launches</h1>{PARAGRAPHS}</article>"
            f'<div class="related-stories"><p>{"Another story " * 20}</p></div><footer>{MENU}</footer></body></html>')
    found = mc.main_content(page, "text/html", "https://news.example.com/a")
    assert found["method"] == "builtin" and found["text"].startswith("Ferry launches\n\nParagraph 0")
    assert "Section 3" not in found["text"] and "cookies" not in found["text"]
    assert "Another story" not in found["text"]


def test_without_an_article_tag_the_densest_block_wins(monkeypatch):
    no_trafilatura(monkeypatch)
    page = (f"<html><body><div class='top'><ul>{MENU}</ul></div>"
            f"<div class='wrap'><div class='content'><h1>Ferry launches</h1>{PARAGRAPHS}</div>"
            f"<div class='aside-links'><p><a href='/x'>{'Link text ' * 30}</a></p></div></div></body></html>")
    text = mc.main_content(page, "text/html", "https://news.example.com/a")["text"]
    assert "Paragraph 7" in text and "Section 1" not in text and "Link text" not in text


def test_markdown_from_a_reader_loses_its_leading_navigation():
    nav = "\n".join(f"* [Section {n}](https://news.example.com/s/{n})" for n in range(40))
    body = "\n\n".join(f"Paragraph {n} of the story about the new harbour ferry, with enough words to read "
                       f"like an article." for n in range(6))
    footer = "\n".join(f"[Footer {n}](https://news.example.com/f/{n})" for n in range(20))
    page = f"Title: Ferry launches\n\n{nav}\n\n## Ferry launches\n\nBy A. Writer\n\n{body}\n\n{footer}\n"
    text = mc.main_content(page, "text/markdown", "https://news.example.com/a")["text"]
    assert text.startswith("Title: Ferry launches") and "## Ferry launches" in text[:200]
    assert "Section 3" not in text and "Footer 2" not in text and "Paragraph 5" in text


def test_a_cookie_notice_alone_is_not_the_page():
    text = ("We use cookies. We and our partners use cookies for consent and personalisation. "
            "Accept all or Reject all. Manage preferences in privacy settings. Your privacy matters.")
    assert mc.consent_only(text)
    verdict = assess("https://news.example.com/story/ferry", {"content": f"<p>{text}</p>", "text": text,
                                                               "content_type": "text/html"})
    assert verdict["complete"] is False and verdict["placeholder"]
    assert not mc.consent_only("Paragraph about ferries. " * 40 + " We use cookies.")


async def test_read_returns_main_text_on_request(tmp_path, monkeypatch):
    no_trafilatura(monkeypatch)
    page = f"<html><body><nav><ul>{MENU}</ul></nav><article><h1>Ferry launches</h1>{PARAGRAPHS}</article></body></html>"
    handle = lambda request: httpx.Response(200, text=page, headers={"content-type": "text/html"})
    async with Runtime(tmp_path, transport=httpx.MockTransport(handle)) as web:
        plain = await web.read("https://news.example.com/a", WebPolicy(provider="http"))
        cut = await web.read("https://news.example.com/b", WebPolicy(provider="http", main_content=True))
    assert "main_text" not in plain
    assert cut["main_text"].startswith("Ferry launches")
    assert cut["receipt"]["main_content"]["method"] == "builtin"
    assert cut["receipt"]["main_content"]["chars"] < cut["receipt"]["main_content"]["of_chars"]
