FrankenSurf product and architecture specification
==================================================

Status: local MVP implemented; capability-first architecture specified

.. note:: Revision note, 2026-10-06 (v0.17.0, owner decision). Profiles add a
   FrankenSurf-owned BrowserProfileStore (section 12): an encrypted, versioned,
   site-scoped session with a pinned fingerprint, which any provider allowed by
   its sharing level (local or hosted) may carry. The rule that sessions never
   leave the owner's browser now applies to identities only. Agents still
   receive opaque names, never profile bytes, cookies or tokens.

.. note:: Revision note, 2026-10-06 (v0.15.0). Every fetch and search tool the
   landing page names is now a provider or search source, except Stagehand and
   Chrome DevTools MCP, which drive browsers for agents and pair with FrankenSurf.

.. note:: Revision note, 2026-10-06 (v0.14.0). ``scripts/toolbench.py`` measures
   the stitched router against each tool alone on tuned and held-out sites; see
   README.rst. Automatic plain-HTTP pages that may be missing rendered content
   get a rendered second opinion.

.. note:: Revision note, 2026-10-05 (v0.13.0). All six escalation rungs of the
   landing page now exist: T2 hosted managed browsers, T3 Web Bot Auth request
   signing and T5 human handoff joined T0, T1 and T4. Route memory learns from
   observations that passed Core's content checks.

.. note:: Revision note, 2026-10-05 (v0.12.0). FrankenBench (section 21), its
   manifests and release gates, adapter contracts and semantic-contract
   certification were retired with ``scripts/frankenbench.py``. Content quality
   is measured by ``scripts/sitebench.py``; sections that describe them are
   historical.

Date: 2026-10-04
Revision: 0.3 -- Capability first, extensible everything, release gates

This specification defines the target product. README.rst describes the current
runtime. Requirements below do not imply that a feature already exists.

Revision 0.3 replaces 0.2's conservative non-goals. Earlier text that stopped on
blocks, excluded CAPTCHA solving, refused paid fallbacks or disclaimed anti-bot
capability is superseded: those are now configurable policies with defaults, not
product ceilings. Implementations must not hard-code a limit that this
specification describes as policy.

1. Promise
----------

Every site. Every model. One call.

FrankenSurf gives any agent, on any model, one web interface that reaches the
content or completes the operation it asks for, by composing the strongest
available tools: search APIs, official APIs, structured endpoints, HTTP, crawlers,
JavaScript runtimes, managed and stealth browsers, unblocker networks, proxies,
solvers, the user's own authenticated Chrome, specialist data providers, vision
models and compiled adapters.

FrankenSurf does not reinvent those tools. Its durable value is the layer above
them: routing, route memory, plugins, identity authority, evidence, compilation,
repair, benchmarking and observability. When a better tool appears, it is bolted
on as a plugin and earns its place on the benchmark.

2. Principles
-------------

* Capability is the goal. Maximize correct task completion and evidence quality,
  then optimize cost and latency.
* Defaults set the floor; nothing caps the ceiling. Every limit (escalation,
  providers, budget, retries, solvers, proxies, identity use, action class) is a
  policy value the user can change. No capability is removed by hard-coding.
* Extensible everything. Providers, search sources, adapters, hooks, identity and
  vault backends are plugins behind stable interfaces. A savvy user can make
  FrankenSurf do anything their own machine and accounts can do.
* Protect the user by default. Secrets never reach the agent or model; credentials
  stay scoped; personal identities default to LOCAL_ONLY. These are user
  protections, enabled by default and changeable only by explicit owner policy.
* Honest evidence. Results carry receipts, freshness and typed outcomes. Unknown
  stays unknown. Unmeasured numbers are never reported as measured.
* Deterministic known paths, intelligence at the edges. LLMs discover, plan and
  repair; deterministic code executes, verifies, caches and scales.
* The benchmark decides. Provider choice, routing defaults and release readiness
  follow measured results, not preference.
* Local and open. Local Core is fully useful without any hosted service.

3. Local Core is authoritative
------------------------------

Local Core owns the effective policy, plugin registry, router and route memory,
identities and vault bindings, adapter registry, queue, evidence store, cache,
capability observations, verification and local traces. Its API/MCP works without
FrankenSurf Cloud, a Cloud account, licensing or a hosted database.

