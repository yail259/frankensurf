import json
import httpx
import pytest
from frankensurf import Runtime, WebPolicy
from frankensurf.route_memory import record_observations


@pytest.fixture(autouse=True)
def _route_memory_only(monkeypatch):
    """These tests isolate route memory; per-origin hints are tested in test_escalation.py."""
    monkeypatch.setattr(Runtime, "_origin_hint", lambda self, url, policy: None)


async def test_fresh_attempts_persist_but_cache_hits_do_not_count(tmp_path):
    async with Runtime(tmp_path, transport=httpx.MockTransport(lambda _: httpx.Response(200, text="exact content"))) as web:
        await web.read("https://example.com/items/123?token=secret", WebPolicy(provider="http"))
        await web.read("https://example.com/items/123?token=secret", WebPolicy(provider="http", freshness="cached"))
    raw = (tmp_path / "route-observations.jsonl").read_text()
    rows = [json.loads(line) for line in raw.splitlines()]
    assert len(rows) == 1 and "secret" not in raw
    assert rows[0]["path_pattern"] == "/items/{number}"
    assert rows[0]["provider_version"] == "legacy"
    assert len(rows[0]["provider_binding_id"]) == 64
    assert rows[0]["adapter_binding_id"] is None
    assert rows[0]["independent_correctness_verified"] is False


def test_named_identity_journal_omits_path_identity_and_exception_details(tmp_path):
    record_observations(tmp_path, {"url": "https://example.com/secret-path", "receipt": {
        "trace_id": "trace", "identity": "secret-identity", "operation": "read",
        "attempts": [{"provider": "local_cdp", "provider_binding_id": "a" * 64,
                      "status": "failed", "failure": "BLOCKED",
                      "message": "secret-message"}]}})
    raw = (tmp_path / "route-observations.jsonl").read_text()
    assert "secret" not in raw
    row = json.loads(raw)
    assert row["path_pattern"] is None and row["identity_class"] == "named"
    assert row["failure_code"] == "BLOCKED"


def test_benchmark_verification_joins_and_summarizes_exact_bindings(tmp_path):
    from datetime import datetime, timezone
    from frankensurf.route_memory import capability_summary, record_benchmark_verification

    provider_binding = "1" * 64
    adapter_binding = "2" * 64
    receipt = {"trace_id": "trace", "status": "observed", "identity": None,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "requested_url": "https://example.com/items/1", "operation": "extract",
        "adapter": "json", "adapter_version": "1",
        "adapter_binding_id": adapter_binding, "acquisition_context": "c" * 64,
        "attempts": [{"provider": "working", "provider_version": "1",
                      "provider_binding_id": provider_binding,
                      "status": "observed", "latency_ms": 10}]}
    record_observations(tmp_path, {"url": receipt["requested_url"], "receipt": receipt})

    class Registry:
        def __init__(self, identifier, binding_id):
            self.row = {"id": identifier, "version": "1", "binding_id": binding_id,
                        "enabled": True}
        def inspect(self):
            return [self.row]

    kwargs = {"case_id": "exact", "manifest_version": "test",
        "manifest_sha256": "a" * 64, "case_sha256": "b" * 64,
        "declared_assertions": [{"path": "structured.id", "op": "equals", "value": 1}],
        "assertion_results": [{"path": "structured.id", "op": "equals", "passed": True}]}
    assert record_benchmark_verification(tmp_path, receipt,
        providers=Registry("working", "3" * 64),
        adapters=Registry("json", adapter_binding), **kwargs) == []
    assert record_benchmark_verification(tmp_path, receipt,
        providers=Registry("working", provider_binding),
        adapters=Registry("json", "4" * 64), **kwargs) == []

    rows = record_benchmark_verification(tmp_path, receipt,
        providers=Registry("working", provider_binding),
        adapters=Registry("json", adapter_binding), **kwargs)
    assert len(rows) == 1
    assert rows[0]["provider_binding_id"] == provider_binding
    assert rows[0]["adapter_binding_id"] == adapter_binding
    group = capability_summary(tmp_path)["groups"][0]
    assert group["provider_binding_id"] == provider_binding
    assert group["adapter_binding_id"] == adapter_binding
    assert group["independently_verified_count"] == 1

    verification_path = tmp_path / "route-verifications.jsonl"
    mismatched = json.loads(verification_path.read_text())
    mismatched["provider_binding_id"] = "5" * 64
    verification_path.write_text(json.dumps(mismatched) + "\n")
    assert capability_summary(tmp_path)["groups"][0][
        "independently_verified_count"] == 0


