"""Site benchmark: does FrankenSurf return the right content from real sites?

The scoreboard counts a read as done when it comes back "observed". That let
challenge pages, sign-in redirects and raw PDF bytes count as successes. Here
every case also has a content check for its kind of page:

  listing  enough text and a visible price
  search   at least N same-site links matching the case's listing pattern
  article  enough text (and the case's `expect` pattern, if any)
  pdf      real text, not PDF bytes
  json     a non-empty document (and `expect`, if any)

A search case can `follow` its listing links: the first N live links found on
the page are read as listing cases, so the corpus stays current the way
a resale consumer browses. A followed link that answers NOT_FOUND is a listing that has
gone; it is reported under dead_links and replaced by the next link. Use a fresh --state per run so one run's blocks and route
hints do not skew the next.

  PYTHONPATH=.:src python scripts/sitebench.py --state /tmp/sb-1 [--paid] [--only SITE ...]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

EVALS = Path.home() / ".local/share/frankensurf/evals/sitebench"
CORPUS = Path(__file__).with_name("sitebench-corpus.json")
PRICE = re.compile(r"(?:A\$|AU\$|AUD\s?|\$|€|£)\s?\d[\d,.\s]*|\d[\d\s.,]*\s?(?:€|EUR\b)")
MIN_CHARS = {"listing": 300, "search": 500, "article": 800, "pdf": 100}


def same_site_links(result: dict, base: str, pattern: str | None) -> list[str]:
    """Same-site links in page order, like Runtime.watch collects them."""
    from bs4 import BeautifulSoup
    content = result.get("content") or ""
    if "html" not in (result.get("content_type") or "html"):
        return []
    host = (urlparse(base).hostname or "").removeprefix("www.")
    compiled = re.compile(pattern) if pattern else None
    found = []
    for anchor in BeautifulSoup(content, "html.parser").select("a[href]"):
        href = urljoin(base, anchor["href"]).split("#", 1)[0]
        parsed = urlparse(href)
        if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").removeprefix("www.") != host:
            continue
        if compiled is not None and not compiled.search(href):
            continue
        if href not in found:
            found.append(href)
    return found


def check(case: dict, result: dict) -> tuple[bool, str | None]:
    """Content check for an observed read. Returns (valid, reason when invalid)."""
    kind = case["kind"]
    text = result.get("text") or ""
    if kind == "json":
        data = result.get("structured")
        if not data or (isinstance(data, dict) and not any(data.values())):
            return False, "empty_json"
        haystack = json.dumps(data)[:2_000_000]
    else:
        haystack = text
        if len(text.strip()) < case.get("min_chars", MIN_CHARS.get(kind, 0)):
            return False, "thin"
        if kind == "pdf" and text.lstrip().startswith("%PDF"):
            return False, "pdf_bytes"
        if kind == "listing" and not PRICE.search(text):
            return False, "no_price"
        if kind == "search":
            follow = case.get("follow") or {}
            links = same_site_links(result, result.get("url") or case["url"], follow.get("pattern"))
            if len(links) < follow.get("min_links", 5):
                return False, f"few_links:{len(links)}"
    if case.get("expect") and not re.search(case["expect"], haystack, re.I):
        return False, "expect_missing"
    return True, None


def row_for(case: dict, result: dict, started: float, followed_from: str | None = None) -> dict:
    receipt = result.get("receipt") or {}
    observed = receipt.get("status") == "observed"
    valid, reason = check(case, result) if observed else (False, None)
    routing = receipt.get("routing") or {}
    return {
        "site": case["site"], "wall": case.get("wall"), "kind": case["kind"], "url": case["url"],
        "followed_from": followed_from, "status": receipt.get("status"),
        "failure": (receipt.get("failure") or {}).get("code"), "valid": valid, "invalid_reason": reason,
        "provider": ((receipt.get("attempts") or [{}])[-1]).get("provider"),
        "attempts": len(receipt.get("attempts") or []), "escalated_to": routing.get("escalated_to"),
        "latency_ms": receipt.get("latency_ms") or round((time.monotonic() - started) * 1000),
        "cost_usd": receipt.get("cost_usd"), "text_chars": len(result.get("text") or ""),
        "title": (result.get("title") or "")[:120],
        "observed_at": receipt.get("observed_at"),
    }


async def read_case(web, case: dict, paid: bool) -> tuple[dict, dict]:
    overrides = {"allow_paid_fallbacks": True} if paid else {}
    if case.get("identity"):
        overrides["identity"] = case["identity"]
    started = time.monotonic()
    result = await web.read(case["url"], policy_overrides=overrides or None)
    return result, row_for(case, result, started)


async def run(cases: list[dict], state: Path, paid: bool, out: Path) -> list[dict]:
    from frankensurf import Runtime
    rows, dead = [], []

    def keep(row):
        rows.append(row)
        with out.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, sort_keys=True) + "\n")
        print(json.dumps({k: row[k] for k in ("site", "kind", "status", "valid", "invalid_reason",
                                              "failure", "provider", "latency_ms", "url")}), flush=True)

    async with Runtime(state_dir=state) as web:
        for case in cases:
            result, row = await read_case(web, case, paid)
            keep(row)
            follow = case.get("follow")
            if not follow or row["status"] != "observed":
                continue
            # A listed link can point at a listing that has since gone. That
            # NOT_FOUND is a correct answer, so it is logged as dead_link and
            # the next link is read in its place (at most n extra reads).
            wanted = follow.get("n", 3)
            links = same_site_links(result, result.get("url") or case["url"], follow.get("pattern"))
            for href in links[: wanted * 2]:
                if wanted == 0:
                    break
                child = {"site": case["site"], "wall": case.get("wall"), "kind": follow.get("kind", "listing"),
                         "url": href, "identity": case.get("identity"), "expect": follow.get("expect")}
                _, child_row = await read_case(web, child, paid)
                child_row["followed_from"] = case["url"]
                if child_row["failure"] == "NOT_FOUND":
                    child_row["status"] = "dead_link"
                    dead.append(child_row)
                    keep(child_row)
                    continue
                wanted -= 1
                keep(child_row)
    return [r for r in rows if r["status"] != "dead_link"], dead


def summarize(rows: list[dict]) -> dict:
    def block(group):
        valid = sum(r["valid"] for r in group)
        observed = sum(r["status"] == "observed" for r in group)
        latencies = [r["latency_ms"] for r in group if r["status"] == "observed"]
        costs = [r["cost_usd"] for r in group if r.get("cost_usd") is not None]
        return {"cases": len(group), "observed": observed, "valid": valid,
                "valid_rate": round(valid / len(group), 3) if group else None,
                "false_success": observed - valid,
                "p50_latency_ms": round(statistics.median(latencies)) if latencies else None,
                "cost_usd": round(sum(costs), 4) if costs else 0.0,
                "failures": sorted({r["failure"] for r in group if r["failure"]}),
                "invalid": sorted({r["invalid_reason"] for r in group if r["invalid_reason"]})}
    by = lambda key: {k: block([r for r in rows if r[key] == k]) for k in sorted({r[key] or "-" for r in rows})}
    return {"overall": block(rows), "by_site": by("site"), "by_kind": by("kind"), "by_wall": by("wall")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", type=Path, default=CORPUS)
    parser.add_argument("--state", type=Path, required=True, help="fresh FrankenSurf state directory for this run")
    parser.add_argument("--paid", action="store_true", help="allow paid fallbacks (keys must be set)")
    parser.add_argument("--only", nargs="*", help="limit to these sites")
    parser.add_argument("--label", default="run")
    args = parser.parse_args(argv)
    cases = json.loads(args.corpus.read_text())["cases"]
    if args.only:
        cases = [c for c in cases if c["site"] in set(args.only)]
    EVALS.mkdir(parents=True, exist_ok=True)
    out = EVALS / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + args.label + ".jsonl")
    rows, dead = asyncio.run(run(cases, args.state.expanduser(), args.paid, out))
    print(json.dumps({"rows": str(out), "summary": summarize(rows),
                      "dead_links": [r["url"] for r in dead]}, indent=2))


if __name__ == "__main__":
    main()
