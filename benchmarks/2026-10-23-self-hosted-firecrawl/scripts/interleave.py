"""Interleaved A/B: two Firecrawl-compatible servers, each site read by both, the first
reader alternating site by site, so caches (Jina's, the sites' CDNs) favour neither."""
import asyncio, json, sys, time
import httpx

CONCURRENCY = 4


async def scrape(client, base, key, url):
    t = time.monotonic()
    try:
        r = await client.post(f"{base}/v2/scrape", json={"url": url, "formats": ["markdown"]},
                              headers={"Authorization": f"Bearer {key}"})
        body = r.json()
        return {"text": (body.get("data") or {}).get("markdown"), "latency_ms": round((time.monotonic() - t) * 1000),
                "error": None if body.get("success") else str(body.get("error"))[:120]}
    except Exception as e:
        return {"text": None, "latency_ms": round((time.monotonic() - t) * 1000), "error": type(e).__name__}


async def main(cases_path, out_a, out_b, base_a, base_b, key):
    cases = json.load(open(cases_path))["cases"]
    gate = asyncio.Semaphore(CONCURRENCY)
    rows_a, rows_b = [], []
    async with httpx.AsyncClient(timeout=120) as client:
        async def one(i, c):
            async with gate:
                order = [("a", base_a), ("b", base_b)] if i % 2 == 0 else [("b", base_b), ("a", base_a)]
                for arm, base in order:
                    row = {"site": c["site"], "url": c["url"], "first": order[0][0] == arm,
                           **await scrape(client, base, key, c["url"])}
                    (rows_a if arm == "a" else rows_b).append(row)
        await asyncio.gather(*(one(i, c) for i, c in enumerate(cases)))
    for path, rows in ((out_a, rows_a), (out_b, rows_b)):
        with open(path, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
    print("done", len(rows_a), len(rows_b))


asyncio.run(main(*sys.argv[1:7]))