def test_post_upgrade_verification_joins_one_legacy_observation(tmp_path):
    from datetime import datetime, timezone
    from frankensurf.route_memory import (
        capability_summary, record_benchmark_verification)

    binding = "1" * 64
    receipt = {"trace_id": "legacy-delayed", "status": "observed",
        "identity": None, "observed_at": datetime.now(timezone.utc).isoformat(),
        "requested_url": "https://legacy.example/items/1", "operation": "read",
        "adapter": None, "adapter_version": None, "adapter_binding_id": None,
        "acquisition_context": "2" * 64,
        "attempts": [{"provider": "working", "provider_version": "1",
                      "provider_binding_id": binding,
                      "status": "observed", "latency_ms": 10}]}
    record_observations(
        tmp_path, {"url": receipt["requested_url"], "receipt": receipt})
    observation_path = tmp_path / "route-observations.jsonl"
    observation = json.loads(observation_path.read_text())
    observation.pop("attempt_ordinal")
    observation_path.write_text(json.dumps(observation) + "\n")

    class Registry:
        def inspect(self):
            return [{"id": "working", "version": "1",
                     "binding_id": binding, "enabled": True}]

    rows = record_benchmark_verification(
        tmp_path, receipt, providers=Registry(), case_id="legacy-delayed",
        manifest_version="test", manifest_sha256="a" * 64,
        case_sha256="b" * 64,
        declared_assertions=[{"path": "title", "op": "nonempty"}],
        assertion_results=[{"path": "title", "op": "nonempty",
                            "passed": True}])
    assert len(rows) == 1
    assert "attempt_ordinal" not in rows[0]
    persisted = json.loads(
        (tmp_path / "route-verifications.jsonl").read_text())
    assert "attempt_ordinal" not in persisted
    summary = capability_summary(tmp_path)
    assert summary["invalid_verification_records"] == 0
    assert summary["groups"][0]["independently_verified_count"] == 1


@pytest.mark.parametrize("failure_first", [True, False])
def test_conflicting_legacy_failure_blocks_delayed_exact_verification(
        tmp_path, failure_first):
    from datetime import datetime, timezone
    from frankensurf.route_memory import record_benchmark_verification

    binding = "1" * 64
    receipt = {"trace_id": "mixed-upgrade", "status": "observed",
        "identity": None, "observed_at": datetime.now(timezone.utc).isoformat(),
        "requested_url": "https://legacy.example/items/1", "operation": "read",
        "adapter": None, "adapter_version": None, "adapter_binding_id": None,
        "acquisition_context": "2" * 64,
        "attempts": [{"provider": "working", "provider_version": "1",
                      "provider_binding_id": binding,
                      "status": "observed", "latency_ms": 10}]}
    record_observations(
        tmp_path, {"url": receipt["requested_url"], "receipt": receipt})
    observation_path = tmp_path / "route-observations.jsonl"
    exact = json.loads(observation_path.read_text())
    legacy_failure = {**exact, "outcome": "failed",
        "failure_code": "BLOCKED", "latency_ms": 2,
        # Canonicalize the acquisition before scope filtering; otherwise this
        # conflicting row could be hidden while the exact success is verified.
        "acquisition_context": "3" * 64}
    legacy_failure.pop("attempt_ordinal")
    rows = ([legacy_failure, exact] if failure_first
            else [exact, legacy_failure])
    observation_path.write_text(
        "".join(json.dumps(item) + "\n" for item in rows))

    class Registry:
        def inspect(self):
            return [{"id": "working", "version": "1",
                     "binding_id": binding, "enabled": True}]

    appended = record_benchmark_verification(
        tmp_path, receipt, providers=Registry(), case_id="mixed-upgrade",
        manifest_version="test", manifest_sha256="a" * 64,
        case_sha256="b" * 64,
        declared_assertions=[{"path": "title", "op": "nonempty"}],
        assertion_results=[{"path": "title", "op": "nonempty",
                            "passed": True}])
    assert appended == []
    assert not (tmp_path / "route-verifications.jsonl").exists()