The target is that the large majority of useful operations (initially 70--80% of
the weighted benchmark workload, rising over time) succeed with Local Core plus
locally installed plugins. Hosted third-party providers a user configures (an
unblocker API, a proxy pool) count as local plugins; they do not require
FrankenSurf Cloud. Web retrieval still needs network access to its source.

4. Cloud supplies scale, never authority
----------------------------------------

Optional FrankenSurf Cloud may supply distributed workers, shared capability
intelligence, hosted observability, team administration, managed providers,
identity convenience, unified billing and repair coordination.

Cloud never replaces Local Core as authority for a local deployment. It can suggest
routes, plugins or adapters and request work; Local Core rechecks policy before
execution. Shared statistics and remote receipts are inputs with provenance, not
permission grants. Local Core keeps working when Cloud is disconnected.

5. Product boundary
-------------------

FrankenSurf owns the unified API/MCP, policy engine, plugin system, router, route
memory, capability graph, identity authority, vault abstraction, queue,
evidence/cache, adapter registry, validation, traces, repair and the benchmark
harness.

It does not need to own Chromium, proxies, CAPTCHA infrastructure, unblocker
networks, search indexes, frontier models or browser fleets. It integrates all of
them as plugins, and ships first-party plugins wherever a strong free or open
option exists. WTB lives in Trade as sourcing and watch logic; Trade owns
valuation, margin, portfolio, negotiation and trading decisions.

6. Agent and operator API
-------------------------

Agent surface::

  web.search(query, policy)
  web.read(url, policy)
  web.extract(url, schema, policy)
  web.paginate(url, adapter, policy=None, *,
               continuation_adapter=None)
  web.do(intent, policy)
  web.batch(operations, policy)
  web.watch(operation, refresh_policy, policy)

Search handles source selection and fuzzy discovery. Read returns the strongest
representation reachable under policy. Extract validates a schema. Paginate
follows a policy-bounded continuation route through either a concrete adapter or
a caller-neutral adapter contract. In contract mode every committed page is an
ordinary resolved extract with its own evidence and replay chain, and the
aggregate receipt binds page order and termination. Do performs actions within
the policy's action classes. Batch streams partial results. Watch maintains
versioned observations.

Operator surface: explain/trace/retry, capabilities, plugins (list/install-check/
enable/disable/test), routes (inspect/pin/forget), identity status/enrollment/
replica/revocation, adapter inspect/test/repair, benchmark run/report and worker
health. Inspection never exposes secrets.

The agent surface is model-agnostic. Every capability is reachable through the
Python API, the CLI and MCP with identical semantics and receipts.

7. Plugin architecture
----------------------

Every replaceable component is a plugin behind a typed interface:

ProviderPlugin
  Acquires content: HTTP clients, browsers (local, managed, stealth, vision),
  unblocker and scraping APIs, the user's own Chrome, specialist data APIs.
  Declares a capability manifest: operations (read/extract/do), rendering,
  authentication support, stealth/fingerprint class, proxy/geography support,
  cost model, concurrency limits, required configuration and dependencies.

SearchPlugin
  Discovery sources: self-hosted SearXNG, public engines, search APIs.

AdapterPlugin
  Site- or format-specific extraction and validation (schema, invariants,
  galleries, pagination, listing-state claims). Hand-written, generated or
  compiled adapters share one contract.

Hook
  Middleware at defined points: before routing, before request, after response,
  on failure, before export to the agent. Hooks implement proxy rotation, header
  and fingerprint shaping, challenge solving, Markdown negotiation, request
  signing (for example Web Bot Auth), content distillation and custom parsing
  without modifying Core.

IdentityBackend and VaultBackend
  Where canonical browser state and secrets live: local profile, OS keychain,
  managed provider, team vault.

Contract
  Plugins return typed outcomes (section 18), never raw exceptions, and report
  attempts, latency, measured cost and evidence references for receipts. A plugin
  may run in-process or in an isolated worker process with its own environment
  (the current provider_worker pattern) when its dependencies conflict with Core.

Registration and trust
  Plugins register through Python entry points or a local configuration file.
  Core never downloads or installs a plugin during an operation. Installed plugins
  run with the trust the user grants them; secret access is scoped by the vault
  binding, not by plugin identity alone. First-party plugins ship in the repo; any
  plugin is benchmarked on the same harness before becoming a routing default.

