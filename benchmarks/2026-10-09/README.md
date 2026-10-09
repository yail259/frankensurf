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

## Hedging slow reads

- `toolbench-heldout4-hedge-free.jsonl`: the free arm on the same 139 sites with
  racing plus the hedge (`hedge_after_seconds` 10: a slow read gets one extra
  read pinned to the first free ladder tool). 112/139 against 114/139 (noise
  at this size: Bloomingdale's and MediaWorld were walled this run, Bax-shop
  read). On successful reads: median 4.5 s (unchanged), p90 24.5 s against
  37.2 s, mean 8.9 s against 11.6 s. Michaels 50 s to 17 s, Naukri 57 s to
  13 s, Anthropologie 49 s to 16 s.
- This set has now been used three times, so these runs measure speed, not
  fresh-site accuracy.

## Articles: main content and the quality grade

- `articles-108.json`: 108 news and company-news article URLs, one per site,
  taken from Bing News results for 40 business queries (`scripts/article_eval.py
  collect`). Read with free tools and `main_content` on.
- `articles-108-v0.28.jsonl` (v0.28.0) and `articles-108-quality.jsonl` (the
  article check, quality grade and paywall flag): article present (600+
  characters and 2+ prose paragraphs of main text) on 103 and 105 pages; none
  lost; the grade agreed with article presence on 107 of 108 (the exception
  is a winery directory page, which is not an article). Median read 3.3 s
  against 4.0 s: thin article pages now escalate.

## From a server (Azure VM, GitHub Actions)

`.github/workflows/server-bench.yml` runs the free arm and the article sample on a
fresh GitHub-hosted Ubuntu VM in Azure: a data-centre address, which sites
refuse more often than a home connection.

- `toolbench-heldout4-free-azure.jsonl`: 98/139 (70.5%), against 112/139 at home
  (same code, free tools). Lost on the server: retailers and marketplaces behind
  bot walls, and image and social sites; plain HTTP that worked at home was
  refused.
- `toolbench-heldout4-free-azure-steel.jsonl`: with the self-hosted Steel
  browser running beside it (as docker-compose.yml does), 104/139 (74.8%).
- `articles-108-azure-steel.jsonl`: articles read 103/108, article present
  100/108, against 105 at home.

The rest of the gap is the data-centre address; FRANKENSURF_PROXY routes the
local tools through a proxy the owner brings (not measured here).

## Watching many sites (watch_sites)

The 108 sites behind `articles-108.json`, watched from a home connection
with `watch_sites` (see the Watch docs). First poll, 2-day window: 1,004 requests, 14
minutes, 2,301 new pages on 77 sites; 51 sites had feeds, 43 only sitemaps,
14 neither. A poll straight after reported 247 (undated sitemap pages, now
treated as baseline), and the next one 43, all dated within minutes of the
poll: real new or changed pages. A repeat poll costs about 370 small requests
for 108 sites (84 answered 304 Not Modified) and under 4 minutes.

## Walls: warm-up retry and site sessions

- `toolbench-heldout4-free-home-walls.jsonl` (home) and
  `toolbench-heldout4-free-azure-steel-walls.jsonl` (Azure VM with Steel): the
  free arm with the warm-up retry for walled deep links and the interact step.
  Home 110/139 (112 before), server 103/139 (104 before): within run-to-run
  noise. The warm-up retry recovered the same walled sites in both runs
  (Bloomingdale's, OzBargain; Darty and Homes.com on the server); other sites
  came and went between runs.
