# Self-hosted Firecrawl vs Frankensurf's Docker image (23 October 2026)

Firecrawl is open source, so the fair question for anyone running their own is: against the
Firecrawl you'd self-host, not Firecrawl's cloud, how many pages does each read?

- **Firecrawl:** v2.11.604 (`ghcr.io/firecrawl/firecrawl:2.11.604`), set up the way its `SELF_HOST.md`
  says for a first run: the root `docker-compose.yaml`, `USE_DB_AUTHENTICATION=false`, bundled
  Playwright with the basic fetch fallback, no proxy, no Fire-engine (Firecrawl's docs: self-hosted
  instances don't have access to it).
- **Frankensurf:** `ghcr.io/yail259/frankensurf:latest` (0.51.0), `docker run --shm-size=1g`, free
  tools only.
- **Sites:** held-out set 7, 150 sites chosen before any was read (`scripts/toolbench-heldout7.json`).
- **Same client for both** (`scripts/scrape.py`): `POST /v2/scrape` with `formats: ["markdown"]`, four
  pages at a time, 120 s timeout, one run each, both servers on the same machine and connection
  (a home machine; the connection exited through AS136557 Host Universal at the time).
- **Graded** by toolbench's check (`scripts/grade.py`, as for the GPT Researcher benchmark): the
  markdown must hold what the page should, at least three times.

| Server | Right page | Wrong page passed on | No page | Median | Slowest tenth |
| --- | --- | --- | --- | --- | --- |
| Firecrawl 2.11.604, self-hosted | 89 (59.3%) | 20 | 41 | 6.0 s | 16.6 s |
| Frankensurf 0.51.0, Docker | 128 (85.3%) | 6 | 16 | 9.8 s | 33.6 s |

- Frankensurf read 39 sites self-hosted Firecrawl didn't, and Firecrawl read none that Frankensurf
  didn't.
- Firecrawl's 41 empty results were nearly all "All scraping engines failed to retrieve content"
  (37); its 20 wrong pages were mostly thin pages (15: a shell or a challenge page passed on as the page).
- The 39 sites spread across every kind of site in the set: shops in Europe, the US and elsewhere,
  food, cars, social, travel, jobs, media and government.
- Frankensurf is slower: it climbs to a browser or stealth browser when the cheap tools fail.
- For reference, Firecrawl's cloud, with Fire-engine, read 84.7% of the same set on 11 October
  (`docs/more/benchmarks`), and Frankensurf's free tools 88.0% that day. Self-hosting loses most of
  what Firecrawl's paid layer adds; Frankensurf's free tools get about as far as the cloud.

Rerun (from the repo root, with both servers up):

```bash
python benchmarks/2026-10-23-self-hosted-firecrawl/scripts/scrape.py scripts/toolbench-heldout7.json   benchmarks/2026-10-23-self-hosted-firecrawl/firecrawl-self-hosted.jsonl http://127.0.0.1:3003
python benchmarks/2026-10-23-self-hosted-firecrawl/scripts/scrape.py scripts/toolbench-heldout7.json   benchmarks/2026-10-23-self-hosted-firecrawl/frankensurf-docker.jsonl http://127.0.0.1:3002 <key>
python benchmarks/2026-10-23-self-hosted-firecrawl/scripts/grade.py   fc=benchmarks/2026-10-23-self-hosted-firecrawl/firecrawl-self-hosted.jsonl   fs=benchmarks/2026-10-23-self-hosted-firecrawl/frankensurf-docker.jsonl
```
