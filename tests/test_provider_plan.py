"""Generic provider planning uses scoped evidence without site preferences."""
import json
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from frankensurf.route_memory import (
    capability_summary, provider_plan, preferred_provider)


URL = "https://example.com/items/123"
CONTEXT = "c" * 64
MANIFESTS = [
    {"id": "http", "version": "1", "paid": False},
    {"id": "browser_use", "version": "9", "paid": False},
    {"id": "camoufox", "version": "3", "paid": False},
]


def write_rows(tmp_path, rows):
    (tmp_path / "route-observations.jsonl").write_text(
        "".join(json.dumps({**row, "independent_correctness_verified": False}) + "\n"
                for row in rows))
    verified = [verification(row) for row in rows
                if row.get("outcome") == "observed"
                and row.get("independent_correctness_verified") is True]
    if verified:
        write_verifications(tmp_path, verified)


def verification(observation):
    declared = [{"path": "title", "op": "equals", "value": "expected"}]
    outcomes = [{"path": "title", "op": "equals", "passed": True}]
    digest = lambda value: hashlib.sha256(json.dumps(value, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    result = {"schema": "frankensurf.route-verification/v1",
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "verification_basis": "frankenbench declared assertions passed",
        "benchmark_case_id": "exact", "benchmark_manifest_version": "test",
        "benchmark_manifest_sha256": "a" * 64, "benchmark_case_sha256": "b" * 64,
        "declared_assertions": declared, "assertion_results": outcomes,
        "assertion_set_sha256": digest(declared),
        "assertion_results_sha256": digest(outcomes),
        "trace_id": observation["trace_id"], "provider": observation["provider"],
        "provider_version": observation["provider_version"],
        "provider_binding_id": observation.get("provider_binding_id"),
        "operation": observation["operation"], "adapter": observation["adapter"],
        "adapter_version": observation["adapter_version"],
        "adapter_binding_id": observation.get("adapter_binding_id"),
        "acquisition_context": observation["acquisition_context"],
        "observation_observed_at": observation["observed_at"]}
    if "attempt_ordinal" in observation:
        result["attempt_ordinal"] = observation["attempt_ordinal"]
    return result


def write_verifications(tmp_path, rows):
    (tmp_path / "route-verifications.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows))


def row(index, provider, *, operation="read", adapter=None, adapter_version=None,
        outcome="observed", latency=10, verified=True, context=CONTEXT, version=None,
        provider_binding_id=None, adapter_binding_id=None):
    result = {"schema": "frankensurf.route-observation/v1", "trace_id": str(index),
        "observed_at": (datetime.now(timezone.utc) - timedelta(seconds=20-index)).isoformat(),
        "domain": "example.com", "path_pattern": "/items/{number}",
        "operation": operation, "adapter": adapter, "adapter_version": adapter_version,
        "identity_class": "public", "acquisition_context": context,
        "provider": provider, "provider_version": version or
            next(item["version"] for item in MANIFESTS if item["id"] == provider),
        "outcome": outcome, "failure_code": "BLOCKED" if outcome == "failed" else None,
        "latency_ms": latency, "independent_correctness_verified": verified}
    if provider_binding_id is not None:
        result["provider_binding_id"] = provider_binding_id
    if adapter_binding_id is not None:
        result["adapter_binding_id"] = adapter_binding_id
    return result


def plan(tmp_path, *, operation="read", adapter=None, adapter_version=None,
         context=CONTEXT, verified=True):
    return provider_plan(tmp_path, URL, operation, adapter, adapter_version, MANIFESTS,
        ["http", "camoufox", "browser_use"], 3600, 2, context,
        require_independent_verification=verified)


def test_cold_plan_preserves_policy_filtered_registration_order(tmp_path):
    result = plan(tmp_path)
    assert result == {"ordered": ["http", "camoufox", "browser_use"],
        "preferred": None, "execution": "sequential",
        "basis": "registration order; insufficient exact-scope evidence", "evidence": []}


def test_verified_read_observations_can_promote_browser_agent(tmp_path):
    write_rows(tmp_path, [row(1, "browser_use", latency=90), row(2, "browser_use", latency=70)])
    result = plan(tmp_path)
    assert result["ordered"] == ["browser_use", "http", "camoufox"]
    assert result["preferred"] == "browser_use"
    assert result["execution"] == "sequential"
    assert result["basis"] == "local exact-scope independently verified observations"
    evidence = next(item for item in result["evidence"] if item["provider"] == "browser_use")
    assert evidence["post_failure_independently_verified_count"] == 2
    assert evidence["median_success_latency_ms"] == 80


def test_unverified_transport_success_does_not_drive_honest_plan(tmp_path):
    write_rows(tmp_path, [row(1, "browser_use", verified=False),
                          row(2, "browser_use", verified=False)])
    result = plan(tmp_path)
    assert result["preferred"] is None
    assert result["ordered"] == ["http", "camoufox", "browser_use"]
    assert result["evidence"][0]["preference_eligible"] is False


def test_inline_transport_flags_cannot_replace_verification_events(tmp_path):
    rows = [row(1, "browser_use"), row(2, "browser_use")]
    (tmp_path / "route-observations.jsonl").write_text(
        "".join(json.dumps(item) + "\n" for item in rows))
    assert plan(tmp_path)["preferred"] is None


def test_append_only_verifications_join_exact_observations(tmp_path):
    context = CONTEXT
    rows = [row(1, "browser_use", verified=False, context=context),
            row(2, "browser_use", verified=False, context=context)]
    write_rows(tmp_path, rows)
    write_verifications(tmp_path, [verification(item) for item in rows])
    result = plan(tmp_path, context=context)
    assert result["preferred"] == "browser_use"
    evidence = next(item for item in result["evidence"]
                    if item["provider"] == "browser_use")
    assert evidence["post_failure_independently_verified_count"] == 2


def test_exact_attempt_supersedes_legacy_alias_without_double_counting(
        tmp_path):
    legacy = row(1, "browser_use", latency=90)
    exact = {**legacy, "attempt_ordinal": 0, "latency_ms": 10}
    write_rows(tmp_path, [legacy, exact])

    summary = capability_summary(tmp_path)
    group = summary["groups"][0]
    assert summary["duplicate_records"] == 1
    assert summary["duplicate_verification_records"] == 1
    assert group["sample_count"] == 1
    assert group["independently_verified_count"] == 1
    assert group["median_latency_ms"] == 10

    result = plan(tmp_path)
    evidence = next(item for item in result["evidence"]
                    if item["provider"] == "browser_use")
    assert result["preferred"] is None
    assert evidence["sample_count"] == 1
    assert evidence["post_failure_independently_verified_count"] == 1
    assert evidence["preference_eligible"] is False


@pytest.mark.parametrize("legacy_first", [True, False])
@pytest.mark.parametrize(("exact_outcome", "legacy_outcome"), [
    ("observed", "failed"),
    ("failed", "observed"),
])
def test_conflicting_mixed_outcomes_retain_failure_without_promotion(
        tmp_path, legacy_first, exact_outcome, legacy_outcome):
    exact = {**row(1, "browser_use", outcome=exact_outcome, latency=10),
             "attempt_ordinal": 0}
    legacy = {
        **row(1, "browser_use", outcome=legacy_outcome, latency=2),
        "observed_at": exact["observed_at"],
    }
    rows = [legacy, exact] if legacy_first else [exact, legacy]
    write_rows(tmp_path, rows)

    summary = capability_summary(tmp_path)
    group = summary["groups"][0]
    assert summary["duplicate_records"] == 1
    assert group["sample_count"] == 1
    assert group["observed_count"] == 0
    assert group["failures"] == {"BLOCKED": 1}
    assert group["independently_verified_count"] == 0

    result = provider_plan(
        tmp_path, URL, "read", None, None, MANIFESTS,
        ["http", "camoufox", "browser_use"], 3600, 1, CONTEXT)
    evidence = next(item for item in result["evidence"]
                    if item["provider"] == "browser_use")
    assert result["preferred"] is None
    assert evidence["sample_count"] == 1
    assert evidence["latest_outcome"] == "failed"
    assert evidence["post_failure_independently_verified_count"] == 0
    assert evidence["preference_eligible"] is False


@pytest.mark.parametrize(("outcomes", "expected"), [
    (("failed", "observed"),
     ("browser_use", "observed", 1, 1, True)),
    (("observed", "failed"),
     (None, "failed", 0, 0, False)),
])
def test_exact_retry_plan_is_independent_of_journal_order(
        tmp_path, outcomes, expected):
    first = {**row(7, "browser_use", outcome=outcomes[0], latency=2),
             "attempt_ordinal": 0}
    second = {**row(7, "browser_use", outcome=outcomes[1], latency=10),
              "observed_at": first["observed_at"], "attempt_ordinal": 1}
    snapshots = []
    for name, rows in (("forward", [first, second]),
                       ("reverse", [second, first])):
        state_dir = tmp_path / name
        state_dir.mkdir()
        write_rows(state_dir, rows)
        result = provider_plan(
            state_dir, URL, "read", None, None, MANIFESTS,
            ["http", "camoufox", "browser_use"], 3600, 1, CONTEXT)
        evidence = next(item for item in result["evidence"]
                        if item["provider"] == "browser_use")
        snapshots.append((result["preferred"], evidence["latest_outcome"],
            evidence["post_failure_observed_count"],
            evidence["post_failure_independently_verified_count"],
            evidence["preference_eligible"]))
    assert snapshots == [expected, expected]


def test_equal_time_legacy_traces_order_failure_conservatively(tmp_path):
    observed = row(10, "browser_use", outcome="observed", latency=10)
    failed = {**row(11, "browser_use", outcome="failed", latency=2),
              "observed_at": observed["observed_at"]}
    snapshots = []
    for name, rows in (("forward", [observed, failed]),
                       ("reverse", [failed, observed])):
        state_dir = tmp_path / name
        state_dir.mkdir()
        write_rows(state_dir, rows)
        result = provider_plan(
            state_dir, URL, "read", None, None, MANIFESTS,
            ["http", "camoufox", "browser_use"], 3600, 1, CONTEXT)
        evidence = next(item for item in result["evidence"]
                        if item["provider"] == "browser_use")
        snapshots.append((result["preferred"], evidence["latest_outcome"],
            evidence["post_failure_observed_count"],
            evidence["post_failure_independently_verified_count"],
            evidence["preference_eligible"]))
    assert snapshots == [(None, "failed", 0, 0, False)] * 2


def test_equal_time_recovered_trace_controls_after_pure_success(tmp_path):
    results = []
    for swap_ids in (False, True):
        for reverse_lines in (False, True):
            state_dir = tmp_path / f"{swap_ids}-{reverse_lines}"
            state_dir.mkdir()
            pure_id, recovered_id = (("pure", "recovered") if not swap_ids
                                     else ("recovered", "pure"))
            observed_at = row(12, "browser_use")["observed_at"]
            pure = {**row(12, "browser_use", latency=5),
                    "trace_id": pure_id, "observed_at": observed_at,
                    "attempt_ordinal": 0}
            recovered_failure = {
                **row(13, "browser_use", outcome="failed", latency=2),
                "trace_id": recovered_id, "observed_at": observed_at,
                "attempt_ordinal": 0}
            recovered_success = {**row(13, "browser_use", latency=30),
                "trace_id": recovered_id, "observed_at": observed_at,
                "attempt_ordinal": 1}
            rows = [pure, recovered_failure, recovered_success]
            write_rows(state_dir, list(reversed(rows)) if reverse_lines else rows)
            results.append(provider_plan(
                state_dir, URL, "read", None, None, MANIFESTS,
                ["http", "camoufox", "browser_use"], 3600, 2, CONTEXT))

    assert results == [results[0]] * 4
    evidence = next(item for item in results[0]["evidence"]
                    if item["provider"] == "browser_use")
    assert results[0]["preferred"] is None
    assert evidence["sample_count"] == 3
    assert evidence["latest_outcome"] == "observed"
    assert evidence["post_failure_observed_count"] == 1
    assert evidence["post_failure_independently_verified_count"] == 1
    assert evidence["post_failure_latency_sample_count"] == 1
    assert evidence["median_success_latency_ms"] == 30
    assert evidence["preference_eligible"] is False


def test_equal_time_recovered_traces_keep_weakest_tail(tmp_path):
    results = []
    for swap_ids in (False, True):
        for reverse_lines in (False, True):
            state_dir = tmp_path / f"{swap_ids}-{reverse_lines}"
            state_dir.mkdir()
            long_id, short_id = (("long", "short") if not swap_ids
                                 else ("short", "long"))
            observed_at = row(14, "browser_use")["observed_at"]
            rows = [
                {**row(14, "browser_use", outcome="failed", latency=1),
                 "trace_id": long_id, "observed_at": observed_at,
                 "attempt_ordinal": 0},
                {**row(14, "browser_use", latency=10),
                 "trace_id": long_id, "observed_at": observed_at,
                 "attempt_ordinal": 1},
                {**row(14, "browser_use", latency=20),
                 "trace_id": long_id, "observed_at": observed_at,
                 "attempt_ordinal": 2},
                {**row(15, "browser_use", outcome="failed", latency=2),
                 "trace_id": short_id, "observed_at": observed_at,
                 "attempt_ordinal": 0},
                {**row(15, "browser_use", latency=70),
                 "trace_id": short_id, "observed_at": observed_at,
                 "attempt_ordinal": 1},
            ]
            write_rows(state_dir, list(reversed(rows)) if reverse_lines else rows)
            results.append(provider_plan(
                state_dir, URL, "read", None, None, MANIFESTS,
                ["http", "camoufox", "browser_use"], 3600, 2, CONTEXT))

    assert results == [results[0]] * 4
    evidence = next(item for item in results[0]["evidence"]
                    if item["provider"] == "browser_use")
    assert results[0]["preferred"] is None
    assert evidence["sample_count"] == 5
    assert evidence["post_failure_observed_count"] == 1
    assert evidence["post_failure_independently_verified_count"] == 1
    assert evidence["post_failure_latency_sample_count"] == 1
    assert evidence["median_success_latency_ms"] == 70


@pytest.mark.parametrize(
    ("weak_verified", "weak_latency", "expected_verified",
     "expected_latency_count", "expected_median", "expected_preferred"), [
        (False, 60, 0, 1, 60, None),
        (True, None, 1, 0, None, None),
        (True, 90, 1, 1, 90, "browser_use"),
    ])
def test_equal_time_recovered_tie_uses_weakest_tail_metrics(
        tmp_path, weak_verified, weak_latency, expected_verified,
        expected_latency_count, expected_median, expected_preferred):
    results = []
    for swap_ids in (False, True):
        for reverse_lines in (False, True):
            state_dir = tmp_path / f"{swap_ids}-{reverse_lines}"
            state_dir.mkdir()
            strong_id, weak_id = (("strong", "weak") if not swap_ids
                                  else ("weak", "strong"))
            observed_at = row(16, "browser_use")["observed_at"]
            rows = [
                {**row(16, "browser_use", outcome="failed", latency=1),
                 "trace_id": strong_id, "observed_at": observed_at,
                 "attempt_ordinal": 0},
                {**row(16, "browser_use", latency=10),
                 "trace_id": strong_id, "observed_at": observed_at,
                 "attempt_ordinal": 1},
                {**row(17, "browser_use", outcome="failed", latency=2),
                 "trace_id": weak_id, "observed_at": observed_at,
                 "attempt_ordinal": 0},
                {**row(17, "browser_use", latency=weak_latency,
                       verified=weak_verified),
                 "trace_id": weak_id, "observed_at": observed_at,
                 "attempt_ordinal": 1},
            ]
            write_rows(state_dir, list(reversed(rows)) if reverse_lines else rows)
            results.append(provider_plan(
                state_dir, URL, "read", None, None, MANIFESTS,
                ["http", "camoufox", "browser_use"], 3600, 1, CONTEXT))

    assert results == [results[0]] * 4
    evidence = next(item for item in results[0]["evidence"]
                    if item["provider"] == "browser_use")
    assert results[0]["preferred"] == expected_preferred
    assert evidence["post_failure_observed_count"] == 1
    assert evidence["post_failure_independently_verified_count"] == expected_verified
    assert evidence["post_failure_latency_sample_count"] == expected_latency_count
    assert evidence["median_success_latency_ms"] == expected_median


@pytest.mark.parametrize("matching_first", [True, False])
def test_conflicting_exact_verification_aliases_never_promote(
        tmp_path, matching_first):
    observation = {**row(8, "browser_use", verified=False),
                   "attempt_ordinal": 0}
    write_rows(tmp_path, [observation])
    matching = verification(observation)
    conflicting = {**matching,
        "observation_observed_at": (
            datetime.fromisoformat(observation["observed_at"])
            + timedelta(seconds=1)).isoformat()}
    marks = ([matching, conflicting] if matching_first
             else [conflicting, matching])
    write_verifications(tmp_path, marks)

    summary = capability_summary(tmp_path)
    assert summary["duplicate_verification_records"] == 1
    assert summary["groups"][0]["independently_verified_count"] == 0
    result = provider_plan(
        tmp_path, URL, "read", None, None, MANIFESTS,
        ["http", "camoufox", "browser_use"], 3600, 1, CONTEXT)
    evidence = next(item for item in result["evidence"]
                    if item["provider"] == "browser_use")
    assert result["preferred"] is None
    assert evidence["post_failure_independently_verified_count"] == 0
    assert evidence["preference_eligible"] is False


def test_identical_exact_verification_aliases_collapse(tmp_path):
    observation = {**row(9, "browser_use", verified=False),
                   "attempt_ordinal": 0}
    write_rows(tmp_path, [observation])
    mark = verification(observation)
    write_verifications(tmp_path, [mark, mark])

    summary = capability_summary(tmp_path)
    assert summary["duplicate_verification_records"] == 1
    assert summary["groups"][0]["independently_verified_count"] == 1
    result = provider_plan(
        tmp_path, URL, "read", None, None, MANIFESTS,
        ["http", "camoufox", "browser_use"], 3600, 1, CONTEXT)
    assert result["preferred"] == "browser_use"


@pytest.mark.parametrize("failure_first", [True, False])
def test_ambiguous_legacy_retry_retains_failure_without_verification(
        tmp_path, failure_first):
    observed = row(2, "browser_use", latency=10)
    failed = {**row(2, "browser_use", outcome="failed", latency=2),
              "observed_at": observed["observed_at"]}
    rows = [failed, observed] if failure_first else [observed, failed]
    write_rows(tmp_path, rows)

    summary = capability_summary(tmp_path)
    group = summary["groups"][0]
    assert summary["duplicate_records"] == 1
    assert group["sample_count"] == 1
    assert group["observed_count"] == 0
    assert group["failures"] == {"BLOCKED": 1}
    assert group["independently_verified_count"] == 0

    result = provider_plan(
        tmp_path, URL, "read", None, None, MANIFESTS,
        ["http", "camoufox", "browser_use"], 3600, 1, CONTEXT)
    evidence = next(item for item in result["evidence"]
                    if item["provider"] == "browser_use")
    assert result["preferred"] is None
    assert evidence["latest_outcome"] == "failed"
    assert evidence["post_failure_independently_verified_count"] == 0


def test_current_bindings_cannot_inherit_same_version_or_legacy_evidence(tmp_path):
    provider_binding = "1" * 64
    adapter_binding = "2" * 64
    rows = [row(index, "browser_use", operation="extract", adapter="json",
                adapter_version="1", provider_binding_id=provider_binding,
                adapter_binding_id=adapter_binding)
            for index in (1, 2)]
    write_rows(tmp_path, rows)
    manifests = [{**item, "binding_id": (provider_binding
                  if item["id"] == "browser_use" else str(index) * 64)}
                 for index, item in enumerate(MANIFESTS, start=3)]

    matching = provider_plan(tmp_path, URL, "extract", "json", "1", manifests,
        ["http", "camoufox", "browser_use"], 3600, 2, CONTEXT,
        adapter_binding_id=adapter_binding)
    assert matching["preferred"] == "browser_use"

    changed_provider = [{**item, "binding_id": "9" * 64}
                        if item["id"] == "browser_use" else item
                        for item in manifests]
    assert provider_plan(tmp_path, URL, "extract", "json", "1", changed_provider,
        ["http", "camoufox", "browser_use"], 3600, 2, CONTEXT,
        adapter_binding_id=adapter_binding)["preferred"] is None
    assert provider_plan(tmp_path, URL, "extract", "json", "1", manifests,
        ["http", "camoufox", "browser_use"], 3600, 2, CONTEXT,
        adapter_binding_id="8" * 64)["preferred"] is None

    legacy_rows = [{key: value for key, value in item.items()
                    if key not in {"provider_binding_id", "adapter_binding_id"}}
                   for item in rows]
    write_rows(tmp_path, legacy_rows)
    assert provider_plan(tmp_path, URL, "extract", "json", "1", manifests,
        ["http", "camoufox", "browser_use"], 3600, 2, CONTEXT,
        adapter_binding_id=adapter_binding)["preferred"] is None


def test_changed_assertion_or_malformed_provenance_cannot_train_plan(tmp_path):
    context = CONTEXT
    rows = [row(1, "browser_use", verified=False, context=context),
            row(2, "browser_use", verified=False, context=context)]
    write_rows(tmp_path, rows)
    changed = verification(rows[0])
    changed["declared_assertions"][0]["value"] = "forged"
    malformed = verification(rows[1])
    malformed.pop("benchmark_manifest_sha256")
    write_verifications(tmp_path, [changed, malformed])
    assert plan(tmp_path, context=context)["preferred"] is None


def test_verified_results_need_the_same_floor_of_measured_latency(tmp_path):
    write_rows(tmp_path, [row(1, "browser_use", latency=None),
                          row(2, "browser_use", latency=10)])
    result = plan(tmp_path)
    assert result["preferred"] is None
    evidence = next(item for item in result["evidence"]
                    if item["provider"] == "browser_use")
    assert evidence["post_failure_independently_verified_count"] == 2
    assert evidence["post_failure_latency_sample_count"] == 1


def test_failure_resets_evidence_and_route_must_reearn_preference(tmp_path):
    rows = [row(1, "browser_use"), row(2, "browser_use"),
            row(3, "browser_use", outcome="failed"), row(4, "browser_use")]
    write_rows(tmp_path, rows)
    result = plan(tmp_path)
    assert result["preferred"] is None
    evidence = next(item for item in result["evidence"]
                    if item["provider"] == "browser_use")
    assert evidence["sample_count"] == 4
    assert evidence["post_failure_observed_count"] == 1
    assert evidence["preference_eligible"] is False
    rows.append(row(5, "browser_use"))
    write_rows(tmp_path, rows)
    assert plan(tmp_path)["preferred"] == "browser_use"


def test_scope_context_and_version_mismatches_never_train_plan(tmp_path):
    write_rows(tmp_path, [
        row(1, "browser_use", context="other"), row(2, "browser_use", context="other"),
        row(3, "browser_use", version="old"), row(4, "browser_use", version="old"),
        {**row(5, "browser_use"), "domain": "other.example"},
        {**row(6, "browser_use"), "path_pattern": "/other/{number}"},
    ])
    assert plan(tmp_path)["preferred"] is None


def test_multiple_verified_routes_rank_measured_latency_then_keep_unknown_order(tmp_path):
    write_rows(tmp_path, [row(1, "browser_use", latency=80), row(2, "browser_use", latency=100),
                          row(3, "camoufox", latency=20), row(4, "camoufox", latency=30)])
    result = plan(tmp_path)
    assert result["ordered"] == ["camoufox", "browser_use", "http"]


def test_extract_legacy_wrapper_preserves_current_unverified_behavior(tmp_path):
    rows = [row(1, "browser_use", operation="extract", adapter="json",
                adapter_version="1", verified=False, latency=None),
            row(2, "browser_use", operation="extract", adapter="json",
                adapter_version="1", verified=False)]
    write_rows(tmp_path, rows)
    assert preferred_provider(tmp_path, URL, "json", "1", MANIFESTS, 3600, 2,
                              CONTEXT) == "browser_use"
    assert preferred_provider(tmp_path, URL, None, None, MANIFESTS, 3600, 2,
                              CONTEXT) is None
