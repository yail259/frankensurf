# Generic public-provider routing audit

This audit covers the local public `read` / `extract` router at the current working
tree. It proposes no site-specific provider preference and makes no reliability claim.

## Current evidence

`ProviderRegistry.candidates()` builds the cold public list from installed plugins in
catalog order. It filters declared operation support, rendering, authentication, paid
authority, local-browser authority, enablement and availability. Core executes the result
sequentially. Registration order is a stable fallback, not a FrankenBench result.

Route memory plans both `read` and `extract`. Transport observations remain explicitly
unverified. A separate journal joins only FrankenBench assertion passes for the exact
trace, provider/adapter versions, immutable catalog bindings and acquisition context.
Promotion requires the policy sample floor of these independent verifications plus
measured success latency. A later failure resets the usable success window. Cost and token
fields remain unmeasured, and capability summaries correctly leave reliability unknown.

FrankenBench does produce case-level `assertion_verified_success`, failure classes,
latency and cost aggregates. Its report still marks G1--G6 `unmeasured` and explicitly
says repeated router rounds do not establish route learning. Those reports can compare
explicit arms, but they do not currently authorize a production preference.

The provider manifest now expresses operation support. It cannot yet express URL scope,
rung, proxy or geography support, cost enforcement, safe concurrency, or whether two
calls may be raced.
Consequently Core cannot exclude a semantically irrelevant provider before acquisition.
The current manifest also cannot justify racing: a cancelled loser may still consume a
paid call, GPU time, rate quota, or an external browser session.

## Minimal architecture

Planning should consume only policy-eligible plugins and produce an ordered plan with a
reason for every move:

1. Explicit caller/provider policy and identity authority remain absolute.
2. A version-bound operator or bundled route recipe applies in its exact scope.
3. Exact-scope local observations may promote a provider after the policy's sample floor.
   Production promotion should require independent correctness evidence. A later failure
   invalidates the preference until it is re-earned.
4. FrankenBench arm results may seed the same typed plan for the exact workload class.
5. Candidates with insufficient evidence retain policy-filtered registration order;
   unknown is never presented as reliability.

A bundled benchmark seed should be immutable package data bound to provider and adapter
versions, benchmark case ID, report hash, manifest hash, assertion set and repeat floor.
Core should reject stale or unverifiable seeds. Operator data can override or disable a
seed by ID without modifying package data, and neither source may grant browser, identity,
paid-provider or network authority. A seed affects only ordering among candidates that
the caller's policy already permits.

`route_memory.provider_plan()` supplies Runtime's ordinary one-call plan for both `read`
and `extract`. It binds domain, normalized path, operation, adapter and version, immutable
provider/adapter bindings, public acquisition context, expiry and post-failure samples.
It returns the complete stable order, evidence counts, measured median latency, basis and
preferred provider. Execution remains sequential.

Older per-site hints are now typed records in `bundled_route_seeds.json`. They can supply
readiness defaults and the cold candidate order, but remain provisional with null
reliability. Runtime has no domain/provider switch. The generic planner still sees every
eligible provider, so enough local exact-scope verification supersedes a seed. Explicit
caller routes and named identity authority bypass automatic seeds and learning.

Remaining integration work includes:

- extend `ProviderManifest` with rung, request-scope preflight, cost-reporting
  class, concurrency limit and race-safety declaration;
- add policy values for aggregate latency, attempts/rungs, parallelism and hedge delay;
- record measured cost, model tokens and provider rung in capability observations;
- add a typed benchmark-seed registry and validation gate before any packaged result can
  alter routing; current FrankenBench reports remain comparison evidence only;
- replace provisional compatibility seeds with committed benchmark-verified records as
  equivalent acceptance evidence accumulates.

Optional racing should be a later executor feature, not a planner shortcut. It may run
only for public reads when policy permits it, every provider declares the call free or
cost-bounded and race-safe, domain/global concurrency permits it, and loser cancellation
and cleanup are accounted. A measured hedge delay can start a stronger provider while a
cheap call is slow. First transport success is insufficient: the winning projection must
pass its adapter/assertions. Every loser still records latency, outcome and any cost.

Under this design Browser Use moves earlier when exact local or benchmark evidence earns
that position, or when capability filtering proves earlier plugins cannot satisfy the
request. It is never promoted globally merely because it is powerful. This supports G1
task success, G2 cost/latency and G3 repeat improvement without inventing a preference.