8. Effective policy
-------------------

WebPolicy is the single place limits live. It includes quality (best/balanced/
cheap), freshness (now/hour/day/cached), evidence (required/preferred/none),
identity, action classes, providers allow/deny/prefer, plugin and hook selection,
escalation (on_block: escalate|stop, max_attempts, max_rungs), budget (max_cost,
max_latency, max_tokens), allow_local_browser, allow_paid_providers, solvers,
proxies and geography.

Defaults are capability-seeking within a zero or near-zero cost budget: escalate
through every installed free plugin until success or exhaustion. Paid plugins run
when the user enables them or raises the budget.

Precedence: owner policy, then operation policy, then defaults, then learned route
preferences. Caller overrides can change any value the owner policy permits.
Provider output and model prompts can never change policy. Missing credentials,
exhausted budgets and disabled plugins are typed outcomes with the exact policy
change that would unlock them.

9. Routing and escalation
-------------------------

Candidate rungs, ordered by expected cost and filtered by policy and the capability
graph::

  R0  fresh scoped cache / evidence
  R1  official API / structured feed / discovered JSON endpoint
  R2  Markdown negotiation (Accept: text/markdown) and signed-agent requests
  R3  direct HTTP with adapter extraction
  R4  lightweight JavaScript runtime
  R5  local or self-hosted Chromium (for example Steel)
  R6  managed cloud browser
  R7  stealth browser (for example Camoufox, Scrapling/Patchright)
  R8  unblocker network / scraping API, proxies, solvers
  R9  the user's authenticated local Chrome or approved replica
  R10 vision / frontier browser agent
  R11 operator handoff (2FA, consent, confirmation)

This is a candidate set, not a compulsory ladder. The router starts at the rung
route memory predicts will succeed, skips rungs that cannot satisfy the operation
(authentication, location, visual requirements) and escalates on failure. A block,
challenge or rate limit is a routing signal: by default the router escalates to a
stronger rung and records the outcome; stop-on-block is a policy choice.
Authentication and identity requirements never silently disappear during fallback.

10. Route memory and capability graph
-------------------------------------

Every attempt records domain, operation, path pattern, identity class, provider and
version, rung, outcome, correctness checks, latency, measured cost, tokens and
time. The router reads these records:

* Route memory: per domain and path pattern, the cheapest rung and provider that
  recently succeeded. The next request starts there. Entries expire and are
  re-probed when they start failing or when a cheaper rung may have recovered.
* Capability graph: aggregate success, cost and latency per provider and site
  class, with sample counts. Unknown confidence stays unknown; a small sample is
  not a reliability estimate.

Hard-coded per-site preferences are temporary seeds. Each must be expressible as
route-memory or capability-graph data and replaced by learned routing once the
graph has enough samples.

Local records are authoritative for local experience. Shared Cloud records carry
provenance and are hints until validated locally. Cookies, secrets and personal
identity labels are never uploaded as capability intelligence.

11. Identity authority
----------------------

LOCAL_ONLY
  Canonical browser state and vault authority stay on an enrolled local machine.
  Default for personal authenticated identities.

SYNC_ALLOWED
  One canonical authority issues encrypted, scoped, versioned profile snapshots to
  named execution replicas the owner approves.

CLOUD_MANAGED
  An owner-selected cloud or provider authority owns canonical browser state.

EPHEMERAL
  A short-lived identity with no reusable canonical profile.

The owner can choose any mode for any identity; changing mode or exporting a
profile is an explicit, audited owner action, never an automatic fallback. The
registry binds identity ID, canonical owner, profile reference/version, domain
scopes, approved executors/providers, credential bindings, export policy,
network/geography, health, last validation and replica lineage. Agents receive
opaque references, never profile bytes, cookies, tokens or passwords.

Resolve identity and enforce policy before cache lookup, routing and every
fallback. An authenticated operation never silently substitutes an anonymous
page, a different account or a cached claim; the router may instead use any other
executor the identity's policy approves.

12. Vault, browser state and execution
--------------------------------------

CredentialVault is separate from BrowserProfileStore. Vault references resolve
inside the executor or plugin boundary, with scoped access, rotation, revocation
and audit. Profile authority does not imply credential export, and vice versa.
Logs, receipts, errors, capability records, screenshots and exports are redacted.

