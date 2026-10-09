# Benchmarks, 10 October 2026: held-out set 5

- `scripts/toolbench-heldout5.json` (built by `scripts/make_heldout5.py`): 150
  sites chosen from general knowledge before any of their pages was read. Every
  host and every brand on an earlier set or spot check (519 hosts) was excluded
  automatically, and the 150 were taken round-robin across 15 categories (US,
  AU/NZ, EU and other retail, classifieds, jobs, property, travel, news,
  government, media, food, cars, developer sites and social).
- Some URLs were guessed wrong (Adore Beauty, Cotswold, Seat61 and Lastminute
  answer 404 to every tool, including after their walls); that counts against
  every arm alike.

## First run (blind), v0.30.0 code

`toolbench-heldout5-v0.30.jsonl` (home connection) and
`toolbench-heldout5-free-azure-v0.30.jsonl` (free arm on an Azure VM):

| Arm | Sites read | Median | p90 | Reported cost |
|---|---|---|---|---|
| FrankenSurf, paid fallbacks | 130/150 (86.7%) | 5.1 s | 59.6 s | $0.068 |
| FrankenSurf, free only | 120/150 (80.0%) | 4.9 s | 24.1 s | $0 |
| FrankenSurf, free only, Azure VM | 115/150 (76.7%) | | | $0 |
| Firecrawl alone | 113/150 (75.3%) | 4.3 s | 8.5 s | $0.555 |

131 sites were read by at least one arm; FrankenSurf with paid fallbacks got
130 of them (99.2%), Firecrawl 113 (86.3%). FrankenSurf alone read 18 sites
Firecrawl missed; Firecrawl alone read 1. Firecrawl's single rate-limited page
was re-run one at a time.

Every news, government, property, media and developer site passed. The misses
were wrong URLs (above), bot walls (mobile.de, Avito, Bass Pro, Strava's
sign-in), and search pages that came back without their results.

After this run the set is no longer blind; later runs on it measure changes.
