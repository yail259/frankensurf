import asyncio
import json

import httpx
import pytest

from frankensurf import compare

ARTICLE = "Tide tables for the harbour this week. " * 20


class FakeWeb:
    def __init__(self, pages):
        self.pages = pages

    async def read(self, url, policy=None):
        text = self.pages.get(url)
        if text is None:
            return {"receipt": {"status": "failed", "failure": {"code": "CAPTCHA"}, "attempts": [{"provider": "http"}]}}
        return {"text": text, "receipt": {"status": "observed", "cost_usd": 0, "attempts": [{"provider": "camoufox"}]}}


def endpoint(pages):
    def handler(request):
        url = json.loads(request.content)["url"]
        text = pages.get(url)
        if text is None:
            return httpx.Response(200, json={"success": False, "error": "blocked"})
        return httpx.Response(200, json={"success": True, "data": {"markdown": text, "metadata": {"creditsUsed": 1}}})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_cases_take_an_optional_phrase_after_a_tab(tmp_path):
    path = tmp_path / "urls.txt"
    path.write_text("# my sites\nhttps://a.example/tides\tharbour\n\nhttps://b.example/\n")

    assert compare.load_cases(str(path)) == [
        {"url": "https://a.example/tides", "expect": "harbour"},
        {"url": "https://b.example/", "expect": None}]


def test_an_empty_file_says_so(tmp_path):
    path = tmp_path / "urls.txt"
    path.write_text("# nothing yet\n")

    with pytest.raises(ValueError, match="no URLs"):
        compare.load_cases(str(path))


def test_a_phrase_grades_the_page_and_without_one_only_blocks_and_empties_count():
    with_phrase = {"url": "https://a.example/", "expect": "harbour"}
    without = {"url": "https://a.example/", "expect": None}

    assert compare.grade(with_phrase, ARTICLE) == "right"
    assert compare.grade(with_phrase, "Shop opening hours. " * 20) == "wrong"
    assert compare.grade(with_phrase, "") == "empty"
    assert compare.grade(without, ARTICLE) == "page"
    assert compare.grade(without, "Just a moment... Checking your browser before accessing. " * 5) == "blocked"


def test_both_sides_read_every_url_and_the_summary_names_who_got_what():
    cases = [{"url": "https://a.example/tides", "expect": "harbour"},
             {"url": "https://b.example/walled", "expect": "harbour"}]
    web = FakeWeb({"https://a.example/tides": ARTICLE, "https://b.example/walled": ARTICLE})
    client = endpoint({"https://a.example/tides": ARTICLE})

    rows = asyncio.run(compare.run_cases(web, client, None, "http://fc.local", cases))
    text = compare.summary(rows, "http://fc.local")

    assert [row["frankensurf"]["grade"] for row in rows] == ["right", "right"]
    assert [row["against"]["grade"] for row in rows] == ["right", "empty"]
    assert rows[1]["against"]["error"] == "blocked"
    assert rows[0]["against"]["credits"] == 1
    assert "Only Frankensurf got a page: 1; only http://fc.local: 0" in text
    assert "1 credits" in text


def test_firecrawls_cloud_needs_a_key(monkeypatch, tmp_path):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)

    with pytest.raises(ValueError, match="needs a key"):
        asyncio.run(compare.compare([{"url": "https://a.example/", "expect": None}],
                                    state_dir=tmp_path, policy=None))
