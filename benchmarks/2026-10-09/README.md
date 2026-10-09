# Benchmarks, 9 October 2026: held-out set 4

- `toolbench-heldout4-v0.26.jsonl`: 139 sites chosen before any was read
  (`scripts/toolbench-heldout4.json`), three arms (FrankenSurf with paid
  fallbacks, FrankenSurf free-only, Firecrawl alone), one run on the v0.26.0
  code (bot-check and login-form detection included). 417 rows.
- Carsales was dropped from the list before scoring because it already has a
  route seed, so it is not a fresh site.
- Firecrawl hit its plan's rate limit on 38 pages at 12 workers. Those pages were
  re-run at 2 workers, then one at a time, until none were rate-limited, so the
  Firecrawl arm is a measure of Firecrawl and not of the plan's concurrency.

| Arm | Sites read | Median | p90 | Reported cost |
|---|---|---|---|---|
| FrankenSurf, paid fallbacks | 123/139 (88.5%) | 9.2 s | 49.5 s | $0.027 |
| FrankenSurf, free only | 113/139 (81.3%) | 5.6 s | 50.2 s | $0 |
| Firecrawl alone | 103/139 (74.1%) | 6.6 s | 14.7 s | $0.578 |

127 sites were read by at least one arm; of those FrankenSurf got 123 (96.9%)
and Firecrawl 103 (81.1%). FrankenSurf alone got 22 sites Firecrawl missed;
Firecrawl alone got 2 (LEGO, Gumtree UK). Every news, government, travel, jobs,
media, property, food and cars site passed; the misses are US and AU retail
(potterybarn, qvc, dsw, lego, burlington, cabelas, priceline-au,
templeandwebster, babybunting, dicksmith), prisjakt, gumtree-uk, bax-shop,
readthedocs, Bluesky and Pinterest.

FrankenSurf's slow tail is sequential escalation: an incomplete first page tries
one stronger tool after another. v0.26.0 races free tools in pairs instead (see
`completeness_parallel`).

## Racing the free tools

- `toolbench-heldout4-race-free-v0.26.jsonl`: the free arm again on the same
  139 sites, with free ladder tools racing in pairs (`completeness_parallel`
  2). 114/139 (82.0%) against 113/139; median 4.5 s against 5.6 s; p90 37.2 s
  against 50.2 s. One run each, so a one-site difference is noise; the latency
  drop is not.