def test_legacy_observation_does_not_wildcard_a_same_provider_retry(tmp_path):
    from datetime import datetime, timezone
    from frankensurf.route_memory import record_benchmark_verification

    binding = "1" * 64
    receipt = {"trace_id": "legacy-retry", "status": "observed",
        "identity": None, "observed_at": datetime.now(timezone.utc).isoformat(),
        "requested_url": "https://legacy.example/items/1", "operation": "read",
        "adapter": None, "adapter_version": None, "adapter_binding_id": None,
        "acquisition_context": "2" * 64,
        "attempts": [
            {"provider": "working", "provider_version": "1",
             "provider_binding_id": binding, "status": "failed",
             "failure": "PROVIDER_DOWN", "latency_ms": 1},
            {"provider": "working", "provider_version": "1",
             "provider_binding_id": binding, "status": "observed",
             "latency_ms": 10}]}
    record_observations(
        tmp_path, {"url": receipt["requested_url"], "receipt": receipt})
    observation_path = tmp_path / "route-observations.jsonl"
    observed = [json.loads(line) for line in
                observation_path.read_text().splitlines()
                if json.loads(line)["outcome"] == "observed"]
    observed[0].pop("attempt_ordinal")
    observation_path.write_text(json.dumps(observed[0]) + "\n")

    class Registry:
        def inspect(self):
            return [{"id": "working", "version": "1",
                     "binding_id": binding, "enabled": True}]

    rows = record_benchmark_verification(
        tmp_path, receipt, providers=Registry(), case_id="legacy-retry",
        manifest_version="test", manifest_sha256="a" * 64,
        case_sha256="b" * 64,
        declared_assertions=[{"path": "title", "op": "nonempty"}],
        assertion_results=[{"path": "title", "op": "nonempty",
                            "passed": True}])
    assert rows == []
    assert not (tmp_path / "route-verifications.jsonl").exists()


def test_ambiguous_legacy_retry_rows_cannot_verify_a_retained_failure(
        tmp_path):
    from datetime import datetime, timezone
    from frankensurf.route_memory import record_benchmark_verification

    binding = "1" * 64
    receipt = {"trace_id": "legacy-journal-retry", "status": "observed",
        "identity": None, "observed_at": datetime.now(timezone.utc).isoformat(),
        "requested_url": "https://legacy.example/items/1", "operation": "read",
        "adapter": None, "adapter_version": None, "adapter_binding_id": None,
        "acquisition_context": "2" * 64,
        "attempts": [
            {"provider": "working", "provider_version": "1",
             "provider_binding_id": binding, "status": "failed",
             "failure": "PROVIDER_DOWN", "latency_ms": 1},
            {"provider": "working", "provider_version": "1",
             "provider_binding_id": binding, "status": "observed",
             "latency_ms": 10}]}
    record_observations(
        tmp_path, {"url": receipt["requested_url"], "receipt": receipt})
    observation_path = tmp_path / "route-observations.jsonl"
    observations = [json.loads(line) for line in
                    observation_path.read_text().splitlines()]
    for observation in observations:
        observation.pop("attempt_ordinal")
    observation_path.write_text("".join(
        json.dumps(observation) + "\n" for observation in observations))
    single_success_receipt = {
        **receipt, "attempts": [receipt["attempts"][1]]}

    class Registry:
        def inspect(self):
            return [{"id": "working", "version": "1",
                     "binding_id": binding, "enabled": True}]

    rows = record_benchmark_verification(
        tmp_path, single_success_receipt, providers=Registry(),
        case_id="legacy-journal-retry", manifest_version="test",
        manifest_sha256="a" * 64, case_sha256="b" * 64,
        declared_assertions=[{"path": "title", "op": "nonempty"}],
        assertion_results=[{"path": "title", "op": "nonempty",
                            "passed": True}])
    assert rows == []
    assert not (tmp_path / "route-verifications.jsonl").exists()


