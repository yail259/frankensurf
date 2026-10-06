"""Local routing observations and scoped preferences; reliability remains unmeasured."""
import hashlib
import json
import os
import re
import stat
from datetime import datetime, timezone
from urllib.parse import urlparse


_OBSERVATION_SCHEMA = "frankensurf.route-observation/v1"
_VERIFICATION_SCHEMA = "frankensurf.route-verification/v1"


def _binding_id(value):
    """Return a validated catalog binding ID, or ``None`` for legacy evidence."""
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) else None


def _attempt_ordinal(row):
    """Return a validated attempt position, or ``None`` for legacy rows."""
    if "attempt_ordinal" not in row:
        return None
    value = row["attempt_ordinal"]
    if type(value) is not int or value < 0:
        raise ValueError()
    return value


def _canonical_attempt_rows(rows):
    """Collapse aliases for one trace/provider without rewriting journals.

    Exact ordinals supersede legacy ``None`` aliases only when the legacy rows
    add no outcome absent from the exact attempts.  A mixed legacy/exact
    conflict collapses the whole acquisition to a failure and is marked
    ambiguous so it cannot inherit verification, regardless of file order.
    Contradictory rows for one remaining identity are treated the same way.
    """
    rows = list(rows)
    groups = {}
    for position, (row, attempt_ordinal) in enumerate(rows):
        base = (row["trace_id"], row["provider"])
        groups.setdefault(base, []).append((position, row, attempt_ordinal))
    retained = []
    for members in groups.values():
        exact = [item for item in members if item[2] is not None]
        legacy = [item for item in members if item[2] is None]
        outcomes_are_observations = all(
            item[1].get("outcome") in {"observed", "failed"}
            for item in members)
        exact_outcomes = {item[1].get("outcome") for item in exact
                          if item[1].get("outcome") in {"observed", "failed"}}
        legacy_outcomes = {item[1].get("outcome") for item in legacy
                           if item[1].get("outcome") in {"observed", "failed"}}
        if (outcomes_are_observations and exact
                and legacy_outcomes - exact_outcomes):
            # Missing ordinals cannot be assumed to alias an exact attempt when
            # they introduce a different outcome.  Retain a real failure from
            # the acquisition and suppress every success rather than letting a
            # conflicting legacy journal train routing.
            selected = next(
                (item for item in exact
                 if item[1].get("outcome") == "failed"), None)
            if selected is None:
                selected = next(item for item in members
                                if item[1].get("outcome") == "failed")
            retained.append((*selected, False))
            continue
        candidates = exact if exact else members
        identities = {}
        for item in candidates:
            identities.setdefault(item[2], []).append(item)
        for aliases in identities.values():
            selected = next(
                (item for item in aliases
                 if item[1].get("outcome") == "failed"),
                aliases[0])
            retained.append((*selected, len(aliases) == 1))
    retained.sort(key=lambda item: item[0])
    canonical = [(row, attempt_ordinal, unambiguous)
                 for _, row, attempt_ordinal, unambiguous in retained]
    return canonical, len(rows) - len(canonical)


