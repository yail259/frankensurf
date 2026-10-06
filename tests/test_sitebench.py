import asyncio
import importlib.util
import json
import sys
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "sitebench", Path(__file__).resolve().parents[1] / "scripts" / "sitebench.py")
sitebench = importlib.util.module_from_spec(SPEC)
sys.modules["sitebench"] = sitebench
SPEC.loader.exec_module(sitebench)

SEARCH = {"site": "shop", "kind": "search", "url": "https://shop.example.com/search?q=x",
          "follow": {"pattern": r"/item/\d+", "n": 2, "min_links": 2}}


def page(text="", content="", content_type="text/html", status="observed", structured=None, url=None):
    return {"text": text, "content": content, "content_type": content_type, "structured": structured,
            "url": url, "receipt": {"status": status, "attempts": [{"provider": "http"}], "latency_ms": 10}}


def test_checks_catch_the_false_successes_the_scoreboard_missed():
    listing = {"site": "s", "kind": "listing", "url": "u"}
    assert sitebench.check(listing, page("Sony A7 III body. " * 30 + "Price $1,250"))[0]
    assert sitebench.check(listing, page("Robot or human? " * 30)) == (False, "no_price")
    assert sitebench.check(listing, page("Price $5")) == (False, "thin")
    assert sitebench.check(listing, page("Vélo de course en bon état. " * 20 + "450 €"))[0]
    assert sitebench.check(listing, page("Road bike in good order. " * 20 + "£450"))[0]
    assert sitebench.check({"site": "s", "kind": "pdf", "url": "u"}, page("%PDF-1.4 " + "x" * 200)) == (False, "pdf_bytes")
    assert sitebench.check({"site": "s", "kind": "json", "url": "u"}, page(structured={"products": []})) == (False, "empty_json")
    assert sitebench.check({"site": "s", "kind": "json", "url": "u", "expect": "handle"},
                           page(structured={"products": [{"handle": "bike"}]}))[0]
    article = {"site": "s", "kind": "article", "url": "u", "expect": "Attention"}
    assert sitebench.check(article, page("Attention Is All You Need. " * 40))[0]
    assert sitebench.check(article, page("Other text. " * 100)) == (False, "expect_missing")


def test_search_needs_enough_matching_same_site_links():
    links = ('<a href="/item/1">a</a><a href="/item/2">b</a><a href="/about">c</a>'
             '<a href="https://other.example.com/item/3">d</a>')
    good = page("results " * 100, content=links, url=SEARCH["url"])
    assert sitebench.check(SEARCH, good)[0]
    assert sitebench.same_site_links(good, SEARCH["url"], r"/item/\d+") == [
        "https://shop.example.com/item/1", "https://shop.example.com/item/2"]
    one = page("results " * 100, content='<a href="/item/1">a</a>', url=SEARCH["url"])
    assert sitebench.check(SEARCH, one) == (False, "few_links:1")


class FakeWeb:
    def __init__(self, dead=()):
        self.read_urls = []
        self.dead = set(dead)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def read(self, url, policy_overrides=None):
        self.read_urls.append(url)
        if url in self.dead:
            result = page(url=url, status="failed")
            result["receipt"]["failure"] = {"code": "NOT_FOUND"}
            return result
        if "search" in url:
            return page("results " * 100, content='<a href="/item/1">a</a><a href="/item/2">b</a><a href="/item/3">c</a>', url=url)
        return page("Camera body in great condition. " * 20 + "$900", url=url)


def test_search_cases_follow_live_listing_links(tmp_path, monkeypatch):
    import frankensurf
    web = FakeWeb()
    monkeypatch.setattr(frankensurf, "Runtime", lambda state_dir: web)
    out = tmp_path / "rows.jsonl"
    rows, dead = asyncio.run(sitebench.run([SEARCH], tmp_path / "state", False, out))
    assert dead == []
    assert web.read_urls == [SEARCH["url"], "https://shop.example.com/item/1", "https://shop.example.com/item/2"]
    assert [r["kind"] for r in rows] == ["search", "listing", "listing"]
    assert all(r["valid"] for r in rows) and rows[1]["followed_from"] == SEARCH["url"]
    assert len(out.read_text().splitlines()) == 3
    summary = sitebench.summarize(rows)
    assert summary["overall"]["valid_rate"] == 1.0 and summary["by_kind"]["listing"]["cases"] == 2


def test_a_dead_followed_link_is_replaced_not_scored(tmp_path, monkeypatch):
    import frankensurf
    web = FakeWeb(dead={"https://shop.example.com/item/1"})
    monkeypatch.setattr(frankensurf, "Runtime", lambda state_dir: web)
    out = tmp_path / "rows.jsonl"
    rows, dead = asyncio.run(sitebench.run([SEARCH], tmp_path / "state", False, out))
    assert web.read_urls[1:] == ["https://shop.example.com/item/1", "https://shop.example.com/item/2",
                                 "https://shop.example.com/item/3"]
    assert [r["url"] for r in dead] == ["https://shop.example.com/item/1"]
    assert len(rows) == 3 and all(r["valid"] for r in rows)
    assert len(out.read_text().splitlines()) == 4


def test_summary_counts_false_successes():
    rows = [{"site": "a", "wall": "w", "kind": "listing", "status": "observed", "valid": False, "invalid_reason": "no_price",
             "failure": None, "latency_ms": 100, "cost_usd": 0},
            {"site": "a", "wall": "w", "kind": "listing", "status": "failed", "valid": False, "invalid_reason": None,
             "failure": "BLOCKED", "latency_ms": 50, "cost_usd": None}]
    overall = sitebench.summarize(rows)["overall"]
    assert overall["false_success"] == 1 and overall["valid"] == 0 and overall["failures"] == ["BLOCKED"]


def test_corpus_is_well_formed():
    corpus = json.loads(sitebench.CORPUS.read_text())["cases"]
    assert len(corpus) >= 20
    for case in corpus:
        assert case["url"].startswith("https://") and case["site"] and case["kind"] in {"listing", "search", "article", "pdf", "json"}
        if case["kind"] == "search":
            assert case.get("follow", {}).get("pattern")
