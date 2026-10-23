"""Compare Frankensurf with a Firecrawl-compatible endpoint on your own URLs.

    frankensurf compare urls.txt                      # against Firecrawl's cloud ($FIRECRAWL_API_KEY)
    frankensurf compare urls.txt --against http://localhost:3003

One URL per line. Add a tab and a phrase the page should contain (a product name, a headline word)
to grade it: a page counts as right only if the phrase is in the text. Without a phrase the
comparison can only say whether a page came back and whether it looks like a block page, so it
lists the URLs where the two disagree for you to look at, rather than grading them with
Frankensurf's own checks.
"""
from __future__ import annotations

import asyncio
import json
import os
import statistics
import time

FIRECRAWL_CLOUD = "https://api.firecrawl.dev"
CONCURRENCY = 4
TIMEOUT_S = 120
MIN_CHARS = 200
_BLOCK_MARKERS = ("just a moment", "attention required", "verify you are human", "are you a robot",
                  "checking your browser", "access denied", "request blocked", "unusual traffic",
                  "pardon our interruption", "enable javascript", "client challenge")


def load_cases(path: str) -> list[dict]:
    cases = []
    with open(path, encoding="utf-8") as source:
        for line in source:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            url, _, expect = line.partition("\t")
            cases.append({"url": url.strip(), "expect": expect.strip() or None})
    if not cases:
        raise ValueError(f"no URLs in {path}")
    return cases


def grade(case: dict, text: str | None) -> str:
    """right / wrong / blocked / empty with a phrase; page / blocked / empty without one."""
    text = text or ""
    if len(text.strip()) < MIN_CHARS:
        return "empty"
    if case["expect"]:
        return "right" if case["expect"].lower() in text.lower() else "wrong"
    head = text[:2000].lower()
    if len(text) < 3000 and any(marker in head for marker in _BLOCK_MARKERS):
        return "blocked"
    return "page"


async def _frankensurf(web, policy, case: dict) -> dict:
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(web.read(case["url"], policy=policy), TIMEOUT_S * 2)
    except Exception as error:  # one URL's failure is that URL's result
        result = {"receipt": {"status": "failed", "failure": {"code": type(error).__name__}}}
    receipt = result.get("receipt") or {}
    text = result.get("text") if receipt.get("status") == "observed" else None
    attempts = receipt.get("attempts") or [{}]
    return {"text": text, "seconds": round(time.monotonic() - started, 1),
            "cost_usd": receipt.get("cost_usd"), "tool": attempts[-1].get("provider"),
            "error": (receipt.get("failure") or {}).get("code")}


async def _endpoint(client, base: str, case: dict) -> dict:
    started = time.monotonic()
    try:
        response = await client.post(f"{base}/v2/scrape", json={"url": case["url"], "formats": ["markdown"]})
        body = response.json()
        data = body.get("data") or {}
        metadata = data.get("metadata") or {}
        return {"text": data.get("markdown"), "seconds": round(time.monotonic() - started, 1),
                "credits": metadata.get("creditsUsed"),
                "error": None if body.get("success") else str(body.get("error") or response.status_code)[:120]}
    except Exception as error:
        return {"text": None, "seconds": round(time.monotonic() - started, 1), "credits": None,
                "error": type(error).__name__}


async def compare(cases: list[dict], *, state_dir, policy, against: str = FIRECRAWL_CLOUD,
                  against_key: str | None = None) -> list[dict]:
    import httpx
    from .runtime import Runtime

    against = against.rstrip("/")
    key = against_key or os.getenv("FIRECRAWL_API_KEY")
    if against == FIRECRAWL_CLOUD and not key:
        raise ValueError("comparing with Firecrawl's cloud needs a key: --against-key or $FIRECRAWL_API_KEY "
                         "(or --against another Firecrawl-compatible URL)")
    headers = {"Authorization": f"Bearer {key or 'local'}"}
    async with Runtime(state_dir=state_dir) as web, httpx.AsyncClient(timeout=TIMEOUT_S, headers=headers) as client:
        return await run_cases(web, client, policy, against, cases)


async def run_cases(web, client, policy, against: str, cases: list[dict]) -> list[dict]:
    """Read each case both ways at once, a few cases at a time."""
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(case):
        async with gate:
            ours, theirs = await asyncio.gather(_frankensurf(web, policy, case), _endpoint(client, against, case))
        for side in (ours, theirs):
            side["chars"] = len(side.get("text") or "")
            side["grade"] = grade(case, side.pop("text"))
        return {"url": case["url"], "expect": case["expect"], "frankensurf": ours, "against": theirs}
    return await asyncio.gather(*(one(case) for case in cases))


_GOOD = {"right", "page"}


def summary(rows: list[dict], against: str) -> str:
    graded = all(row["expect"] for row in rows)
    lines = [f"{len(rows)} URLs, Frankensurf vs {against}"
             + ("" if graded else " (add a tab and an expected phrase to each line to grade pages)")]
    for side, label in (("frankensurf", "Frankensurf"), ("against", "Against")):
        grades = [row[side]["grade"] for row in rows]
        counts = ", ".join(f"{name} {grades.count(name)}" for name in
                           ("right", "wrong", "page", "blocked", "empty") if grades.count(name))
        seconds = sorted(row[side]["seconds"] for row in rows)
        cost = ""
        if side == "frankensurf":
            known = [row[side]["cost_usd"] for row in rows if row[side]["cost_usd"] is not None]
            cost = f", paid tools ${sum(known):.4f}"
        else:
            credits = [row[side]["credits"] for row in rows if row[side]["credits"] is not None]
            cost = f", {sum(credits)} credits" if credits else ""
        lines.append(f"  {label:12s} {counts}; median {statistics.median(seconds)}s, "
                     f"slowest tenth {seconds[int(len(seconds) * .9)]}s{cost}")
    ours_only = [row["url"] for row in rows if row["frankensurf"]["grade"] in _GOOD and row["against"]["grade"] not in _GOOD]
    theirs_only = [row["url"] for row in rows if row["against"]["grade"] in _GOOD and row["frankensurf"]["grade"] not in _GOOD]
    lines.append(f"  Only Frankensurf got a page: {len(ours_only)}; only {against}: {len(theirs_only)}")
    for url in theirs_only[:10]:
        lines.append(f"    {against} only: {url}")
    if not graded:
        lines.append("  Both returned a page that may still be the wrong one; look at the rows (--rows) to be sure.")
    return "\n".join(lines)


def write_rows(rows: list[dict], path: str) -> None:
    with open(path, "w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")