async def test_retry_attempts_have_distinct_route_evidence_and_verification(
        tmp_path, monkeypatch):
    from frankensurf import providers
    from frankensurf.providers import ProviderManifest, ProviderRegistry
    from frankensurf.route_memory import (
        capability_summary, provider_plan, record_benchmark_verification)
    from frankensurf.runtime import WebFailure

    class RetryProvider:
        manifest = ProviderManifest("retry_route", "1")

        def __init__(self):
            self.calls = 0

        async def acquire(self, request, services):
            self.calls += 1
            if self.calls == 1:
                raise WebFailure("PROVIDER_DOWN", "transient route fixture")
            return {"url": request.url,
                    "content": "<h1>Retry route success</h1>",
                    "content_type": "text/html", "http_status": 200}

    registry = ProviderRegistry()
    registry.register(RetryProvider())
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    target = "https://retry-route.example/items/1"
    checks = [{"path": "title", "op": "equals",
               "value": "Retry route success"}]
    outcomes = [{"path": "title", "op": "equals", "passed": True}]

    async with Runtime(tmp_path) as web:
        result = await web.read(target, WebPolicy(
            provider="retry_route",
            provider_max_attempts_per_candidate=2,
            provider_retry_delay_seconds=0))
        receipt = result["receipt"]
        verifications = record_benchmark_verification(
            tmp_path, receipt, providers=web.providers,
            case_id="retry-route", manifest_version="test",
            manifest_sha256="a" * 64, case_sha256="b" * 64,
            declared_assertions=checks, assertion_results=outcomes)
        manifests = web.providers.inspect()

    observations = [json.loads(line) for line in
        (tmp_path / "route-observations.jsonl").read_text().splitlines()]
    assert [(row["attempt_ordinal"], row["outcome"])
            for row in observations] == [(0, "failed"), (1, "observed")]
    assert len(verifications) == 1
    assert verifications[0]["attempt_ordinal"] == 1
    summary = capability_summary(tmp_path)
    assert summary["invalid_records"] == 0
    assert summary["duplicate_records"] == 0
    group = summary["groups"][0]
    assert group["sample_count"] == 2
    assert group["observed_count"] == 1
    assert group["failures"] == {"PROVIDER_DOWN": 1}
    assert group["independently_verified_count"] == 1

    plan = provider_plan(
        tmp_path, target, "read", None, None, manifests, ["retry_route"],
        ttl=3600, minimum=1, context=receipt["acquisition_context"])
    assert plan["preferred"] == "retry_route"
    assert plan["evidence"] == [{
        "provider": "retry_route", "provider_version": "1",
        "provider_binding_id": receipt["attempts"][1]["provider_binding_id"],
        "sample_count": 2, "post_failure_observed_count": 1,
        "post_failure_independently_verified_count": 1,
        "post_failure_latency_sample_count": 1,
        "median_success_latency_ms": observations[1]["latency_ms"],
        "latest_outcome": "observed", "preference_eligible": True}]


