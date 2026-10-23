"""Read every held-out set 7 URL through a Firecrawl-compatible /v2/scrape endpoint.

The same client, request and timeout for every server, so the only difference is what answers:
    python scrape.py <cases.json> <out.jsonl> <base url> [key]
"""
import asyncio, json, sys, time

import httpx

CONCURRENCY = 4
TIMEOUT_S = 120


async def one(client, base, case):
    t = time.monotonic()
    try:
        r = await client.post(f"{base}/v2/scrape", json={"url": case["url"], "formats": ["markdown"]})
        body = r.json()
        data = body.get("data") or {}
        return {"site": case["site"], "url": case["url"], "status": r.status_code,
                "latency_ms": round((time.monotonic() - t) * 1000),
                "text": data.get("markdown"), "error": None if body.get("success") else body.get("error")}
    except Exception as e:
        return {"site": case["site"], "url": case["url"], "status": None,
                "latency_ms": round((time.monotonic() - t) * 1000), "text": None, "error": type(e).__name__}


async def main(cases_path, out, base, key):
    cases = json.load(open(cases_path))["cases"]
    sem = asyncio.Semaphore(CONCURRENCY)
    headers = {"Authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(timeout=TIMEOUT_S, headers=headers) as client:
        async def guarded(c):
            async with sem:
                return await one(client, base, c)
        rows = await asyncio.gather(*(guarded(c) for c in cases))
    with open(out, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(out, len(rows), "with text", sum(1 for r in rows if r.get("text")))


asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3].rstrip("/"), sys.argv[4] if len(sys.argv) > 4 else "local"))