def _ordered_route_records(records, verification_keys,
                           require_independent_verification):
    """Order retained provider records without trusting journal line order."""
    import math
    from statistics import median

    traces = {}
    for record in records:
        traces.setdefault(record[1]["trace_id"], []).append(record)
    ordered_traces = []
    for members in traces.values():
        if all(attempt_ordinal is not None
               for _, _, attempt_ordinal, _ in members):
            sequence = sorted(members, key=lambda item: item[2])
        else:
            # Legacy rows cannot establish an attempt sequence.  Canonical
            # handling normally leaves one row; this defensive ordering puts a
            # failure last when equally timed legacy evidence remains.
            sequence = sorted(members, key=lambda item: (
                item[0], item[1].get("outcome") == "failed"))
        failure_positions = [index for index, (_, row, _, _) in
                             enumerate(sequence)
                             if row.get("outcome") == "failed"]
        last_failure = max(failure_positions, default=-1)
        tail = [record for record in sequence[last_failure + 1:]
                if record[1].get("outcome") == "observed"]
        verified_count = sum(_independently_verified(
            row, verification_keys, unambiguous)
            for _, row, _, unambiguous in tail)
        latencies = [row.get("latency_ms") for _, row, _, _ in tail]
        latencies = [value for value in latencies
                     if type(value) in (int, float)
                     and math.isfinite(value) and value >= 0]
        observed_count = len(tail)
        if require_independent_verification:
            supported_floor = min(
                observed_count, verified_count, len(latencies))
        else:
            supported_floor = observed_count if latencies else 0
        if not failure_positions:
            trace_class = 0
        elif sequence[-1][1].get("outcome") == "failed":
            trace_class = 2
        else:
            trace_class = 1
        signature = [{"attempt_ordinal": attempt_ordinal,
                      "outcome": row.get("outcome"),
                      "verified": _independently_verified(
                          row, verification_keys, unambiguous),
                      "unambiguous": unambiguous,
                      "latency_ms": (row.get("latency_ms")
                          if type(row.get("latency_ms")) in (int, float)
                          and math.isfinite(row["latency_ms"])
                          and row["latency_ms"] >= 0 else None)}
                     for _, row, attempt_ordinal, unambiguous in sequence]
        signature_digest = hashlib.sha256(json.dumps(
            signature, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        trace_time = max(item[0] for item in sequence)
        # Equal-time acquisitions have no trustworthy cross-trace chronology.
        # Pure successes precede recovered traces, and failure-ending traces
        # come last.  Among recovered traces, stronger tails precede weaker
        # tails so arbitrary trace IDs cannot improve the evidence floor.
        order_key = (trace_time, trace_class, -supported_floor,
            -verified_count, -len(latencies), -observed_count,
            median(latencies) if latencies else float("inf"), signature_digest)
        ordered_traces.append((order_key, sequence))
    ordered_traces.sort(key=lambda item: item[0])
    return [record for _, sequence in ordered_traces
            for record in sequence]


def _append_jsonl(path, rows):
    """Append complete JSONL records without rewriting existing routing evidence."""
    rows = list(rows)
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError("Refusing to append routing evidence through a symlink")
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    lock = None
    try:
        try:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            lock = fcntl
        except ImportError:
            pass
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("Routing evidence must be a regular file")
        try:
            os.fchmod(descriptor, 0o600)
        except (AttributeError, OSError):
            pass
        payload = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows).encode()
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("Unable to append routing evidence")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        if lock is not None:
            lock.flock(descriptor, lock.LOCK_UN)
        os.close(descriptor)


def _verification_key_from_observation(row):
    return (row.get("trace_id"), row.get("provider"), row.get("provider_version"),
            row.get("provider_binding_id"), row.get("operation"), row.get("adapter"),
            row.get("adapter_version"), row.get("adapter_binding_id"),
            row.get("acquisition_context"), row.get("observed_at"),
            _attempt_ordinal(row))


def _verification_key(row):
    return (row.get("trace_id"), row.get("provider"), row.get("provider_version"),
            row.get("provider_binding_id"), row.get("operation"), row.get("adapter"),
            row.get("adapter_version"), row.get("adapter_binding_id"),
            row.get("acquisition_context"), row.get("observation_observed_at"),
            _attempt_ordinal(row))


def _verification_keys(state_dir):
    path = state_dir / "route-verifications.jsonl"
    keys, invalid = set(), 0
    if not path.exists():
        return keys, invalid, 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return keys, invalid, 0
    valid_rows = []
    for line in lines:
        try:
            row = json.loads(line)
            attempt_ordinal = _attempt_ordinal(row)
            declared = row.get("declared_assertions")
            outcomes = row.get("assertion_results")
            valid_declared = (isinstance(declared, list) and bool(declared)
                and all(isinstance(check, dict)
                    and isinstance(check.get("path"), str) and check["path"]
                    and all(check["path"].split("."))
                    and check.get("op") in {"equals", "contains", "nonempty", "gallery_decoded"}
                    and (check["op"] not in {"equals", "contains"} or "value" in check)
                    for check in declared))
            valid_outcomes = (isinstance(outcomes, list)
                and len(outcomes) == len(declared or [])
                and all(isinstance(outcome, dict) and outcome.get("passed") is True
                    and outcome.get("path") == check.get("path")
                    and outcome.get("op") == check.get("op")
                    for check, outcome in zip(declared or [], outcomes)))
            declared_hash = hashlib.sha256(json.dumps(declared, sort_keys=True,
                separators=(",", ":"), allow_nan=False).encode()).hexdigest() if valid_declared else None
            outcome_hash = hashlib.sha256(json.dumps(outcomes, sort_keys=True,
                separators=(",", ":"), allow_nan=False).encode()).hexdigest() if valid_outcomes else None
            observed_at = datetime.fromisoformat(row["observation_observed_at"])
            verified_at = datetime.fromisoformat(row["verified_at"])
            if (not isinstance(row, dict) or row.get("schema") != _VERIFICATION_SCHEMA
                    or not all(isinstance(value, str) and value for value in
                               (row.get("trace_id"), row.get("provider"),
                                row.get("provider_version"), row.get("operation"),
                                row.get("acquisition_context"),
                                row.get("observation_observed_at"), row.get("verified_at")))
                    or row.get("verification_basis") != "frankenbench declared assertions passed"
                    or not isinstance(row.get("benchmark_case_id"), str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]+", row["benchmark_case_id"])
                    or not isinstance(row.get("benchmark_manifest_version"), str)
                    or not row["benchmark_manifest_version"]
                    or any(not isinstance(row.get(field), str)
                           or not re.fullmatch(r"[0-9a-f]{64}", row[field])
                           for field in ("benchmark_manifest_sha256", "benchmark_case_sha256",
                                         "assertion_set_sha256", "assertion_results_sha256"))
                    or row.get("assertion_set_sha256") != declared_hash
                    or row.get("assertion_results_sha256") != outcome_hash
                    or not valid_declared or not valid_outcomes
                    or row.get("operation") not in ("read", "extract")
                    or not re.fullmatch(r"[0-9a-f]{64}", row["acquisition_context"])
                    or (row.get("adapter") is not None
                        and (not isinstance(row.get("adapter"), str) or not row["adapter"]))
                    or (row.get("adapter_version") is not None
                        and (not isinstance(row.get("adapter_version"), str)
                             or not row["adapter_version"]))
                    or (row.get("adapter") is None) != (row.get("adapter_version") is None)
                    or (row.get("provider_binding_id") is not None
                        and _binding_id(row.get("provider_binding_id")) is None)
                    or (row.get("adapter_binding_id") is not None
                        and _binding_id(row.get("adapter_binding_id")) is None)
                    or (row.get("adapter") is None
                        and row.get("adapter_binding_id") is not None)
                    or observed_at.tzinfo is None or verified_at.tzinfo is None
                    or verified_at < observed_at):
                raise ValueError()
            valid_rows.append((row, attempt_ordinal))
        except (ValueError, TypeError, KeyError, AttributeError):
            invalid += 1
    unique_rows = {}
    duplicates = 0
    for row, attempt_ordinal in valid_rows:
        key = _verification_key(row)
        if key in unique_rows:
            duplicates += 1
            continue
        unique_rows[key] = (row, attempt_ordinal)
    canonical, canonical_duplicates = _canonical_attempt_rows(
        unique_rows.values())
    duplicates += canonical_duplicates
    for row, _, unambiguous in canonical:
        if not unambiguous:
            continue
        key = _verification_key(row)
        if key in keys:
            duplicates += 1
            continue
        keys.add(key)
    return keys, invalid, duplicates


def _independently_verified(row, verification_keys, unambiguous=True):
    # Transport observations never certify their own correctness. Only a
    # separately validated assertion event can promote an acquisition.
    return (unambiguous and row.get("outcome") == "observed"
            and _verification_key_from_observation(row) in verification_keys)


def acquisition_context(policy):
    """Opaque public policy scope; never persist selectors, entry queries or identities."""
    from dataclasses import asdict
    from hashlib import sha256
    if policy.identity:
        return None
    values = asdict(policy)
    for key in ("identity", "provider", "provider_candidates", "freshness", "use_route_memory",
                "route_memory_ttl_seconds", "route_memory_min_samples"):
        values.pop(key, None)
    raw = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    return sha256(raw).hexdigest()


def record_observations(state_dir, result):
    receipt = result["receipt"]
    if receipt.get("cache_hit") or not receipt.get("attempts"):
        return
    adapter = receipt.get("adapter")
    adapter_version = receipt.get("adapter_version")
    adapter_binding_id = receipt.get("adapter_binding_id")
    if ((adapter is None) != (adapter_version is None)
            or (adapter is None and adapter_binding_id is not None)
            or (adapter is not None and _binding_id(adapter_binding_id) is None)):
        return
    source = urlparse(receipt.get("requested_url") or result.get("url") or "")
    named = bool(receipt.get("identity"))
    # Named paths may themselves carry secrets. Query/fragment values never enter
    # this routing journal, even for public operations.
    pattern = None if named else re.sub(r"[0-9]+", "{number}", source.path)
    rows = []
    for attempt_ordinal, attempt in enumerate(receipt["attempts"]):
        provider_binding_id = attempt.get("provider_binding_id")
        if _binding_id(provider_binding_id) is None:
            continue
        rows.append({"schema": _OBSERVATION_SCHEMA,
            "trace_id": receipt["trace_id"], "observed_at": receipt.get("observed_at"),
            "domain": source.hostname, "path_pattern": pattern,
            "operation": receipt["operation"], "adapter": receipt.get("adapter"),
            "adapter_version": receipt.get("adapter_version"),
            "adapter_binding_id": adapter_binding_id,
            "identity_class": "named" if named else "public",
            "acquisition_context": None if named else receipt.get("acquisition_context"),
            "provider": attempt["provider"], "provider_version": attempt.get("provider_version"),
            "provider_binding_id": provider_binding_id,
            "attempt_ordinal": attempt_ordinal,
            "outcome": attempt["status"], "failure_code": attempt.get("failure"),
            "latency_ms": attempt.get("latency_ms"), "cost_usd": None,
            "tokens_to_model": None, "independent_correctness_verified": False})
    _append_jsonl(state_dir / "route-observations.jsonl", rows)


def record_benchmark_verification(state_dir, receipt, *, case_id, manifest_version,
                                  manifest_sha256, case_sha256, declared_assertions,
                                  assertion_results, providers=None, adapters=None):
    """Append marks for observed attempts covered by passing FrankenBench checks.

    A mark is emitted only when its exact public observation already exists and
    the receipt still names the current provider and adapter versions and
    catalog bindings. The
    observation journal remains immutable; failed or missing assertions append
    nothing.
    """
    if (not isinstance(receipt, dict) or receipt.get("status") != "observed"
            or receipt.get("identity") or not isinstance(case_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", case_id)
            or not isinstance(manifest_version, str) or not manifest_version
            or not isinstance(manifest_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256)
            or not isinstance(case_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", case_sha256)
            or not isinstance(declared_assertions, list) or not declared_assertions
            or not isinstance(assertion_results, list) or not assertion_results
            or len(declared_assertions) != len(assertion_results)
            or any(not isinstance(check, dict)
                   or not isinstance(check.get("path"), str) or not check["path"]
                   or not all(check["path"].split("."))
                   or check.get("op") not in {"equals", "contains", "nonempty", "gallery_decoded"}
                   or (check["op"] in {"equals", "contains"} and "value" not in check)
                   for check in declared_assertions)
            or any(not isinstance(item, dict) or item.get("passed") is not True
                   or item.get("path") != check.get("path")
                   or item.get("op") != check.get("op")
                   for check, item in zip(declared_assertions, assertion_results))):
        return []
    trace_id = receipt.get("trace_id")
    context = receipt.get("acquisition_context")
    operation = receipt.get("operation")
    adapter = receipt.get("adapter")
    adapter_version = receipt.get("adapter_version")
    adapter_binding_id = receipt.get("adapter_binding_id")
    if (not isinstance(trace_id, str) or not trace_id
            or not isinstance(context, str) or not re.fullmatch(r"[0-9a-f]{64}", context)
            or operation not in ("read", "extract")
            or (adapter is not None and (not isinstance(adapter, str) or not adapter))
            or (adapter_version is not None
                and (not isinstance(adapter_version, str) or not adapter_version))
            or (adapter is None) != (adapter_version is None)
            or (adapter is None and adapter_binding_id is not None)
            or (adapter is not None and _binding_id(adapter_binding_id) is None)):
        return []

    if providers is None:
        from .providers import DEFAULT_PROVIDERS
        providers = DEFAULT_PROVIDERS
    provider_versions = {item["id"]: (item["version"], _binding_id(item.get("binding_id")))
                         for item in providers.inspect() if item.get("enabled")}
    if adapter is None:
        if adapter_version is not None:
            return []
    else:
        if adapters is None:
            from .adapters import DEFAULT_ADAPTERS
            adapters = DEFAULT_ADAPTERS
        adapter_versions = {item["id"]: (item["version"], _binding_id(item.get("binding_id")))
                            for item in adapters.inspect() if item.get("enabled")}
        current_adapter = adapter_versions.get(adapter)
        if (current_adapter is None or current_adapter[0] != adapter_version
                or (current_adapter[1] is not None
                    and current_adapter[1] != adapter_binding_id)):
            return []

    observation_path = state_dir / "route-observations.jsonl"
    try:
        observed_rows = [json.loads(line) for line in
                         observation_path.read_text(encoding="utf-8").splitlines()]
    except (OSError, ValueError, TypeError):
        return []
    valid_rows = []
    for row in observed_rows:
        try:
            attempt_ordinal = _attempt_ordinal(row)
        except (TypeError, ValueError):
            continue
        if (isinstance(row, dict) and row.get("schema") == _OBSERVATION_SCHEMA
                and row.get("trace_id") == trace_id
                and row.get("outcome") in {"observed", "failed"}
                and isinstance(row.get("provider"), str) and row["provider"]
                and isinstance(row.get("observed_at"), str)):
            valid_rows.append((row, attempt_ordinal))
    matching = {}
    canonical_rows, _ = _canonical_attempt_rows(valid_rows)
    for row, attempt_ordinal, unambiguous in canonical_rows:
        if (row["outcome"] == "observed" and unambiguous
                and row.get("identity_class") == "public"
                and row.get("operation") == operation
                and row.get("adapter") == adapter
                and row.get("adapter_version") == adapter_version
                and row.get("adapter_binding_id") == adapter_binding_id
                and row.get("acquisition_context") == context):
            matching[(row.get("provider"), row.get("provider_version"),
                      row.get("provider_binding_id"), attempt_ordinal)] = row

    receipt_attempt_counts = {}
    for attempt in receipt.get("attempts", []):
        if not isinstance(attempt, dict):
            continue
        provider_key = (attempt.get("provider"), attempt.get("provider_version"),
                        attempt.get("provider_binding_id"))
        receipt_attempt_counts[provider_key] = (
            receipt_attempt_counts.get(provider_key, 0) + 1)

    already, _, _ = _verification_keys(state_dir)
    assertion_set_sha256 = hashlib.sha256(json.dumps(declared_assertions, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    assertion_results_sha256 = hashlib.sha256(json.dumps(assertion_results, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    verified_at = datetime.now(timezone.utc).isoformat()
    rows = []
    for attempt_ordinal, attempt in enumerate(receipt.get("attempts", [])):
        if not isinstance(attempt, dict) or attempt.get("status") != "observed":
            continue
        provider = attempt.get("provider")
        provider_version = attempt.get("provider_version")
        provider_binding_id = attempt.get("provider_binding_id")
        current_provider = provider_versions.get(provider)
        if (_binding_id(provider_binding_id) is None or current_provider is None
                or current_provider[0] != provider_version
                or (current_provider[1] is not None
                    and current_provider[1] != provider_binding_id)):
            continue
        provider_key = (provider, provider_version, provider_binding_id)
        exact = matching.get((*provider_key, attempt_ordinal))
        observation_ordinal = attempt_ordinal
        if exact is not None:
            observation = exact
        else:
            legacy = matching.get((*provider_key, None))
            if (legacy is None
                    or receipt_attempt_counts.get(provider_key) != 1):
                # Missing ordinals from an old journal can match a single
                # post-upgrade attempt only. Retried or mixed evidence remains
                # fail-closed instead of treating None as a wildcard.
                continue
            observation = legacy
            observation_ordinal = None
        row = {"schema": _VERIFICATION_SCHEMA, "verified_at": verified_at,
            "verification_basis": "frankenbench declared assertions passed",
            "benchmark_case_id": case_id, "benchmark_manifest_version": manifest_version,
            "benchmark_manifest_sha256": manifest_sha256,
            "benchmark_case_sha256": case_sha256,
            "declared_assertions": declared_assertions,
            "assertion_results": assertion_results,
            "assertion_set_sha256": assertion_set_sha256,
            "assertion_results_sha256": assertion_results_sha256,
            "trace_id": trace_id, "provider": provider,
            "provider_version": provider_version,
            "provider_binding_id": provider_binding_id, "operation": operation,
            "adapter": adapter, "adapter_version": adapter_version,
            "adapter_binding_id": adapter_binding_id,
            "acquisition_context": context,
            "observation_observed_at": observation["observed_at"]}
        if observation_ordinal is not None:
            row["attempt_ordinal"] = observation_ordinal
        key = _verification_key(row)
        if key not in already:
            rows.append(row)
            already.add(key)
    _append_jsonl(state_dir / "route-verifications.jsonl", rows)
    return rows


def capability_summary(state_dir):
    from collections import Counter
    from statistics import median
    import math
    path = state_dir / "route-observations.jsonl"
    groups, invalid = {}, 0
    verified_keys, invalid_verifications, duplicate_verifications = _verification_keys(state_dir)
    if not path.exists():
        return {"groups": [], "invalid_records": 0, "duplicate_records": 0,
                "invalid_verification_records": invalid_verifications,
                "duplicate_verification_records": duplicate_verifications}
    valid_rows = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
                if (not isinstance(row, dict) or row.get("schema") != _OBSERVATION_SCHEMA
                        or row.get("outcome") not in ("observed", "failed")
                        or not isinstance(row.get("trace_id"), str)
                        or not isinstance(row.get("provider"), str)):
                    raise ValueError()
                attempt_ordinal = _attempt_ordinal(row)
                fields = ("domain", "path_pattern", "operation", "adapter",
                    "adapter_version", "adapter_binding_id", "identity_class",
                    "provider", "provider_version", "provider_binding_id",
                    "acquisition_context")
                key = tuple(row.get(field) for field in fields)
                if any(value is not None and not isinstance(value, str) for value in key): raise ValueError()
                if ((row.get("provider_binding_id") is not None
                        and _binding_id(row.get("provider_binding_id")) is None)
                        or (row.get("adapter_binding_id") is not None
                            and _binding_id(row.get("adapter_binding_id")) is None)
                        or (row.get("adapter") is None
                            and row.get("adapter_binding_id") is not None)):
                    raise ValueError()
                valid_rows.append((row, attempt_ordinal))
            except (ValueError, TypeError):
                invalid += 1
                continue
    canonical_rows, duplicates = _canonical_attempt_rows(valid_rows)
    for row, _, unambiguous in canonical_rows:
        fields = ("domain", "path_pattern", "operation", "adapter",
            "adapter_version", "adapter_binding_id", "identity_class",
            "provider", "provider_version", "provider_binding_id",
            "acquisition_context")
        key = tuple(row.get(field) for field in fields)
        group = groups.setdefault(key, {"sample_count": 0, "observed_count": 0,
            "independently_verified_count": 0, "failures": Counter(), "latencies": []})
        group["sample_count"] += 1
        group["observed_count"] += row["outcome"] == "observed"
        group["independently_verified_count"] += _independently_verified(
            row, verified_keys, unambiguous)
        if row["outcome"] == "failed":
            code = row.get("failure_code")
            group["failures"][code if isinstance(code, str) else "UNKNOWN"] += 1
        value = row.get("latency_ms")
        if type(value) in (int, float) and math.isfinite(value) and value >= 0:
            group["latencies"].append(value)
    output = []
    fields = ("domain", "path_pattern", "operation", "adapter", "adapter_version",
              "adapter_binding_id", "identity_class", "provider", "provider_version",
              "provider_binding_id", "acquisition_context")
    for key, group in groups.items():
        latencies = group.pop("latencies")
        group["failures"] = dict(group["failures"])
        output.append({**dict(zip(fields, key)), **group,
            "median_latency_ms": median(latencies) if latencies else None,
            "latency_sample_count": len(latencies), "reliability": None,
            "cost_usd": None, "tokens_to_model": None})
    return {"groups": output, "invalid_records": invalid, "duplicate_records": duplicates,
            "invalid_verification_records": invalid_verifications,
            "duplicate_verification_records": duplicate_verifications}


def provider_plan(state_dir, url, operation, adapter, adapter_version, manifests,
                  candidates, ttl, minimum, context=None, *,
                  adapter_binding_id=None,
                  require_independent_verification=True):
    """Build an exact-scope, evidence-bearing order without inventing reliability.

    ``candidates`` is already policy/configuration filtered. This function can
    promote a candidate only from current-version, current-binding observations
    for the same domain, path pattern, operation, projection and acquisition context. The
    default requires independent correctness evidence; the legacy preference
    wrapper below opts into its older transport-observation behavior explicitly.

    Execution remains sequential. Racing needs separate policy, provider
    concurrency/cost declarations and loser-cleanup accounting.
    """
    from datetime import datetime, timezone
    from statistics import median
    import math
    ordered = list(dict.fromkeys(candidates))
    neutral = {"ordered": ordered, "preferred": None, "execution": "sequential",
        "basis": "registration order; insufficient exact-scope evidence", "evidence": []}
    path = state_dir / "route-observations.jsonl"
    if (not path.exists() or ttl == 0 or type(minimum) is not int or minimum < 1
            or type(require_independent_verification) is not bool
            or not isinstance(operation, str) or not operation
            or (adapter is None and adapter_binding_id is not None)
            or (adapter_binding_id is not None and _binding_id(adapter_binding_id) is None)):
        return neutral
    try: source = urlparse(url)
    except (ValueError, TypeError): return neutral
    if not source.hostname:
        return neutral
    pattern = re.sub(r"[0-9]+", "{number}", source.path)
    now = datetime.now(timezone.utc)
    definitions = {manifest["id"]: (manifest["version"],
                       _binding_id(manifest.get("binding_id")))
                   for manifest in manifests
                   if (isinstance(manifest, dict) and manifest.get("id") in ordered
                       and isinstance(manifest.get("version"), str)
                       and (manifest.get("binding_id") is None
                            or _binding_id(manifest.get("binding_id")) is not None))}
    groups = {}
    verified_keys, _, _ = _verification_keys(state_dir)
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return neutral
    valid_rows = []
    for line in lines:
        try:
            row = json.loads(line)
            if (not isinstance(row, dict)
                    or row.get("schema") != _OBSERVATION_SCHEMA
                    or row.get("outcome") not in ("observed", "failed")
                    or not isinstance(row.get("trace_id"), str)
                    or not isinstance(row.get("provider"), str)):
                continue
            attempt_ordinal = _attempt_ordinal(row)
            valid_rows.append((row, attempt_ordinal))
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    canonical_rows, _ = _canonical_attempt_rows(valid_rows)
    for row, attempt_ordinal, unambiguous in canonical_rows:
        try:
            if (row.get("identity_class") != "public"
                    or row.get("domain") != source.hostname or row.get("path_pattern") != pattern
                    or row.get("adapter") != adapter or row.get("adapter_version") != adapter_version
                    or row.get("operation") != operation or row.get("provider") not in definitions
                    or row.get("provider_version") != definitions[row["provider"]][0]): continue
            if ((row.get("provider_binding_id") is not None
                    and _binding_id(row.get("provider_binding_id")) is None)
                    or (row.get("adapter_binding_id") is not None
                        and _binding_id(row.get("adapter_binding_id")) is None)):
                continue
            provider_binding_id = definitions[row["provider"]][1]
            if (provider_binding_id is not None
                    and row.get("provider_binding_id") != provider_binding_id):
                continue
            if (adapter_binding_id is not None
                    and row.get("adapter_binding_id") != adapter_binding_id):
                continue
            if adapter is None and row.get("adapter_binding_id") is not None:
                continue
            if row.get("acquisition_context") != context: continue
            when = datetime.fromisoformat(row["observed_at"])
            age = (now - when).total_seconds()
            if not 0 <= age <= ttl or row.get("outcome") not in ("observed", "failed"): continue
            groups.setdefault(row["provider"], []).append(
                (when, row, attempt_ordinal, unambiguous))
        except (ValueError, TypeError, KeyError, AttributeError): continue
    choices, evidence = [], []
    for position, provider in enumerate(ordered):
        records = groups.get(provider, [])
        records = _ordered_route_records(
            records, verified_keys, require_independent_verification)
        last_failure = max((index for index, (_, row, _, _) in enumerate(records)
                            if row["outcome"] == "failed"), default=-1)
        successes = [(row, unambiguous)
                     for _, row, _, unambiguous in records[last_failure+1:]
                     if row["outcome"] == "observed"]
        latencies = [row.get("latency_ms") for row, _ in successes]
        latencies = [value for value in latencies if type(value) in (int,float) and math.isfinite(value) and value >= 0]
        verified = sum(_independently_verified(
            row, verified_keys, unambiguous)
            for row, unambiguous in successes)
        enough_latency = (len(latencies) >= minimum if require_independent_verification
                          else bool(latencies))
        qualifies = (bool(records) and records[-1][1]["outcome"] == "observed"
            and len(successes) >= minimum
            and enough_latency
            and (not require_independent_verification or verified >= minimum))
        definition = definitions.get(provider)
        evidence.append({"provider": provider,
            "provider_version": definition[0] if definition else None,
            "provider_binding_id": definition[1] if definition else None,
            "sample_count": len(records), "post_failure_observed_count": len(successes),
            "post_failure_independently_verified_count": verified,
            "post_failure_latency_sample_count": len(latencies),
            "median_success_latency_ms": median(latencies) if latencies else None,
            "latest_outcome": records[-1][1]["outcome"] if records else None,
            "preference_eligible": qualifies})
        if qualifies:
            choices.append((median(latencies), position, provider))
    if not choices:
        return {**neutral, "evidence": evidence}
    promoted = [provider for _, _, provider in sorted(choices)]
    return {"ordered": promoted + [provider for provider in ordered if provider not in promoted],
        "preferred": promoted[0], "execution": "sequential",
        "basis": ("local exact-scope independently verified observations"
                  if require_independent_verification else
                  "local exact-scope observations that passed Core content checks"),
        "evidence": evidence}


def preferred_provider(state_dir, url, adapter, adapter_version, manifests, ttl, minimum,
                       context=None, *, adapter_binding_id=None):
    """Compatibility preference for repeated recent free adapter projections."""
    if not adapter:
        return None
    free = [manifest for manifest in manifests if not manifest["paid"]]
    plan = provider_plan(state_dir, url, "extract", adapter, adapter_version, free,
        [manifest["id"] for manifest in free], ttl, minimum, context,
        adapter_binding_id=adapter_binding_id,
        require_independent_verification=False)
    return plan["preferred"]
