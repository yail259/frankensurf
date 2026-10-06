# Benchmarks, 6 October 2026

- `toolbench-all-arms.jsonl`: every arm (seven tools alone, stitched free and
  stitched paid) on the 82 frozen site-benchmark URLs and held-out list 1,
  v0.13.0 code. 945 rows.
- `toolbench-heldout-v0.14.jsonl`: both held-out lists, v0.14.0 code (second
  opinion), ZenRows not run. 360 rows.
- `sitebench-paid-v0.14.jsonl`: the paid site benchmark on v0.14.0, 82/82 valid.
- `summary.json`: the figures quoted in README.rst, on the site and in posts,
  computed from these rows.

One row per read: arm, set, site, wall label (public reputation, not vendor
detection), URL, status, whether the content check passed and why not, final
provider, latency and reported cost.

## v0.16.0: completeness escalation, 152 fresh sites

- `toolbench-heldout3-v0.16.jsonl`: 152 sites chosen before any was read
  (`scripts/toolbench-heldout3.json`), 13 arms, one run. Scrapfly's free plan ran
  out of credits after about 40 rendered reads (1,000 credits), so its arm is not
  a measure of Scrapfly.
- `toolbench-dev1-v0.16.jsonl`, `toolbench-dev2-v0.16.jsonl`: the development
  runs on the already-used sets, while the completeness check was designed.
- `summary-heldout3-v0.16.json`: per-arm figures.

On the 152 fresh sites: FrankenSurf with paid fallbacks 89.5% (136), Firecrawl
alone 80.3% (122), Zyte 73.7%, FrankenSurf free-only 73.7%, Jina Reader 63.8%.
146 sites were read correctly by at least one arm; of those, FrankenSurf got
93.2% and Firecrawl 83.6%. FrankenSurf alone got 19 sites Firecrawl missed;
Firecrawl alone got 5. Cost: $0.10 for FrankenSurf's paid reads against $0.53
for Firecrawl (some reads report no cost). Median latency 9.5 s against 5.6 s.
The agent-with-retry arm (a model judges each page, up to two retries) scored
88.8%: no gain over the router alone, at twice the latency.

## v0.19.0 spot checks (scripts/spotcheck.py, router with paid fallbacks)

- `spot/v0.19-heldout3-misses.jsonl`: the 11 held-out-3 sites still missed after
  v0.18; 3 now read (Kayak, Ziprecruiter, and Arstechnica, whose grading was
  wrong: by hand the Tech page is correct but rarely says "gadget"). In-sample
  estimate for held-out 3: 144/152.
- `spot/v0.19-fresh-56.jsonl`: 56 sites never used before (`fresh-56-cases.json`):
  49 read (87.5%). Misses: hard anti-bot walls (Shopee, Idealista, The
  Economist), results that never render (Northern Tool), two URLs that answered
  NOT_FOUND (Walmart Mexico, The RealReal; possibly wrong URLs) and Lululemon.
  With the 18-site check under v0.18 (16/18): 65 of 74 fresh sites, 88%.

## v0.20.0 spot check: longer render waits on escalation

- Escalation reads settle at least 5 s (`completeness_settle_ms`); Firecrawl gets
  `waitFor` and Zyte a `waitForTimeout` action.
- Fresh 56 with the two dead URLs rotated (Walmart Mexico to eBay Canada, The
  RealReal to Costco UK): 50/56 (89.3%), against 49/56 before; within run-to-run
  noise (Menards and Patagonia passed before and failed this time). Rows:
  `spot/v0.20-fresh-56-rotated.jsonl`.
- The four sites whose results never appeared (Bing Lee, Leroy Merlin, Boots,
  Northern Tool): still 0/4 (`spot/v0.20-slow-render.jsonl`). Three never
  escalated, because the structure check accepted menu links as items; Northern
  Tool failed on every tool. The gap is detection and hard walls, not waiting.