def test_legacy_route_journals_without_attempt_ordinal_still_join_and_train(
        tmp_path):
    import hashlib
    from datetime import datetime, timezone
    from frankensurf.route_memory import capability_summary, provider_plan

    observed_at = datetime.now(timezone.utc).isoformat()
    binding = "1" * 64
    context = "2" * 64
    declared = [{"path": "title", "op": "nonempty"}]
    outcomes = [{"path": "title", "op": "nonempty", "passed": True}]
    observation = {
        "schema": "frankensurf.route-observation/v1", "trace_id": "legacy",
        "observed_at": observed_at, "identity_class": "public",
        "domain": "legacy.example", "path_pattern": "/items/{number}",
        "operation": "read", "adapter": None, "adapter_version": None,
        "adapter_binding_id": None, "acquisition_context": context,
        "provider": "legacy_provider", "provider_version": "1",
        "provider_binding_id": binding, "outcome": "observed",
        "failure_code": None, "latency_ms": 4,
        "cost_usd": None, "tokens_to_model": None,
        "independent_correctness_verified": False}
    verification = {
        "schema": "frankensurf.route-verification/v1",
        "verified_at": observed_at,
        "verification_basis": "frankenbench declared assertions passed",
        "benchmark_case_id": "legacy", "benchmark_manifest_version": "test",
        "benchmark_manifest_sha256": "a" * 64,
        "benchmark_case_sha256": "b" * 64,
        "declared_assertions": declared, "assertion_results": outcomes,
        "assertion_set_sha256": hashlib.sha256(json.dumps(
            declared, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "assertion_results_sha256": hashlib.sha256(json.dumps(
            outcomes, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "trace_id": "legacy", "provider": "legacy_provider",
        "provider_version": "1", "provider_binding_id": binding,
        "operation": "read", "adapter": None, "adapter_version": None,
        "adapter_binding_id": None, "acquisition_context": context,
        "observation_observed_at": observed_at}
    (tmp_path / "route-observations.jsonl").write_text(
        json.dumps(observation) + "\n")
    (tmp_path / "route-verifications.jsonl").write_text(
        json.dumps(verification) + "\n")

    summary = capability_summary(tmp_path)
    assert summary["invalid_records"] == 0
    assert summary["invalid_verification_records"] == 0
    assert summary["groups"][0]["independently_verified_count"] == 1
    plan = provider_plan(
        tmp_path, "https://legacy.example/items/7", "read", None, None,
        [{"id": "legacy_provider", "version": "1", "binding_id": binding}],
        ["legacy_provider"], ttl=3600, minimum=1, context=context)
    assert plan["preferred"] == "legacy_provider"
    assert plan["evidence"][0]["sample_count"] == 1
    assert plan["evidence"][0]["post_failure_independently_verified_count"] == 1


async def test_capability_summary_persists_versions_counts_and_unknown_reliability(tmp_path):
    from frankensurf.route_memory import capability_summary
    async with Runtime(tmp_path, transport=httpx.MockTransport(lambda _: httpx.Response(200, text="exact"))) as web:
        await web.read("https://example.com/items/1", WebPolicy(provider="http"))
        await web.read("https://example.com/items/2", WebPolicy(provider="http"))
        group = web.route_capabilities()["groups"][0]
    assert group["sample_count"] == 2 and group["observed_count"] == 2
    assert group["provider_version"] == "legacy" and group["latency_sample_count"] == 2
    assert group["reliability"] is None and group["independently_verified_count"] == 0
    assert capability_summary(tmp_path)["groups"][0] == group


def test_summary_reports_corrupt_and_duplicate_rows(tmp_path):
    from frankensurf.route_memory import capability_summary
    row = {"schema": "frankensurf.route-observation/v1", "trace_id": "a", "provider": "http", "outcome": "failed", "failure_code": "BLOCKED", "latency_ms": True}
    (tmp_path / "route-observations.jsonl").write_text(json.dumps(row)+"\n"+json.dumps(row)+"\n{broken\n")
    result = capability_summary(tmp_path)
    assert result["invalid_records"] == 1 and result["duplicate_records"] == 1
    assert result["groups"][0]["failures"] == {"BLOCKED": 1}
    assert result["groups"][0]["median_latency_ms"] is None


@pytest.mark.parametrize("attempt_ordinal", [-1, True, 1.5, "1", None])
def test_summary_rejects_present_noninteger_attempt_ordinals(
        tmp_path, attempt_ordinal):
    from frankensurf.route_memory import capability_summary

    row = {"schema": "frankensurf.route-observation/v1", "trace_id": "a",
           "provider": "http", "outcome": "observed",
           "attempt_ordinal": attempt_ordinal}
    (tmp_path / "route-observations.jsonl").write_text(
        json.dumps(row) + "\n")
    result = capability_summary(tmp_path)
    assert result["invalid_records"] == 1
    assert result["groups"] == []


async def test_learned_order_survives_runtime_restart_and_respects_override(tmp_path, monkeypatch):
    from frankensurf import providers
    from frankensurf.providers import ProviderRegistry, ProviderManifest
    class Plugin:
        def __init__(self, identifier):
            from frankensurf.providers import ProviderManifest as Manifest
            self.manifest = Manifest(identifier, "1")
        async def acquire(self, request, services):
            from frankensurf.runtime import WebFailure
            if self.manifest.id == "blocked": raise WebFailure("BLOCKED", "blocked")
            return {"url": request.url, "content": '{"id":1}', "content_type": "application/json", "http_status": 200}
    registry = ProviderRegistry(); registry.register(Plugin("blocked")); registry.register(Plugin("working"))
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    from frankensurf.route_memory import record_benchmark_verification
    policy = WebPolicy(route_memory_min_samples=2)
    async with Runtime(tmp_path) as web:
        for number in (1,2):
            result = await web.extract("https://example.com/items/"+str(number), "json", policy)
            record_benchmark_verification(tmp_path, result["receipt"], case_id="exact",
                manifest_version="test", manifest_sha256="a"*64, case_sha256="b"*64,
                declared_assertions=[{"path":"structured.id","op":"equals","value":1}],
                assertion_results=[{"path":"structured.id","op":"equals","passed":True}])
    observations = [json.loads(line) for line in
                    (tmp_path / "route-observations.jsonl").read_text().splitlines()]
    verifications = (tmp_path / "route-verifications.jsonl").read_text().splitlines()
    assert all(item["independent_correctness_verified"] is False for item in observations)
    assert len(verifications) == 2
    assert (tmp_path / "route-verifications.jsonl").stat().st_mode & 0o777 == 0o600
    async with Runtime(tmp_path) as web:
        learned = await web.extract("https://example.com/items/3", "json", policy)
        assert [item["provider"] for item in learned["receipt"]["attempts"]] == [
            "working"]
        assert learned["receipt"]["routing"]["memory_provider"] == "working"
        explicit = await web.extract("https://example.com/items/4", "json",
            WebPolicy(provider_candidates=("blocked", "working")))
        assert [item["provider"] for item in explicit["receipt"]["attempts"]] == [
            "blocked", "working"]
        assert "provider_plan" not in explicit["receipt"].get("routing", {})
        expired = await web.extract("https://example.com/items/5", "json",
            WebPolicy(route_memory_ttl_seconds=0))
        assert [item["provider"] for item in expired["receipt"]["attempts"]] == [
            "blocked", "working"]


async def test_content_checked_observations_reorder_an_exact_path(
        tmp_path, monkeypatch):
    from frankensurf import providers
    from frankensurf.providers import ProviderRegistry, ProviderManifest
    from frankensurf.runtime import WebFailure

    class Plugin:
        def __init__(self, identifier, failure=None):
            self.manifest = ProviderManifest(identifier, "1")
            self.failure = failure
        async def acquire(self, request, services):
            if self.failure:
                raise WebFailure(self.failure, "fixture failure")
            return {"url": request.url,
                    "content": "<h1>Route</h1>" + "<p>listing text</p>" * 20,
                    "content_type": "text/html", "http_status": 200}

    registry = ProviderRegistry()
    registry.register(Plugin("direct", "BLOCKED"))
    registry.register(Plugin("mature_browser"))
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    # Origin hints off, so only route memory can reorder.
    policy = WebPolicy(route_memory_min_samples=2, origin_route_hint_ttl_seconds=0)

    async with Runtime(tmp_path) as web:
        for number in (1, 2):
            first = await web.read(
                "https://new-provider-scope.example/items/" + str(number), policy)
            assert [attempt["provider"] for attempt in first["receipt"]["attempts"]] == [
                "direct", "mature_browser"]
        learned = await web.read(
            "https://new-provider-scope.example/items/3", policy)
        other_path = await web.read(
            "https://new-provider-scope.example/about", policy)

    assert [attempt["provider"] for attempt in learned["receipt"]["attempts"]] == [
        "mature_browser"]
    plan = learned["receipt"]["routing"]["provider_plan"]
    assert plan["preferred"] == "mature_browser"
    assert plan["basis"] == "local exact-scope observations that passed Core content checks"
    assert plan["reliability"] is None
    assert [attempt["provider"] for attempt in other_path["receipt"]["attempts"]] == [
        "direct", "mature_browser"]


async def test_verified_local_route_supersedes_a_provisional_catalog_seed(
        tmp_path, monkeypatch):
    from frankensurf import experimental, providers
    from frankensurf.providers import ProviderRegistry, ProviderManifest
    from frankensurf.runtime import WebFailure
    from frankensurf.route_memory import record_benchmark_verification

    class Plugin:
        def __init__(self, identifier, version, failure=None):
            self.manifest = ProviderManifest(identifier, version)
            self.failure = failure
        def available(self, configured):
            return True
        async def acquire(self, request, services):
            if self.failure:
                raise WebFailure(self.failure, "fixture failure")
            return {"url": request.url,
                    "content": "<h1>Verified fallback</h1>",
                    "content_type": "text/html", "http_status": 200}

    registry = ProviderRegistry()
    registry.register(Plugin("recovery", "1"))
    registry.register(Plugin("camoufox", "legacy", "BLOCKED"))
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    monkeypatch.setattr(experimental, "installed", lambda _: True)
    policy = WebPolicy(route_memory_min_samples=2)
    checks = [{"path": "title", "op": "equals",
               "value": "Verified fallback"}]
    outcomes = [{"path": "title", "op": "equals", "passed": True}]
    target = "https://www.ebay.com.au/itm/123456789"

    async with Runtime(tmp_path) as web:
        for _ in range(2):
            result = await web.read(target, policy_overrides={
                "route_memory_min_samples": 2})
            assert [attempt["provider"] for attempt in result["receipt"][
                "attempts"]] == ["camoufox", "recovery"]
            record_benchmark_verification(tmp_path, result["receipt"],
                providers=web.providers, case_id="seed-replacement",
                manifest_version="test", manifest_sha256="a" * 64,
                case_sha256="b" * 64, declared_assertions=checks,
                assertion_results=outcomes)
        learned = await web.read(target, policy_overrides={
            "route_memory_min_samples": 2})

    assert [attempt["provider"] for attempt in learned["receipt"]["attempts"]] == [
        "recovery"]
    routing = learned["receipt"]["routing"]
    assert routing["catalog_seeds"][0]["reliability"] is None
    assert routing["provider_plan"]["baseline"] == (
        "typed provisional compatibility seed order")
    assert routing["provider_plan"]["preferred"] == "recovery"


def test_latest_failure_and_changed_version_invalidate_preference(tmp_path):
    from datetime import datetime, timezone
    from frankensurf.route_memory import preferred_provider
    rows = []
    for index, outcome in enumerate(("observed", "observed", "failed")):
        rows.append({"schema": "frankensurf.route-observation/v1", "trace_id": str(index),
            "identity_class": "public", "domain": "example.com", "path_pattern": "/items/{number}",
            "operation": "extract", "adapter": "json", "adapter_version": "legacy",
            "provider": "working", "provider_version": "1", "outcome": outcome,
            "latency_ms": 10, "observed_at": datetime.now(timezone.utc).isoformat()})
    path = tmp_path / "route-observations.jsonl"
    path.write_text("".join(json.dumps(row)+"\n" for row in rows))
    manifest = [{"id": "working", "version": "1", "paid": False}]
    assert preferred_provider(tmp_path, "https://example.com/items/1", "json", "legacy", manifest, 3600, 2) is None
    path.write_text("".join(json.dumps(row)+"\n" for row in rows[:2]))
    manifest[0]["version"] = "2"
    assert preferred_provider(tmp_path, "https://example.com/items/1", "json", "legacy", manifest, 3600, 2) is None


def test_route_must_reearn_samples_after_failure(tmp_path):
    from datetime import datetime, timezone, timedelta
    from frankensurf.route_memory import preferred_provider
    base = datetime.now(timezone.utc) - timedelta(seconds=10)
    outcomes = ["observed", "observed", "failed", "observed", "observed"]
    rows = [{"schema": "frankensurf.route-observation/v1", "trace_id": str(i),
        "identity_class": "public", "domain": "example.com", "path_pattern": "/items/{number}",
        "operation": "extract", "adapter": "json", "adapter_version": "legacy",
        "provider": "working", "provider_version": "1", "outcome": outcome,
        "latency_ms": 10, "observed_at": (base+timedelta(seconds=i)).isoformat()}
        for i, outcome in enumerate(outcomes)]
    path = tmp_path / "route-observations.jsonl"
    manifests = [{"id": "working", "version": "1", "paid": False}]
    path.write_text("".join(json.dumps(row)+"\n" for row in rows[:4]))
    assert preferred_provider(tmp_path, "https://example.com/items/1", "json", "legacy", manifests, 3600, 2) is None
    path.write_text("".join(json.dumps(row)+"\n" for row in rows))
    assert preferred_provider(tmp_path, "https://example.com/items/1", "json", "legacy", manifests, 3600, 2) == "working"
    assert preferred_provider(tmp_path, "https://[", "json", "legacy", manifests, 3600, 2) is None


async def test_invalid_url_remains_typed_when_route_journal_exists(tmp_path):
    (tmp_path / "route-observations.jsonl").write_text("{}\n")
    async with Runtime(tmp_path) as web:
        result = await web.extract("https://[", "json")
    assert result["receipt"]["failure"]["code"] == "INVALID_URL"


def test_route_memory_does_not_mix_acquisition_contexts(tmp_path):
    from datetime import datetime, timezone
    from frankensurf.route_memory import preferred_provider
    rows = [{"schema": "frankensurf.route-observation/v1", "trace_id": str(i),
        "identity_class": "public", "domain": "example.com", "path_pattern": "/items/{number}",
        "operation": "extract", "adapter": "json", "adapter_version": "legacy",
        "provider": "working", "provider_version": "1", "outcome": "observed",
        "latency_ms": 10, "observed_at": datetime.now(timezone.utc).isoformat(),
        "acquisition_context": "direct"} for i in range(3)]
    (tmp_path / "route-observations.jsonl").write_text("".join(json.dumps(row)+"\n" for row in rows))
    manifests = [{"id": "working", "version": "1", "paid": False}]
    assert preferred_provider(tmp_path, "https://example.com/items/1", "json", "legacy", manifests, 3600, 3, "direct") == "working"
    assert preferred_provider(tmp_path, "https://example.com/items/1", "json", "legacy", manifests, 3600, 3, "category") is None
    assert preferred_provider(tmp_path, "https://example.com/items/1", "json", "legacy", manifests, 3600, 3) is None


def test_capability_summary_rejects_nonstring_policy_context(tmp_path):
    from frankensurf.route_memory import capability_summary
    row = {"schema": "frankensurf.route-observation/v1", "trace_id": "a", "provider": "http",
           "outcome": "observed", "acquisition_context": {"untrusted": "value"}}
    (tmp_path / "route-observations.jsonl").write_text(json.dumps(row)+"\n")
    result = capability_summary(tmp_path)
    assert result["invalid_records"] == 1 and result["groups"] == []