Remote local execution uses an outbound, authenticated worker channel with signed,
expiring, replay-protected job envelopes; the worker rechecks local policy and
acquires a lease before execution. Leases bind owner/executor, profile version,
network/geography, TTL and fencing epoch; the same identity does not run from
inconsistent contexts at once. Replicas follow requested -> approved -> created ->
active -> expired/revoked, never merge back silently, and report stale, divergent
or cleanup-failed states.

13. Compilation, validation and repair
--------------------------------------

Prefer official APIs, then structured endpoints, HTTP adapters, browser adapters
and finally agentic execution. The first successful exploration produces an
adapter proposal with fixtures and invariants; later runs reuse the compiled
adapter. A site change triggers diagnosis, a patch, sandbox and fixture checks,
browser-truth comparison, a live canary, performance comparison and promotion.
Generated adapters never capture secrets and never gain wider authority than the
policy that produced them. Local Core runs and repairs without Cloud.

14. Evidence, freshness and listing state
-----------------------------------------

Receipts expose status, observed time/age, method, rung, provider and version,
identity reference and mode, executor, adapter version, measured cost, tokens,
evidence references and trace ID. Transport success is an observation, not
semantic verification; field-level verification records which evidence supports
each claim. freshness=now requires current evidence; cached evidence is returned
only as explicitly stale material when requested.

Listing states: live, sold, pending, ended, blocked, unavailable, unknown. Unknown
never silently becomes live. Asking price, current bid and confirmed sale price
stay separate.

15. Output for agents
---------------------

Results are distilled for the model: the operation's requested projection first
(schema fields, Markdown, selected sections), with full content and artifacts
available by reference. Token count is a measured receipt field and a benchmark
metric. The goal is the smallest representation that answers the task.

16. Photos and vision
---------------------

Photos are decoded artifacts with source attribution, bytes, hash, format,
dimensions, observed time and per-image failures, plus gallery coverage and
dedupe. Authenticated image retrieval uses the selected identity's executor.
Vision observations stay distinct from seller claims and inference.

17. Batch, watches, caching and search
--------------------------------------

Queueing, global and per-domain adaptive concurrency, bounded retries, checkpoints,
streaming partial results, budgets, incremental refresh, evidence reuse and dedupe.
TTL is operation-specific; authenticated cache keys include identity, context and
version. Search uses the query lattice (exact SKU, family, brand+noun, partial
model, misspellings/OCR variants, category, seller-intent language, newest
browsing) and a source registry with evaluated operations and health.

18. Action classes and outcomes
-------------------------------

Action classes: READ_PUBLIC, READ_AUTHENTICATED, WRITE_REVERSIBLE, WRITE_EXTERNAL,
PURCHASE/FINANCIAL and ACCOUNT_SECURITY. Policy grants classes per identity and
operation; READ never implies WRITE. Defaults allow READ classes; write, purchase
and account-security classes require explicit owner policy.

Typed outcomes: BLOCKED, AUTH_REQUIRED, AUTH_EXPIRED, CAPTCHA, RATE_LIMITED,
NOT_FOUND, STALE, SCHEMA_CHANGED, TIMEOUT, PROVIDER_DOWN, PROVIDER_UNAVAILABLE,
VISUAL_REQUIRED, BUDGET_EXHAUSTED, POLICY_DENIED, PLUGIN_DISABLED, UNKNOWN, and
identity outcomes (IDENTITY_EXECUTOR_OFFLINE, IDENTITY_AUTHORITY_UNAVAILABLE,
IDENTITY_IN_USE, IDENTITY_EXPORT_DENIED, IDENTITY_POLICY_DENIED,
IDENTITY_REPLICA_STALE, IDENTITY_PROFILE_DIVERGED, IDENTITY_REVOKED,
IDENTITY_REAUTH_REQUIRED, RESULT_EXPORT_DENIED, EXECUTION_OUTCOME_UNKNOWN).

Every final failure reports the rungs tried, why each failed, and the next step
that could succeed: a stronger installed plugin, a policy change, a budget
increase, an identity, or an operator handoff. Uncertain external writes are not
repeated until reconciled.

19. Observability
-----------------

