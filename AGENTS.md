# Agent instructions for FrankenSurf

Read `SPEC.rst` (revision 0.3) before changing code. It supersedes earlier
conservative non-goals. Key rules:

- **Capability first.** Maximize correct task completion and evidence quality,
  then cost and latency.
- **No hard-coded ceilings.** Every limit (stop-on-block, provider allowlists,
  budgets, retries, solvers, proxies, paid providers, action classes) belongs in
  `WebPolicy` with a default. Do not add new hard-coded stops or allowlists; when
  touching existing ones, move them into policy.
- **Everything is a plugin.** New acquisition methods, search sources, site
  adapters and middleware are plugins behind the interfaces in SPEC section 7.
  Do not add new provider branches inside `Runtime.read`.
- **Per-site preferences are seeds.** Logic like `experimental.public_preference`
  must become route-memory or capability-graph data (SPEC section 10).
- **Protect the user by default.** Secrets never reach the agent or model;
  identity authority rules in SPEC sections 11, 12 and 23 still hold for
  identities. Profiles (owner decision, 2026-10-06) are FrankenSurf's own
  encrypted copy of a login and may be carried by any provider their sharing
  level allows, scoped to their sites; agents still see only profile names.
- **A path is real only end to end.** Catalog registration and parser fixtures
  prove contracts, not site access. Count a workload only when Core acquisition
  produces the adapter input and the site benchmark verifies its live content and
  retained evidence.
- **Execution creates evidence; schemas only check it.** A gate may pass only from
  retained observations produced by a runner that actually invoked the claimed
  client, model, installer, provider and workload. Self-authored attestations and
  synthetic fixtures can test rejection and contracts, but cannot qualify a gate.
- **Acquisition claims come from Core.** Adapters project acquired input; they
  cannot assert navigation, identity, freshness, scope or coverage unless Core
  passes an attestation from the exact acquisition into the adapter request.
- **The benchmark decides.** Routing defaults and release readiness follow
  site benchmark results (`scripts/sitebench.py`). Never report unmeasured numbers
  as measured.

- **Polite pacing; unblockers allowed.** Standing rule from the owner
  (2026-10-04, revised 2026-10-05): reads are paced per origin
  (`origin_min_interval_seconds`) and a block, challenge or rate limit pauses
  direct reads of that origin (`origin_cooldown_seconds`). Unblocker services
  (ZenRows, Scrapfly, Bright Data Web Unlocker, and others stitched in as
  plugins) are allowed as the paid last rung, as the landing page shows. They
  run only with their key configured and `allow_paid_fallbacks`, and an
  explicit unblocker read is not held by the cool-down. Prefer a few polite
  polls over bulk sweeps either way.
- **Substrate, not consumer.** FrankenSurf serves any agent. Resale formats,
  sold comps, valuation and site packs belong in consumer projects.
  Do not add new site-specific Python modules; per-site knowledge is route-seed
  or field-map data, or a site module (`site_modules.py`) that callers save in
  their own state directory. No site module ships in this repo; tests use
  synthetic fixtures. Consumers pin tagged releases.

The SPEC section 22 milestones
(H benchmark harness, P plugins and policy, M route memory) still apply.

The marketing site and docs live in `site/` (Astro + Starlight). Keep docs in
`site/src/content/docs/docs/` consistent with SPEC.rst and the code.

Landing page copy (`site/src/pages/index.astro`, `site/src/data/site.ts`) is the
product promise and follows the maintainer's design direction:

- Keep the promise uncaveated: "Every site. Every model. One call."
- Mark unmeasured numbers as goals by pairing them with today's figure (the
  "today / goal" key in the targets section), or, where no today figure exists,
  with a small `goal` tag. Say it once per group, never per row. Shipped
  behaviour carries no label. Do not add disclaimer lines, eyebrow labels, side
  paragraphs or long caveat sentences to the landing page.
- Less is more: no word repeated in every row (put shared labels in one key),
  no paragraph that restates its heading, and a small graphic over a sentence.
- Put detail and limitations in the docs, not on the landing page.
- Do not change the landing page layout or visual style. Content-only updates
  that reflect new measurements or shipped features are welcome.
