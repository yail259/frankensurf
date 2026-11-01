# Benchmarks, 28 October 2026: held-out set 9 (blind, every tool, paid included)

`scripts/toolbench-heldout9.json` (built by `scripts/make_heldout9.py` from
`scripts/heldout9-candidates.json`): 150 sites in 15 categories, written from general knowledge
before any page was read, leaving out the 4,389 hosts (and their brands) every earlier set used. So
these are less-known, often regional sites, and a harder set than set 7. Home connection, FrankenSurf
0.52.0, every tool read every site on its own.

## Corrections after the first run

My URLs were wrong for 10 sites: a 404 for every tool, Firecrawl's and Zyte's included, or the site
redirecting to a different page (OneRoof sent "Wellington" to Auckland, CARFAX's model code was a
Silverado, Bic Camera's search wants Shift-JIS). Each was replaced by the same page from the site's
own navigation (`was` in the set keeps the original), and every tool re-read it. Reichelt
geo-redirects to English, so its pattern also takes "resistor". Two sites no longer exist and were
dropped: moosejaw.com redirects to a different store, and ingatlannet.hu doesn't answer. 148 sites.

No router change was made before these numbers.

| Arm | Sites read | Median (read) | p90 (read) | Cost | Per 1k read |
|---|---|---|---|---|---|
| **FrankenSurf, paid fallbacks** | **131/148 (88.5%)** | 4.9 s | 33.0 s | $0.13 | $0.96 |
| FrankenSurf, free | 121/148 (81.8%) | 4.8 s | 22.3 s | $0 | |
| Firecrawl alone | 104/148 (70.3%) | 4.3 s | 10.9 s | $0.54 | $5.22 |
| Zyte alone | 104/148 (70.3%) | 9.5 s | 35.1 s | not reported | |
| Jina Reader alone | 101/148 (68.2%) | 3.3 s | 9.9 s | $0 | |
| Scrapling alone | 87/148 (58.8%) | 4.9 s | 8.3 s | $0 | |
| Camoufox alone | 80/148 (54.1%) | 8.6 s | 13.5 s | $0 | |
| Plain HTTP alone | 62/148 (41.9%) | 0.9 s | 2.6 s | $0 | |
| Headless Chromium alone | 58/148 (39.2%) | 2.3 s | 4.8 s | $0 | |
| Patchright alone | 51/148 (34.5%) | 3.2 s | 5.6 s | $0 | |

- 134 sites were read by at least one arm; FrankenSurf with paid fallbacks read 131 of them.
- It read 28 sites Firecrawl missed; Firecrawl read 1 it missed (MUBI, below).
- With free tools only it read 23 sites Firecrawl missed, and missed 6 Firecrawl read.
- Every single tool together (the union of the eight) reads 84.5%. The paid router reads 8 sites no
  tool read on its own, through retries, rescues and the race, and misses 2 that one did.
- Slow tail: the slowest tenth is 33.0 s against Firecrawl's 10.9 s, when a read climbs every tool.

Rows: `toolbench-heldout9-first-run.jsonl` (all 150 sites, every arm),
`toolbench-heldout9-corrected-sites.jsonl` (every arm on the 11 corrected sites; these replace the
first run's rows for them).

## Run to run

Both routers again on all 148, the same evening (`toolbench-heldout9-router-rerun.jsonl`): 87.2%
paid, 79.7% free. Hard sites flicker: Power Buy, allphones and TfL passed once and failed once.

## What's left, and one fix

The 17 misses: about 10 no tool read at all (bot walls on Bauhaus, kbin, Mr Toys, delicious.
magazine, Vivanuncios, Yapo, Academic Positions; app pages that never finish loading on Trakt and
JioMart; Criterion's search, which runs in the page's URL fragment). Two are pages that are there but
don't say the expected word three times (Pixelfed's tag page is pictures; bund.de's new page). The
rest are flaky.

MUBI was a router mistake: every tool's page looked like an unfinished search, and the router kept the
longest one, which was mostly a cookie notice. Between unfinished search pages it now prefers the one
that mentions what was searched for. That change was made after looking at this set, so this set
can't measure it; it's for the next blind set.

ZenRows (plan off) and Scrapfly (credits used up) couldn't be tried.