Normal output: concise result, evidence, freshness, cost, tokens and trace.
Explain: routing reasons, rungs tried, alternatives and assumptions. Debug: the
sanitized execution tree and local artifact references, without secrets. Remote
telemetry is opt-in.

20. Release gates
-----------------

FrankenSurf is not released as 1.0 until all gates pass on the current benchmark
manifest (section 21). Thresholds are initial and change only by spec revision.

G1 Superiority
  Task success is at least the best single provider's on every workload class,
  and the overall failure rate is at most half the best single provider's.

G2 Efficiency
  Median tokens per successful read are at most half the browser-snapshot
  baseline, and cost per success is no higher than the best single provider at
  equal or better success.

G3 Learning
  On a repeat run, median latency per successful operation drops by at least 30%
  and failed attempts before success drop to near zero for remembered routes.

G4 No silent failures
  Fewer than 0.5% of reported successes fail content assertions.

G5 Model-agnostic
  The same suite through at least three different MCP clients or models produces
  equivalent outcomes and receipts.

G6 Five-minute start
  A fresh machine reaches a first successful read in five minutes or less by
  following the README, with one install command.

Public numbers on the website come from the latest benchmark report. Anything not
yet measured is shown as a target.

21. Benchmark harness (FrankenBench)
------------------------------------

A versioned workload manifest covers site classes: static pages, JavaScript apps,
infinite scroll, sites protected by Cloudflare, DataDome, Akamai and PerimeterX,
login-walled sites with the user's identity, PDFs and documents, geo-restricted
content, and the existing Australian commerce workloads. Each case has content
assertions (fields, identifiers, gallery checks) that decide correctness.

Coverage requirements distinguish active release surfaces from retained
historical ones. Operational status, release inclusion and market scope are
explicit manifest data. An inactive historical surface remains visible with its
unverified workloads, but does not create an active release workload or identity
gap. Australian-local inventory and a global platform's AU-localized surface are
separate scopes; localization alone never proves Australian seller or stock
geography.

Arms: every installed provider plugin alone, the naive browser-snapshot baseline,
and the FrankenSurf router (cold and with route memory). Each arm runs the same
cases for repeated rounds.

Metrics: assertion-verified success, silent failures, tokens to model, measured
cost, latency, attempts and rungs. Output: dated JSON snapshots with receipts,
a comparison report and the gate results. Reports feed the website's coverage
data. Fixtures never contain secrets. The harness runs only with the user's own
access and accounts.

22. Implementation sequence
---------------------------

A. Local acquisition MVP (done): HTTP, local Chromium, self-hosted Steel, isolated
   public providers, search/read/extract/batch, images, receipts and traces.
B. Local authority spine (partly done): registry, vault/profile interfaces,
   leases, typed failures, operator surfaces.
H. FrankenBench harness and manifest (partly done): the strict gate auditor and
   retained reports exist; complete the release manifest and qualifying runs.
P. Plugin system and policy-ization (partly done): provider, search and adapter
   catalogs exist; add hook and backend interfaces and finish moving limits into
   policy values and defaults.
M. Route memory (partly done): exact-scope, independently verified observations
   can reorder providers; complete cost/token observations, expiry re-probing and
   capability-graph routing so provisional seeds can be retired.
C. Cheap rungs and hooks: Markdown negotiation, signed-agent requests, content
   distillation; first-party stealth, proxy and solver plugin slots.
D. Remote local execution, controlled replication and leases.
E. Compilation and repair (public-read declarative repair slice partly done),
   shared intelligence, distributed watches and Cloud.

Release 1.0 when section 20 gates pass. Later milestones continue after 1.0.

23. Authority acceptance cases
------------------------------

These remain required regardless of capability work:

* Disconnect Cloud: the Local Core workload still succeeds.
* LOCAL_ONLY worker offline: typed failure, no silent export or anonymous
  substitution; cached content cannot satisfy freshness=now.
* Remote local read: Cloud sees only allowed results and receipt.
* Replayed, expired or untrusted jobs are rejected before any work.
* Inconsistent executors cannot hold the same identity lease.
* Replica permission, version, recipient and revocation checks hold; replica
  changes never silently alter canonical state.
* Profile grants cannot export vault credentials and vice versa; logs stay
  secret-free.
* Unknown identity or wrong profile fails before navigation; named identities
  have separate cache scopes, receipts and capability records.
* Policy changes and revocation block cached identity data.

