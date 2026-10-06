Frankensurf
===========

**Every site. Every agent. One call.**

Frankensurf is an open-source web layer for AI agents. Ask for a URL or a
search, and it picks the right browsing tool for that page, climbs to a stronger
one when the site pushes back, and returns clean page data with a receipt of how
it got there.

- **Every site.** Plain HTTP, real browsers, stealth browsers, unblockers and, as
  a last resort, a person. Six rungs behind one call.
- **Every agent.** The same tools and results on any model, through MCP, Python
  or the command line.
- **Shows its work.** Every result says which tools ran, how long each took and
  what it cost.

Website and docs: https://frankensurf.pages.dev/docs/

Install
-------

Python 3.12 or newer. Linux, or Windows through WSL (macOS should work but
isn't tested yet)::

  git clone https://github.com/yail259/frankensurf.git
  cd frankensurf
  python3 -m venv .venv
  .venv/bin/pip install -e '.[mcp]'
  .venv/bin/playwright install chromium
  .venv/bin/frankensurf read https://example.com

No account or key needed. See `Install <https://frankensurf.pages.dev/docs/install/>`_
for the optional tools (local search, Steel, stealth browsers, Crawl4AI, paid
services).

Use it
------

From an agent, as an MCP server
(`setup for each client <https://frankensurf.pages.dev/docs/agents/>`_)::

  claude mcp add frankensurf -e FRANKENSURF_STATE=$PWD/state -- $PWD/.venv/bin/frankensurf-mcp

From Python::

  from frankensurf import Runtime

  async with Runtime("state") as web:
      page = await web.read("https://example.com", policy_overrides={"prefer_markdown": True})
      hits = await web.search("playwright python tutorial")

From the command line::

  frankensurf read https://example.com --markdown
  frankensurf search playwright python tutorial
  frankensurf watch https://news.ycombinator.com/ --link-pattern 'item\?id=\d+'

What you get back is raw page data, not answers: text or markdown, JSON-LD, the
page's embedded and fetched JSON, images, PDFs as text, and a receipt. Your agent
decides what the page means.

How it works
------------

Every way to fetch a page is a rung: markdown negotiation and plain HTTP,
browsers, stealth browsers and signed requests, paid unblockers, and finally a
person clearing the wall in a visible browser. A read starts at the cheapest rung
and climbs only when a page pushes back; app shells, challenge pages and empty
renders count as failures, not thin successes. Route memory and per-site hints
make repeat visits fast. See
`How escalation works <https://frankensurf.pages.dev/docs/blocked/escalation/>`_
and the `full list of integrations <https://frankensurf.pages.dev/docs/integrations/>`_.

Benchmarks
----------

``scripts/toolbench.py`` runs each provider on its own, then the stitched router,
on the same pages with the same content checks. Results from 6 October 2026,
with raw rows in ``benchmarks/2026-10-06/``:

=============================  ==========  ===========  ======================
Arm                            Tuned (78)  Unseen (45)  $ per 1k valid, unseen
=============================  ==========  ===========  ======================
Headless Chromium Only         32.1%       15.6%        $0
Plain HTTP Only                43.6%       33.3%        $0
Jina Reader Only               35.9%       51.1%        $0
Camoufox Only                  60.3%       48.9%        $0
Scrapling Only                 61.5%       60.0%        $0
Firecrawl Only                 88.5%       73.3%        $4.61
Frankensurf, free tools only   89.7%       53.3%        $0
Frankensurf                    98.7%       71.1%        $1.07
=============================  ==========  ===========  ======================

"Tuned" pages are the ones route seeds were built on; "unseen" sites were never
tuned on. On unseen sites Frankensurf lands two points behind Firecrawl Only at a
quarter of the cost, with roughly twice the median latency (6.6 s against
2.8 s). ZenRows is left out: the plan's rate limit cut its run short. See
`Benchmarks <https://frankensurf.pages.dev/docs/more/benchmarks/>`_.

Develop
-------

::

  .venv/bin/pip install -e '.[test,mcp]'
  PYTHONPATH=.:src .venv/bin/pytest -q

The product design is in ``SPEC.rst``; contributor rules are in ``AGENTS.md``.
New tools are plugins, never branches in Core; see
`Writing a plugin <https://frankensurf.pages.dev/docs/more/plugins/>`_. The
website and docs live in ``site/``.