24. Current release
-------------------

The current runtime implements A and the local registry, profile and lease portion
of B. Providers are http, local, local_cdp, steel, camoufox, scrapling and
scrapling_http. Temporary per-site public preferences are typed provisional
catalog seeds; Core contains no domain-specific provider branch, and locally
benchmark-verified route memory can supersede their cold order.
HTTP 401/403/429 and challenges are routing signals by default; policy can make
selected failures terminal or stop the current domain context. Named
LOCAL_ONLY identities use owned tabs that run page JavaScript like a normal tab;
Core checks every top-level navigation against the identity's domains and never
clicks or types during a read.
An opt-in owner_visible_snapshot Core capture strategy observes an exact
already-opened, operator-scoped visible region in the attested canonical browser.
It never navigates, interacts or reads embedded application state. Source
freshness stays unknown, its receipt binds the exact pinned executable strategy,
and gallery assignment stays partial/unverified. A Marketplace visible
adapter and dedicated local graphical browser setup helper support this owner-led
workflow; they do not establish automated discovery or real account coverage.
An architecture-first ``web.do`` slice exercises Core-owned policy, identity,
routing, provider binding, operation-contract validation, evidence and
idempotency through an isolated pinned Browser Use worker. The marked
``local_fixture.reversible_draft.v1`` contract remains HTTP-loopback-only and
requires ``WRITE_REVERSIBLE`` in policy and identity. The explicit break-glass
``browser.raw_control.v1`` contract adds bounded control of exact external HTTPS
origins, with loopback HTTP retained for acceptance. It is excluded from default
policy and automatic provider selection. Callers must name the provider,
contract and origins, and the effective allowed-tool set must exactly match the
intent. Core and the enrolled identity independently grant the
full possible mutation ceiling: ``WRITE_REVERSIBLE``, ``WRITE_EXTERNAL``,
``PURCHASE/FINANCIAL`` and ``ACCOUNT_SECURITY``. For raw control the worker
records the durable effect boundary before external navigation, because the
navigation itself can cause an effect. It intercepts exact-origin traffic and
pauses targets spawned by the owned page before their first request. Selectors
are resolved through CDP, inspected in an isolated world and mutated only through
the same bound DOM node, with a fresh binding after same-origin navigation.
Password/file/hidden inputs, downloads, popups, WebSockets, external protocols,
caller-supplied JavaScript/evaluate actions and out-of-origin requests from that
owned execution tree remain denied. Pre-existing unrelated tabs in the shared
profile are outside this worker's sandbox. Completed keys reuse retained
outcomes; every failed raw run is fenced as uncertain. This release has no
operator reconciliation transition, so an uncertain idempotency key remains
fenced permanently.

Raw completion proves only the exact observed steps and always reports unknown
semantic result. It does not prove that a draft exists, that an item is
unpublished or that an operation is reversible. Real marketplace semantics
still require a separately versioned action adapter with account discrimination,
exact mutation endpoints, server-side readback, reconciliation and rollback.
Current acceptance uses authenticated local fixtures and makes no external-site
or marketplace-listing claim. Action contracts are still registered in a closed
Core catalogue; provider plugins cannot yet contribute the separately versioned
site-action contracts described by the target plugin architecture.

Provider, search and adapter catalogs, verified route-memory ordering and the
FrankenBench gate auditor are implemented. The current release manifest remains
incomplete. Its cases now distinguish provider-neutral release qualification
from explicitly provider-bound diagnostics; diagnostics do not satisfy release
coverage, route learning or gate metrics. Every retained report currently marks
G1--G6 unmeasured. A bounded semantic-contract slice now keeps caller intent
above concrete site adapters. Core owns three immutable public output schemas: an
AU equipment-auction directory contract, an AU second-hand listing/gallery/state
contract and an AU second-hand collection-page contract. Installed adapter
plugins optionally contribute immutable contract-and-URL capabilities; the
generic resolver selects exactly one enabled match or fails before acquisition.
The bundled capabilities cover the exact Bidsonline directory, queryless HTTPS
``/products/<slug>`` pages on OzMobiles and SwapUp, and each site's exact
``/products.json`` path with canonical positive ``limit`` and ``page`` query
values. Site parsing, normalization and cross-field invariants stay inside those
adapter plugins.

``Runtime.extract`` and ``Runtime.paginate`` accept ``adapter_contract`` through
Python, CLI and MCP while preserving the explicit-adapter paths and extract's
default-HTML path. Contract pagination pre-resolves each page URL, pins one
adapter authority for the traversal, and sends every page through ordinary
semantic ``Runtime.extract``. For every accepted page, Core separately retains
the transport representation, exact adapter input, acquisition attestation,
normalized projection and decoded gallery artifacts. The aggregate semantic
traversal binds the ordered requested URL and projection SHA256 for every
committed page, the exact aggregate projection and the exact termination.
FrankenBench rederives each plugin capability selection from startup provenance,
verifies every retained artifact, replays every selected page adapter from its
exact input in a fresh process, and rebuilds the aggregate and termination before
assertions or route learning can qualify.

Contract adapters receive no ambient ``WebPolicy``; Core applies policy to
acquisition, image and page limits. A caller's positive ``limit`` is preserved,
and the bundled collection adapter increments ``page`` after every nonempty batch
until it observes an empty page or reaches ``WebPolicy.max_pages``. The status
``page_limit_reached`` is a bounded partial traversal. An empty probe ends only
the observed endpoint and page sequence; ``catalogue_complete`` remains unknown
and no full-catalogue or reliability claim follows. Page requests are not
snapshot-isolated: catalogue mutations between requests can create reported
duplicates or undetectable skips, and an empty terminal probe is not a consistent
inventory snapshot. Contract extraction bypasses cache in this slice. Contract
mode does not use native multi-page route-recipe paths; those remain available
only to explicit-adapter pagination.

Manifest v0.98 retains exactly five main AU release cases through these public
contracts: ``bidsonline-au-directory-discovery``,
``ozmobiles-au-live-gallery-10316454723734``,
``swapup-au-live-gallery-9527809933550``,
``ozmobiles-au-catalogue-traversal`` and
``swapup-au-catalogue-traversal``. Provider diagnostics remain explicit. A
current-source v0.97 live qualification from exact source commit
``78cecdf62bc8d2c523c82245716c90132bce873e`` ran exactly these five cases through
the ordinary router in two cold rounds. All 10/10 observations and 84/84
assertions passed, with retained evidence verified and semantic replay passing in
a fresh process. Both catalogue cases traversed two pages and terminated
``page_limit_reached``; they are bounded partial traversals and
``catalogue_complete`` remains unknown. The sanitized record is
``SEMANTIC_CONTRACT_V097_LIVE_ACCEPTANCE.json`` and raw artifacts remain private.
This evidence establishes neither site-wide nor all-major-site coverage,
snapshot isolation, longitudinal reliability, provider superiority, release
readiness nor any G1--G6 gate.

Manifest v0.98 additionally requires an explicit complete-gallery assertion for
the exact Cash Converters detail and listing-state cases. A focused qualification
from exact clean source commit ``08ccc3e2e459e39ed60fb7e12f1bfa0d189f5235``
ran the four provider-neutral Cash cases through two cold router rounds. All 8/8
observations and 42/42 top-level assertions passed; page two contributed 24 new
IDs with zero overlap, and both exact-item cases decoded all six images while
verifying the product/page gallery agreement. The state route timed out on the
local provider and escalated successfully to Steel in both rounds. The sanitized
record is ``CASH_CONVERTERS_V098_LIVE_ACCEPTANCE.json``. This is exact-case,
two-round evidence, not full-catalogue, site-wide or longitudinal reliability.

The v0.96 acceptance from source commit ``3ec8a65``
and ``SEMANTIC_CONTRACT_V096_LIVE_ACCEPTANCE.json`` remains historical predecessor
context only. The retained ``2a0a441`` Bidsonline acceptance remains historical
evidence for the predecessor exact mapping.

Remote workers, replicas,
Markdown negotiation, signed requests, and solver or proxy plugin implementations
do not exist yet. Actual outcomes are in README.rst, CAPABILITIES.json,
IDENTITY_EVAL.json and the benchmark artifacts; parser fixtures are not site
coverage.

25. North star
--------------

The strongest web access a user's machine, accounts and installed plugins can
achieve, through one interface that any agent on any model can call, without the
agent needing to know which browser, scraper, API, model or adapter produced the
result, and with evidence it can trust.
