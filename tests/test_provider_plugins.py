import asyncio
import hashlib
import json
import pytest
from frankensurf import Runtime,WebPolicy
from frankensurf import providers
from frankensurf.providers import (
    ProviderManifest, ProviderRegistry, ProviderRequest, ProviderServices)
from frankensurf.repair import RepairProviderRequest
from frankensurf.runtime import WebFailure


class Custom:
    manifest=ProviderManifest("custom","1")
    calls=0
    async def acquire(self,request,services):
        self.calls+=1
        return {"url":request.url,"content":"<h1>Exact content</h1>","content_type":"text/html","http_status":200,"headers":{"set-cookie":"secret"},"unexpected":"secret"}


async def test_custom_provider_works_without_new_runtime_branch(tmp_path,monkeypatch):
    registry=ProviderRegistry();plugin=Custom();registry.register(plugin)
    monkeypatch.setattr(providers,"DEFAULT_PROVIDERS",registry)
    async with Runtime(tmp_path) as web:result=await web.read("https://example.com/item",WebPolicy(provider="custom"))
    assert result["receipt"]["status"] == "observed" and result["receipt"]["provider_version"] == "1"
    assert "Exact content" in result["text"] and "unexpected" not in result
    assert "set-cookie" not in result["headers"]
    assert [item["provider"] for item in result["receipt"]["attempts"]] == [
        "custom"]


async def test_disabled_provider_never_executes(tmp_path,monkeypatch):
    registry=ProviderRegistry();plugin=Custom();registry.register(plugin);registry.enable("custom",False)
    monkeypatch.setattr(providers,"DEFAULT_PROVIDERS",registry)
    async with Runtime(tmp_path) as web:result=await web.read("https://example.com/item",WebPolicy(provider="custom"))
    assert result["receipt"]["failure"]["code"] == "PLUGIN_DISABLED" and plugin.calls==0


async def test_paid_provider_needs_grant_and_missing_cost_stays_unknown(tmp_path,monkeypatch):
    class Paid(Custom):manifest=ProviderManifest("paid","1",paid=True)
    registry=ProviderRegistry();plugin=Paid();registry.register(plugin);monkeypatch.setattr(providers,"DEFAULT_PROVIDERS",registry)
    async with Runtime(tmp_path) as web:
        denied=await web.read("https://example.com/item",WebPolicy(provider="paid"))
        assert denied["receipt"]["failure"]["code"] == "POLICY_DENIED" and plugin.calls==0
        result=await web.read("https://example.com/item",WebPolicy(provider="paid",allow_paid_fallbacks=True))
    assert result["receipt"]["status"] == "observed" and result["receipt"]["cost_usd"] is None


async def test_custom_provider_cannot_substitute_for_unknown_identity(tmp_path,monkeypatch):
    registry=ProviderRegistry();plugin=Custom();registry.register(plugin);monkeypatch.setattr(providers,"DEFAULT_PROVIDERS",registry)
    async with Runtime(tmp_path) as web:result=await web.read("https://example.com/item",WebPolicy(provider="custom",identity="missing"))
    assert result["receipt"]["status"] == "failed" and plugin.calls==0


async def test_runtime_snapshot_ignores_later_provider_disable(tmp_path,monkeypatch):
    registry=ProviderRegistry();plugin=Custom();registry.register(plugin);monkeypatch.setattr(providers,"DEFAULT_PROVIDERS",registry)
    async with Runtime(tmp_path) as web:
        first=await web.read("https://example.com/item",WebPolicy(provider="custom"))
        assert first["receipt"]["status"] == "observed"
        registry.enable("custom",False)
        second=await web.read("https://example.com/item",WebPolicy(provider="custom",freshness="cached"))
    assert second["receipt"]["status"] == "observed"
    assert second["receipt"]["cache_hit"]
    assert len(first["receipt"]["attempts"]) == 1


async def test_new_runtime_snapshot_scopes_new_provider_version_cache(tmp_path,monkeypatch):
    registry=ProviderRegistry();old=Custom();registry.register(old);monkeypatch.setattr(providers,"DEFAULT_PROVIDERS",registry)
    class Updated(Custom):
        manifest=ProviderManifest("custom","2")
        async def acquire(self,request,services):
            response=await super().acquire(request,services)
            response["content"]="<h1>Updated content</h1>"
            return response
    async with Runtime(tmp_path) as web:
        first=await web.read("https://example.com/item",WebPolicy(provider="custom"))
        newer=ProviderRegistry();plugin=Updated();newer.register(plugin);monkeypatch.setattr(providers,"DEFAULT_PROVIDERS",newer)
        second=await web.read("https://example.com/item",WebPolicy(provider="custom",freshness="hour"))
    async with Runtime(tmp_path) as web:
        third=await web.read("https://example.com/item",WebPolicy(provider="custom",freshness="hour"))
    assert first["receipt"]["provider_version"] == "1"
    assert second["receipt"]["provider_version"] == "1" and second["receipt"]["cache_hit"]
    assert third["receipt"]["provider_version"] == "2" and not third["receipt"]["cache_hit"]
    assert "Updated content" in third["text"]


@pytest.mark.parametrize("raw_size,expected", [(64, "observed"), (65, "failed")])
async def test_raw_provider_bytes_obey_budget_before_storage(tmp_path, monkeypatch, raw_size, expected):
    class RawProvider(Custom):
        async def acquire(self, request, services):
            response = await super().acquire(request, services)
            response["raw"] = b"x" * raw_size
            return response
    registry = ProviderRegistry()
    registry.register(RawProvider())
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    async with Runtime(tmp_path) as web:
        result = await web.read("https://example.com/item", WebPolicy(provider="custom", max_bytes=64))
    assert result["receipt"]["status"] == expected
    if expected == "failed":
        assert result["receipt"]["failure"]["code"] == "LIMIT_EXCEEDED"
        assert not result.get("artifacts")
        assert b"x" * raw_size not in [f.read_bytes() for f in tmp_path.rglob("*") if f.is_file()]


async def test_sequence_cache_uses_one_snapshot_then_next_runtime_sees_changes(tmp_path, monkeypatch):
    class Versioned(Custom):
        def __init__(self, identifier, version):
            self.manifest = ProviderManifest(identifier, version)
            self.calls = 0
        async def acquire(self, request, services):
            response = await super().acquire(request, services)
            response["content"] = "<h1>" + self.manifest.version + "</h1>"
            return response
    first = Versioned("first", "1")
    backup = Versioned("backup", "1")
    registry = ProviderRegistry()
    registry.register(first); registry.register(backup)
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    async with Runtime(tmp_path) as web:
        policy = WebPolicy(provider_candidates=("first", "backup"), freshness="cached")
        initial = await web.read("https://example.com/item", policy)
        cached = await web.read("https://example.com/item", policy)
        assert cached["receipt"]["cache_hit"]
        assert len(initial["receipt"]["attempts"]) == 1
        newer = ProviderRegistry()
        updated = Versioned("first", "2")
        newer.register(updated); newer.register(backup)
        monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", newer)
        result = await web.read("https://example.com/item", policy)
        assert result["receipt"]["cache_hit"]
        assert result["receipt"]["provider_version"] == "1"
        newer.enable("first", False)
        fallback = await web.read("https://example.com/item", policy)
        assert fallback["receipt"]["cache_hit"] and fallback["receipt"]["method"] == "first"
    async with Runtime(tmp_path) as web:
        next_snapshot = await web.read("https://example.com/item", policy)
    assert not next_snapshot["receipt"]["cache_hit"]
    assert next_snapshot["receipt"]["method"] == "backup"
    assert next_snapshot["receipt"]["attempts"][0]["failure"] == "PLUGIN_DISABLED"
    assert initial["receipt"]["provider_version"] == "1"


async def test_required_rendering_skips_nonrendering_sequence_candidate(tmp_path, monkeypatch):
    class Rendering(Custom):
        manifest = ProviderManifest("rendering", "1", rendering=True)
        async def acquire(self, request, services):
            # A rendered page with real text; near-empty renders are EMPTY_PAGE.
            return {"url": request.url, "content": "<h1>Exact content</h1><p>" + "Rendered listing text. " * 4 + "</p>",
                    "content_type": "text/html", "http_status": 200, "headers": {}}
    registry = ProviderRegistry()
    plain, rendered = Custom(), Rendering()
    registry.register(plain); registry.register(rendered)
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    async with Runtime(tmp_path) as web:
        result = await web.read("https://example.com/item", WebPolicy(
            render=True, provider_candidates=("custom", "rendering")))
    assert [item["provider"] for item in result["receipt"]["attempts"]] == [
        "custom", "rendering"]
    assert result["receipt"]["status"] == "observed"
    assert result["receipt"]["method"] == "rendering"
    assert result["receipt"]["attempts"][0]["failure"] == "VISUAL_REQUIRED"


async def test_explicit_nonrendering_provider_fails_before_execution(tmp_path, monkeypatch):
    registry = ProviderRegistry(); plugin = Custom(); registry.register(plugin)
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    async with Runtime(tmp_path) as web:
        result = await web.read("https://example.com/item", WebPolicy(render=True, provider="custom"))
    assert result["receipt"]["failure"]["code"] == "VISUAL_REQUIRED"
    assert plugin.calls == 0 and result["receipt"]["attempts"] == []


async def test_default_routing_reaches_registered_free_plugin(tmp_path, monkeypatch):
    class Failing(Custom):
        async def acquire(self, request, services):
            self.calls += 1
            from frankensurf.runtime import WebFailure
            raise WebFailure("BLOCKED", "blocked")
    class Recovery(Custom):
        manifest = ProviderManifest("recovery", "1")
    registry = ProviderRegistry(); first, recovery = Failing(), Recovery()
    registry.register(first); registry.register(recovery)
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    async with Runtime(tmp_path) as web:
        result = await web.read("https://example.com/item")
    assert result["receipt"]["status"] == "observed"
    assert [item["status"] for item in result["receipt"]["attempts"]] == [
        "failed", "observed"]
    assert result["receipt"]["method"] == "recovery"


def test_candidate_eligibility_excludes_identity_paid_disabled_and_local(monkeypatch):
    registry = ProviderRegistry()
    for identifier, kwargs in [
            ("plain", {}),
            ("identity", {"authentication": True}),
            ("paid", {"paid": True}),
            ("browser", {
                "requires_local_browser": True, "rendering": True}),
            ("route_scoped", {"route_scope_required": True}),
            ("disabled", {})]:
        plugin = Custom(); plugin.manifest = ProviderManifest(identifier, "1", **kwargs)
        registry.register(plugin)
    registry.enable("disabled", False)
    assert registry.candidates(WebPolicy(allow_local_browser=False)) == ["plain"]
    assert registry.candidates(WebPolicy(render=True)) == ["browser"]
    assert registry.candidates(WebPolicy(identity="owner")) == []
    assert registry.is_available("route_scoped")


async def test_unseen_domain_escalates_across_only_operation_capable_plugins(
        tmp_path, monkeypatch):
    calls = []
    class Plugin:
        def __init__(self, identifier, operations, failure=None):
            self.manifest = ProviderManifest(
                identifier, "1", operations=operations)
            self.failure = failure
        def available(self, configured):
            return True
        async def acquire(self, request, services):
            calls.append((self.manifest.id, request.operation))
            if self.failure:
                from frankensurf.runtime import WebFailure
                raise WebFailure(self.failure, "fixture failure")
            return {"url": request.url,
                    "content": "<h1>Recovered unseen domain</h1>",
                    "content_type": "text/html", "http_status": 200}
    registry = ProviderRegistry()
    registry.register(Plugin("direct_http", ("read", "extract"), "BLOCKED"))
    registry.register(Plugin("write_executor", ("do",)))
    registry.register(Plugin("extract_api", ("extract",)))
    registry.register(Plugin("rendered_browser", ("read", "extract")))
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    async with Runtime(tmp_path) as web:
        result = await web.read("https://previously-unseen.example/items/1")
    assert result["receipt"]["status"] == "observed"
    assert calls == [("direct_http", "read"), ("rendered_browser", "read")]
    assert [attempt["provider"] for attempt in result["receipt"]["attempts"]] == [
        "direct_http", "rendered_browser"]
    assert result["receipt"]["routing"]["provider_plan"]["scope"][
        "operation"] == "read"
    assert "route_recipe" not in result["receipt"]


async def test_explicit_provider_cannot_claim_an_undeclared_operation(
        tmp_path, monkeypatch):
    class ExtractOnly(Custom):
        manifest = ProviderManifest(
            "extract_only", "1", operations=("extract",))
    registry = ProviderRegistry(); plugin = ExtractOnly(); registry.register(plugin)
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    async with Runtime(tmp_path) as web:
        result = await web.read(
            "https://previously-unseen.example/item",
            WebPolicy(provider="extract_only"))
    assert result["receipt"]["failure"]["code"] == "POLICY_DENIED"
    assert plugin.calls == 0


@pytest.mark.parametrize("availability", ["invalid", RuntimeError("secret-from-available")])
async def test_default_routing_rejects_invalid_availability_without_execution_or_disclosure(
        tmp_path, monkeypatch, availability):
    class Unavailable(Custom):
        calls = 0
        def available(self, configured):
            if isinstance(availability, Exception):
                raise availability
            return availability
        async def acquire(self, request, services):
            type(self).calls += 1
            return await super().acquire(request, services)

    registry = ProviderRegistry(); registry.register(Unavailable())
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    async with Runtime(tmp_path) as web:
        result = await web.read("https://example.com/item")
    assert result["receipt"]["status"] == "failed"
    assert Unavailable.calls == 0
    assert "secret-from-available" not in json.dumps(result)


async def test_failed_catalog_seed_falls_through_to_registered_provider(tmp_path, monkeypatch):
    from frankensurf.runtime import WebFailure
    class Seed(Custom):
        manifest = ProviderManifest("camoufox", "legacy", rendering=True)
        async def acquire(self, request, services):
            self.calls += 1
            raise WebFailure("BLOCKED", "blocked")
    class Recovery(Custom):
        manifest = ProviderManifest("recovery", "1")
    registry = ProviderRegistry(); seed, backup = Seed(), Recovery()
    registry.register(backup); registry.register(seed)
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    monkeypatch.setattr("frankensurf.experimental.installed", lambda identifier: True)
    async with Runtime(tmp_path) as web:
        result = await web.read("https://www.ebay.com.au/sch/i.html?_nkw=camera")
    assert result["receipt"]["status"] == "observed"
    assert [a["provider"] for a in result["receipt"]["attempts"]] == ["camoufox", "recovery"]
    assert result["receipt"]["routing"]["catalog_seeds"][0]["basis"] == (
        "bundled provisional compatibility seed")
    assert result["receipt"]["routing"]["catalog_seeds"][0]["reliability"] is None
    assert result["receipt"]["routing"]["provider_plan"]["ordered"] == [
        "camoufox", "recovery"]
    assert result["receipt"]["attempts"][0]["status"] == "failed"
    assert result["receipt"]["attempts"][1]["status"] == "observed"


def _attempt_tree(identifier="child", cost=0.2):
    from frankensurf.providers import provider_attempt_tree
    return provider_attempt_tree([{
        "provider": identifier, "provider_version": "1",
        "status": "observed", "cost_usd": cost, "latency_ms": 1,
    }])


def _registry_services(registry):
    from frankensurf.providers import ProviderServices
    async def transport(url, policy, *args):
        return {"url": url, "content": "ok", "content_type": "text/plain",
                "http_status": 200, "headers": {}}
    services = None
    async def acquire(identifier, request):
        return await registry.acquire(identifier, request, services)
    services = ProviderServices(transport, transport, transport, acquire,
        lambda identifier, policy: registry.require_enabled(identifier, policy))
    return services


class _StagedFailureEvidenceProvider:
    manifest = ProviderManifest("staged_failure", "1")
    content = b"<html><body>bounded provider failure</body></html>"

    async def acquire(self, request, services):
        assert services.retain_failure_evidence is not None
        assert services.retain_failure_evidence(
            self.content, "text/html; rendered=1") is True
        raise WebFailure("BLOCKED", "fixture acquisition blocked", 403,
                         response_url=request.url)


def _staged_failure_registry():
    registry = ProviderRegistry()
    registry.register(_StagedFailureEvidenceProvider())
    return registry


async def test_core_stores_and_links_generic_provider_failure_evidence(
        tmp_path, monkeypatch):
    monkeypatch.setattr(
        providers, "DEFAULT_PROVIDERS", _staged_failure_registry())
    state = tmp_path / "state"

    async with Runtime(state) as web:
        result = await web.read(
            "https://example.com/blocked",
            WebPolicy(provider="staged_failure", max_bytes=4096),
        )

    assert result["receipt"]["failure"]["code"] == "BLOCKED"
    evidence = result["receipt"]["attempts"][0]["evidence"]
    assert len(evidence) == 1
    artifact = state / "evidence" / (
        hashlib.sha256(_StagedFailureEvidenceProvider.content).hexdigest()
        + ".html")
    assert evidence[0]["path"] == str(artifact.absolute())
    assert artifact.read_bytes() == _StagedFailureEvidenceProvider.content
    assert artifact.stat().st_mode & 0o777 == 0o600
    assert artifact.parent.stat().st_mode & 0o777 == 0o700
    recovery = result["receipt"]["recovery"]
    assert recovery["executed"] is False
    assert recovery["requires_explicit_invocation"] is True
    assert recovery["local_artifacts"] == evidence


async def test_failure_evidence_rejection_never_masks_exact_typed_failure(
        tmp_path, monkeypatch):
    content = b"x" * 65

    class OversizedEvidenceProvider:
        manifest = ProviderManifest("oversized_failure_evidence", "1")

        async def acquire(self, request, services):
            assert services.retain_failure_evidence(
                content, "text/html") is False
            raise WebFailure("BLOCKED", "exact fixture block", 403,
                             response_url=request.url)

    registry = ProviderRegistry()
    registry.register(OversizedEvidenceProvider())
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    async with Runtime(tmp_path / "state") as web:
        result = await web.read(
            "https://example.com/blocked",
            WebPolicy(
                provider="oversized_failure_evidence", max_bytes=64),
        )

    assert result["receipt"]["failure"]["code"] == "BLOCKED"
    assert result["receipt"]["http_status"] == 403
    assert result["receipt"]["attempts"][0]["final_url"] == (
        "https://example.com/blocked")
    assert "evidence" not in result["receipt"]["attempts"][0]


async def test_failure_evidence_descriptor_rejection_writes_no_orphan(
        tmp_path, monkeypatch):
    class TinyFailureEvidenceProvider:
        manifest = ProviderManifest("tiny_failure_evidence", "1")

        async def acquire(self, request, services):
            assert services.retain_failure_evidence(
                b"x", "text/plain") is True
            raise WebFailure("BLOCKED", "exact tiny fixture block", 403,
                             response_url=request.url)

    registry = ProviderRegistry()
    registry.register(TinyFailureEvidenceProvider())
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    state = tmp_path / "state"
    async with Runtime(state) as web:
        sentinel = state / "evidence/preexisting.txt"
        sentinel.write_bytes(b"keep")
        sentinel.chmod(0o600)
        before = {path.name for path in sentinel.parent.iterdir()}
        result = await web.read(
            "https://example.com/blocked",
            WebPolicy(provider="tiny_failure_evidence", max_bytes=64),
        )
        after = {path.name for path in sentinel.parent.iterdir()}

    assert result["receipt"]["failure"]["code"] == "BLOCKED"
    assert result["receipt"]["http_status"] == 403
    assert result["receipt"]["attempts"][0]["final_url"] == (
        "https://example.com/blocked")
    assert "evidence" not in result["receipt"]["attempts"][0]
    assert before == after == {"preexisting.txt"}
    assert sentinel.read_bytes() == b"keep"


async def test_failure_evidence_capability_returns_false_after_scope_closes():
    retained = []

    class CaptureCapabilityProvider:
        manifest = ProviderManifest("capture_evidence_capability", "1")

        async def acquire(self, request, services):
            retained.append(services.retain_failure_evidence)
            return {
                "url": request.url,
                "content": "ok",
                "content_type": "text/plain",
                "http_status": 200,
            }

    registry = ProviderRegistry()
    registry.register(CaptureCapabilityProvider())
    result = await registry.acquire(
        "capture_evidence_capability",
        ProviderRequest(
            "https://example.com/item",
            WebPolicy(provider="capture_evidence_capability")),
        _registry_services(registry),
        _evidence_preparer=lambda raw, content_type: None,
    )
    assert result["content"] == "ok"
    assert retained[0](b"late", "text/plain") is False


async def test_core_provider_retry_budget_records_every_attempt(
        tmp_path, monkeypatch):
    class RetryProvider:
        manifest = ProviderManifest("retry_provider", "1")

        def __init__(self):
            self.calls = 0

        async def acquire(self, request, services):
            self.calls += 1
            if self.calls == 1:
                assert services.retain_failure_evidence(
                    b"retry attempt evidence", "text/plain") is True
                raise WebFailure("PROVIDER_DOWN", "transient fixture")
            return {
                "url": request.url,
                "content": "retry recovered content",
                "content_type": "text/plain",
                "http_status": 200,
            }

    registry = ProviderRegistry()
    plugin = RetryProvider()
    registry.register(plugin)
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    async with Runtime(tmp_path / "state") as web:
        result = await web.read(
            "https://example.com/item",
            WebPolicy(
                provider="retry_provider",
                provider_max_attempts_per_candidate=2,
                provider_retry_delay_seconds=0,
            ),
        )

    assert result["receipt"]["status"] == "observed"
    assert plugin.calls == 0  # Runtime executes its immutable session copy.
    assert [(row["status"], row.get("failure"))
            for row in result["receipt"]["attempts"]] == [
        ("failed", "PROVIDER_DOWN"), ("observed", None)]
    assert len(result["receipt"]["attempts"][0]["evidence"]) == 1
    assert "evidence" not in result["receipt"]["attempts"][1]


async def test_provider_retry_budget_is_per_candidate_and_aggregate_is_bounded(
        tmp_path, monkeypatch):
    class AlwaysDown:
        def __init__(self, identifier):
            self.manifest = ProviderManifest(identifier, "1")

        async def acquire(self, request, services):
            raise WebFailure("PROVIDER_DOWN", "bounded retry fixture")

    registry = ProviderRegistry()
    registry.register(AlwaysDown("retry_first"))
    registry.register(AlwaysDown("retry_second"))
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    async with Runtime(tmp_path / "state") as web:
        result = await web.read(
            "https://example.com/item",
            WebPolicy(
                provider_candidates=("retry_first", "retry_second"),
                provider_max_attempts_per_candidate=3,
                provider_retry_delay_seconds=0,
            ),
        )

    assert result["receipt"]["status"] == "failed"
    assert [(row["provider"], row["failure"])
            for row in result["receipt"]["attempts"]] == [
        ("retry_first", "PROVIDER_DOWN"),
        ("retry_first", "PROVIDER_DOWN"),
        ("retry_first", "PROVIDER_DOWN"),
        ("retry_second", "PROVIDER_DOWN"),
        ("retry_second", "PROVIDER_DOWN"),
        ("retry_second", "PROVIDER_DOWN"),
    ]


async def test_core_failure_evidence_capability_obeys_policy(
        tmp_path, monkeypatch):
    class OptOutProvider:
        manifest = ProviderManifest("failure_opt_out", "1")
        async def acquire(self, request, services):
            assert services.retain_failure_evidence(
                b"<html>must not persist</html>", "text/html") is False
            raise WebFailure("BLOCKED", "fixture acquisition blocked")

    registry = ProviderRegistry()
    registry.register(OptOutProvider())
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    state = tmp_path / "state"
    async with Runtime(state) as web:
        result = await web.read(
            "https://example.com/blocked",
            WebPolicy(
                provider="failure_opt_out",
                max_bytes=4096,
                retain_public_failure_evidence=False,
            ),
        )

    assert result["receipt"]["failure"]["code"] == "BLOCKED"
    assert "evidence" not in result["receipt"]["attempts"][0]
    assert list((state / "evidence").iterdir()) == []


async def test_core_failure_evidence_rejects_parent_symlink_without_masking(
        tmp_path, monkeypatch):
    monkeypatch.setattr(
        providers, "DEFAULT_PROVIDERS", _staged_failure_registry())
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (state / "evidence").symlink_to(outside, target_is_directory=True)

    async with Runtime(state) as web:
        result = await web.read(
            "https://example.com/blocked",
            WebPolicy(provider="staged_failure", max_bytes=4096),
        )

    assert result["receipt"]["failure"]["code"] == "BLOCKED"
    assert "evidence" not in result["receipt"]["attempts"][0]
    assert list(outside.iterdir()) == []


async def test_core_failure_evidence_replaces_final_symlink_atomically(
        tmp_path, monkeypatch):
    monkeypatch.setattr(
        providers, "DEFAULT_PROVIDERS", _staged_failure_registry())
    state = tmp_path / "state"
    evidence_dir = state / "evidence"
    evidence_dir.mkdir(parents=True, mode=0o700)
    victim = tmp_path / "victim.html"
    victim.write_text("must remain unchanged")
    artifact = evidence_dir / (
        hashlib.sha256(_StagedFailureEvidenceProvider.content).hexdigest()
        + ".html")
    artifact.symlink_to(victim)

    async with Runtime(state) as web:
        result = await web.read(
            "https://example.com/blocked",
            WebPolicy(provider="staged_failure", max_bytes=4096),
        )

    assert result["receipt"]["failure"]["code"] == "BLOCKED"
    assert len(result["receipt"]["attempts"][0]["evidence"]) == 1
    assert victim.read_text() == "must remain unchanged"
    assert not artifact.is_symlink()
    assert artifact.read_bytes() == _StagedFailureEvidenceProvider.content


@pytest.mark.parametrize("envelope", ["acquisition", "failure", "screenshot"])
async def test_provider_evidence_envelopes_are_aggregate_byte_bounded(envelope):
    from frankensurf.providers import ProviderRequest
    from frankensurf.runtime import WebFailure

    references = [
        {"sha256": f"{index:064x}", "path": "/" + "x" * 180, "bytes": 1}
        for index in range(50)]

    class EvidenceProvider:
        manifest = ProviderManifest("evidence_provider", "1")
        async def acquire(self, request, services):
            if envelope == "failure":
                error = WebFailure("BLOCKED", "blocked")
                error._public_failure_evidence = references
                raise error
            result = {"url": request.url, "content": "ok",
                "content_type": "text/plain", "http_status": 200,
                "headers": {}}
            if envelope == "acquisition":
                result["acquisition_evidence"] = references
            else:
                result["screenshot"] = {
                    "sha256": "a" * 64, "path": "/" + "x" * 4095,
                    "bytes": 1}
            return result

    registry = ProviderRegistry(); registry.register(EvidenceProvider())
    policy = WebPolicy(provider="evidence_provider", max_bytes=4096)
    if envelope == "failure":
        with pytest.raises(WebFailure) as caught:
            await registry.acquire("evidence_provider",
                ProviderRequest("https://example.com/item", policy),
                _registry_services(registry))
        assert caught.value.code == "BLOCKED"
        assert caught.value._public_failure_evidence == []
    else:
        with pytest.raises(WebFailure) as caught:
            await registry.acquire("evidence_provider",
                ProviderRequest("https://example.com/item", policy),
                _registry_services(registry))
        assert caught.value.code == "PROVIDER_DOWN"


@pytest.mark.parametrize("malformed", ["secret", "cycle", "nodes", "path"])
async def test_provider_failure_attempt_trees_are_policy_bounded_and_closed(malformed):
    from frankensurf.providers import ProviderRequest
    from frankensurf.runtime import WebFailure

    class Broken:
        manifest = ProviderManifest("broken", "1")
        async def acquire(self, request, services):
            tree = _attempt_tree()
            if malformed == "secret":
                tree["attempts"][0]["secret"] = "must-not-escape"
            elif malformed == "cycle":
                tree["attempts"][0].update(status="failed", failure="UNKNOWN")
                tree["attempts"][0]["children"] = tree
            elif malformed == "nodes":
                tree["attempts"] = [
                    {"provider": "child", "provider_version": "1",
                     "status": "observed", "cost_usd": 0,
                     "latency_ms": index}
                    for index in range(3)]
            else:
                tree["attempts"][0]["evidence"] = [{
                    "sha256": "a" * 64, "path": "/" + "x" * 2000,
                    "bytes": 1}]
            error = WebFailure("BLOCKED", "blocked", cost_usd=0.25)
            error.provider_attempts = tree
            raise error

    registry = ProviderRegistry(); registry.register(Broken())
    policy = WebPolicy(provider="broken", provider_composition_max_attempts=2,
                       max_bytes=1024)
    with pytest.raises(WebFailure) as caught:
        await registry.acquire("broken",
            ProviderRequest("https://example.com/item", policy),
            _registry_services(registry))
    assert caught.value.code == "PROVIDER_DOWN"
    assert caught.value.cost_usd == 0.25
    assert not hasattr(caught.value, "provider_attempts")


async def test_provider_failure_attempt_tree_is_copied_before_exposure():
    from frankensurf.providers import ProviderRequest
    from frankensurf.runtime import WebFailure
    source = _attempt_tree()

    class Failed:
        manifest = ProviderManifest("failed", "1")
        async def acquire(self, request, services):
            error = WebFailure("BLOCKED", "blocked", cost_usd=0.2)
            error.provider_attempts = source
            raise error

    registry = ProviderRegistry(); registry.register(Failed())
    with pytest.raises(WebFailure) as caught:
        await registry.acquire("failed",
            ProviderRequest("https://example.com/item",
                WebPolicy(provider="failed")),
            _registry_services(registry))
    source["attempts"][0]["provider"] = "mutated"
    assert caught.value.provider_attempts["attempts"][0]["provider"] == "child"


async def test_direct_and_mutual_provider_recursion_stop_before_respawn():
    from frankensurf.providers import ProviderRequest
    from frankensurf.runtime import WebFailure

    class Recursive:
        def __init__(self, identifier, child):
            self.manifest = ProviderManifest(identifier, "1")
            self.child, self.calls = child, 0
        async def acquire(self, request, services):
            self.calls += 1
            return await services.acquire_provider(self.child,
                ProviderRequest(request.url, request.policy))

    direct = Recursive("direct", "direct")
    registry = ProviderRegistry(); registry.register(direct)
    with pytest.raises(WebFailure) as caught:
        await registry.acquire("direct",
            ProviderRequest("https://example.com/", WebPolicy(provider="direct")),
            _registry_services(registry))
    assert caught.value.code == "POLICY_DENIED"
    assert direct.calls == 1
    assert [row["provider"] for row in
            caught.value.provider_attempts["attempts"]] == ["direct"]

    first, second = Recursive("first_recursive", "second_recursive"), Recursive(
        "second_recursive", "first_recursive")
    registry = ProviderRegistry(); registry.register(first); registry.register(second)
    with pytest.raises(WebFailure) as caught:
        await registry.acquire("first_recursive",
            ProviderRequest("https://example.com/",
                WebPolicy(provider="first_recursive")),
            _registry_services(registry))
    outer = caught.value.provider_attempts["attempts"]
    assert first.calls == second.calls == 1
    assert outer[0]["provider"] == "second_recursive"
    assert outer[0]["children"]["attempts"][0]["provider"] == "first_recursive"


async def test_provider_execution_context_isolated_between_root_tasks():
    import asyncio
    from frankensurf.providers import ProviderRequest

    class Concurrent:
        manifest = ProviderManifest("concurrent", "1")
        calls = 0
        async def acquire(self, request, services):
            type(self).calls += 1
            await asyncio.sleep(0)
            return {"url": request.url, "content": "ok",
                    "content_type": "text/plain", "http_status": 200,
                    "headers": {}}

    registry = ProviderRegistry(); registry.register(Concurrent())
    request = ProviderRequest("https://example.com/",
        WebPolicy(provider="concurrent"))
    results = await asyncio.gather(
        registry.acquire("concurrent", request, _registry_services(registry)),
        registry.acquire("concurrent", request, _registry_services(registry)))
    assert len(results) == 2 and Concurrent.calls == 2
    assert all("provider_attempts" not in result for result in results)


async def test_outer_timeout_retains_started_paid_child_as_unknown_cost():
    import asyncio
    from frankensurf.providers import ProviderRequest
    from frankensurf.runtime import WebFailure

    class Paid:
        manifest = ProviderManifest("paid_child", "1", paid=True)
        async def acquire(self, request, services):
            await asyncio.Event().wait()

    class Outer:
        manifest = ProviderManifest("outer", "1")
        async def acquire(self, request, services):
            return await services.acquire_provider("paid_child",
                ProviderRequest(request.url, request.policy))

    registry = ProviderRegistry()
    registry.register(Outer()); registry.register(Paid())
    policy = WebPolicy(provider="outer", allow_paid_fallbacks=True,
        timeout_seconds=0.01, provider_deadline_grace_seconds=0,
        provider_cleanup_grace_seconds=0.01)
    with pytest.raises(WebFailure) as caught:
        await registry.acquire("outer",
            ProviderRequest("https://example.com/", policy),
            _registry_services(registry))
    assert caught.value.code == "TIMEOUT"
    assert caught.value.cost_usd is None
    row = caught.value.provider_attempts["attempts"][0]
    assert row["provider"] == "paid_child"
    assert row["status"] == "failed" and row["failure"] == "TIMEOUT"
    assert row["cost_usd"] is None


@pytest.mark.parametrize("reported,expected_code,expected_cost", [
    (0.3, None, 0.3), (0.1, "PROVIDER_DOWN", 0.2)])
async def test_composite_parent_total_is_billed_once_and_cannot_undercount(
        reported, expected_code, expected_cost):
    from frankensurf.providers import ProviderRequest
    from frankensurf.runtime import WebFailure

    class Child:
        manifest = ProviderManifest("priced_child", "1")
        async def acquire(self, request, services):
            return {"url": request.url, "content": "ok",
                "content_type": "text/plain", "http_status": 200,
                "headers": {}, "cost_usd": 0.2}

    class Outer:
        manifest = ProviderManifest("priced_outer", "1")
        async def acquire(self, request, services):
            child = await services.acquire_provider("priced_child",
                ProviderRequest(request.url, request.policy))
            return {**child, "cost_usd": reported}

    registry = ProviderRegistry()
    registry.register(Outer()); registry.register(Child())
    request = ProviderRequest("https://example.com/",
        WebPolicy(provider="priced_outer"))
    if expected_code:
        with pytest.raises(WebFailure) as caught:
            await registry.acquire("priced_outer", request,
                                   _registry_services(registry))
        assert caught.value.code == expected_code
        assert caught.value.cost_usd == expected_cost
        tree = caught.value.provider_attempts
    else:
        result = await registry.acquire("priced_outer", request,
                                        _registry_services(registry))
        assert result["cost_usd"] == expected_cost
        tree = result["provider_attempts"]
    assert tree["attempts"][0]["cost_usd"] == 0.2


async def test_invalid_composite_result_envelope_retains_tree_and_total():
    from frankensurf.providers import ProviderRequest
    from frankensurf.runtime import WebFailure

    class Child:
        manifest = ProviderManifest("envelope_child", "1")
        async def acquire(self, request, services):
            return {"url": request.url, "content": "ok",
                "content_type": "text/plain", "http_status": 200,
                "headers": {}, "cost_usd": 0.2}

    class Outer:
        manifest = ProviderManifest("envelope_outer", "1")
        async def acquire(self, request, services):
            child = await services.acquire_provider("envelope_child",
                ProviderRequest(request.url, request.policy))
            return {**child, "content": object(), "cost_usd": 0.3}

    registry = ProviderRegistry()
    registry.register(Outer()); registry.register(Child())
    with pytest.raises(WebFailure) as caught:
        await registry.acquire("envelope_outer",
            ProviderRequest("https://example.com/",
                WebPolicy(provider="envelope_outer")),
            _registry_services(registry))
    assert caught.value.code == "PROVIDER_DOWN"
    assert caught.value.cost_usd == 0.3
    assert caught.value.provider_attempts["attempts"][0]["cost_usd"] == 0.2


async def test_runtime_post_provider_adapter_failure_retains_child_tree_and_cost(
        tmp_path, monkeypatch):
    from frankensurf import adapters
    from frankensurf.adapters import AdapterManifest, AdapterRegistry
    from frankensurf.runtime import WebFailure

    class Composite:
        manifest = ProviderManifest("runtime_composite", "1")
        async def acquire(self, request, services):
            return {"url": request.url, "content": "bad",
                "content_type": "text/plain", "http_status": 200,
                "headers": {}, "cost_usd": 0.4,
                "provider_attempts": _attempt_tree(cost=0.4)}

    class FailingAdapter:
        manifest = AdapterManifest("failing_adapter", "1")
        def extract(self, request):
            raise WebFailure("SCHEMA_CHANGED", "changed")

    provider_registry = ProviderRegistry(); provider_registry.register(Composite())
    adapter_registry = AdapterRegistry(); adapter_registry.register(FailingAdapter())
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", provider_registry)
    monkeypatch.setattr(adapters, "DEFAULT_ADAPTERS", adapter_registry)
    async with Runtime(tmp_path) as web:
        result = await web.extract("https://example.com/item", "failing_adapter",
            policy=WebPolicy(provider="runtime_composite"))
    attempt = result["receipt"]["attempts"][0]
    assert result["receipt"]["failure"]["code"] == "SCHEMA_CHANGED"
    assert result["receipt"]["cost_usd"] == 0.4
    assert attempt["cost_usd"] == 0.4
    assert attempt["children"]["attempts"][0]["provider"] == "child"


async def test_runtime_sanitizes_adapter_supplied_attempt_tree(tmp_path, monkeypatch):
    from frankensurf import adapters
    from frankensurf.adapters import AdapterManifest, AdapterRegistry
    from frankensurf.runtime import WebFailure

    class Plain:
        manifest = ProviderManifest("plain_runtime", "1")
        async def acquire(self, request, services):
            return {"url": request.url, "content": "bad",
                "content_type": "text/plain", "http_status": 200,
                "headers": {}}

    class MaliciousAdapter:
        manifest = AdapterManifest("malicious_adapter", "1")
        def extract(self, request):
            error = WebFailure("SCHEMA_CHANGED", "changed")
            tree = _attempt_tree()
            tree["attempts"][0]["secret"] = "must-not-escape"
            error.provider_attempts = tree
            raise error

    provider_registry = ProviderRegistry(); provider_registry.register(Plain())
    adapter_registry = AdapterRegistry(); adapter_registry.register(MaliciousAdapter())
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", provider_registry)
    monkeypatch.setattr(adapters, "DEFAULT_ADAPTERS", adapter_registry)
    async with Runtime(tmp_path) as web:
        result = await web.extract("https://example.com/item",
            "malicious_adapter", policy=WebPolicy(provider="plain_runtime"))
    assert result["receipt"]["failure"]["code"] == "SCHEMA_CHANGED"
    assert "children" not in result["receipt"]["attempts"][0]


@pytest.mark.parametrize("field", [
    "provider_composition_max_depth", "provider_composition_max_attempts"])
def test_provider_composition_policy_limits_are_positive(field):
    with pytest.raises(ValueError):
        WebPolicy(**{field: 0})


async def test_provider_depth_guard_precedes_spawn_and_inherits_through_tasks():
    import asyncio
    from frankensurf.providers import ProviderRequest
    from frankensurf.runtime import WebFailure

    class Chain:
        def __init__(self, identifier, child=None, spawn=False):
            self.manifest = ProviderManifest(identifier, "1")
            self.child, self.spawn, self.calls = child, spawn, 0
        async def acquire(self, request, services):
            self.calls += 1
            if self.child is None:
                return {"url": request.url, "content": "ok",
                    "content_type": "text/plain", "http_status": 200,
                    "headers": {}}
            call = services.acquire_provider(self.child,
                ProviderRequest(request.url, request.policy))
            return await (asyncio.create_task(call) if self.spawn else call)

    first = Chain("depth_first", "depth_second")
    second = Chain("depth_second", "depth_third", spawn=True)
    third = Chain("depth_third")
    registry = ProviderRegistry()
    for plugin in (first, second, third):
        registry.register(plugin)
    policy = WebPolicy(provider="depth_first",
                       provider_composition_max_depth=2)
    with pytest.raises(WebFailure) as caught:
        await registry.acquire("depth_first",
            ProviderRequest("https://example.com/", policy),
            _registry_services(registry))
    assert first.calls == second.calls == 1 and third.calls == 0
    nested = caught.value.provider_attempts["attempts"][0]["children"]
    assert nested["attempts"][0]["provider"] == "depth_third"

    result = await registry.acquire("depth_third",
        ProviderRequest("https://example.com/",
            WebPolicy(provider="depth_third")),
        _registry_services(registry))
    assert result["content"] == "ok" and third.calls == 1


@pytest.mark.parametrize("mode", ["success", "failure"])
async def test_paid_composite_without_parent_cost_stays_unknown(mode):
    from frankensurf.providers import ProviderRequest
    from frankensurf.runtime import WebFailure

    class FreeChild:
        manifest = ProviderManifest("free_lower_bound", "1")
        async def acquire(self, request, services):
            return {"url": request.url, "content": "ok",
                "content_type": "text/plain", "http_status": 200,
                "headers": {}, "cost_usd": 0}

    class PaidOuter:
        manifest = ProviderManifest("paid_composite", "1", paid=True)
        async def acquire(self, request, services):
            child = await services.acquire_provider("free_lower_bound",
                ProviderRequest(request.url, request.policy))
            if mode == "failure":
                raise WebFailure("BLOCKED", "failed after child")
            child.pop("cost_usd", None)
            return child

    registry = ProviderRegistry()
    registry.register(PaidOuter()); registry.register(FreeChild())
    request = ProviderRequest("https://example.com/",
        WebPolicy(provider="paid_composite", allow_paid_fallbacks=True))
    if mode == "failure":
        with pytest.raises(WebFailure) as caught:
            await registry.acquire("paid_composite", request,
                                   _registry_services(registry))
        assert caught.value.code == "BLOCKED"
        assert caught.value.cost_usd is None
        tree = caught.value.provider_attempts
    else:
        result = await registry.acquire("paid_composite", request,
                                        _registry_services(registry))
        assert result["cost_usd"] is None
        tree = result["provider_attempts"]
    assert tree["attempts"][0]["provider"] == "free_lower_bound"
    assert tree["attempts"][0]["cost_usd"] == 0


async def test_cancelled_pending_paid_composite_keeps_own_total_unknown():
    import asyncio
    from frankensurf.providers import ProviderRequest
    from frankensurf.runtime import WebFailure

    class FreeChild:
        manifest = ProviderManifest("cancel_free_child", "1")
        async def acquire(self, request, services):
            return {"url": request.url, "content": "ok",
                "content_type": "text/plain", "http_status": 200,
                "headers": {}, "cost_usd": 0}

    class PaidComposite:
        manifest = ProviderManifest("cancel_paid_composite", "1", paid=True)
        async def acquire(self, request, services):
            await services.acquire_provider("cancel_free_child",
                ProviderRequest(request.url, request.policy))
            await asyncio.Event().wait()

    class FreeOuter:
        manifest = ProviderManifest("cancel_free_outer", "1")
        async def acquire(self, request, services):
            return await services.acquire_provider("cancel_paid_composite",
                ProviderRequest(request.url, request.policy))

    registry = ProviderRegistry()
    for plugin in (FreeOuter(), PaidComposite(), FreeChild()):
        registry.register(plugin)
    policy = WebPolicy(provider="cancel_free_outer", allow_paid_fallbacks=True,
        timeout_seconds=0.01, provider_deadline_grace_seconds=0,
        provider_cleanup_grace_seconds=0.01)
    with pytest.raises(WebFailure) as caught:
        await registry.acquire("cancel_free_outer",
            ProviderRequest("https://example.com/", policy),
            _registry_services(registry))
    assert caught.value.code == "TIMEOUT"
    assert caught.value.cost_usd is None
    paid = caught.value.provider_attempts["attempts"][0]
    assert paid["provider"] == "cancel_paid_composite"
    assert paid["failure"] == "TIMEOUT" and paid["cost_usd"] is None
    lower_bound = paid["children"]["attempts"][0]
    assert lower_bound["provider"] == "cancel_free_child"
    assert lower_bound["cost_usd"] == 0


async def test_delayed_fire_and_forget_child_cannot_enter_closed_scope():
    import asyncio
    from frankensurf.providers import ProviderRequest
    from frankensurf.runtime import WebFailure
    release = asyncio.Event()
    spawned = []

    class Paid:
        manifest = ProviderManifest("late_paid", "1", paid=True)
        calls = 0
        async def acquire(self, request, services):
            type(self).calls += 1
            return {"url": request.url, "content": "late",
                "content_type": "text/plain", "http_status": 200,
                "headers": {}, "cost_usd": 1}

    class Outer:
        manifest = ProviderManifest("late_outer", "1")
        async def acquire(self, request, services):
            async def delayed():
                await release.wait()
                return await services.acquire_provider("late_paid",
                    ProviderRequest(request.url, request.policy))
            spawned.append(asyncio.create_task(delayed()))
            return {"url": request.url, "content": "parent",
                "content_type": "text/plain", "http_status": 200,
                "headers": {}}

    registry = ProviderRegistry(); registry.register(Outer()); registry.register(Paid())
    policy = WebPolicy(provider="late_outer", allow_paid_fallbacks=True)
    result = await registry.acquire("late_outer",
        ProviderRequest("https://example.com/", policy),
        _registry_services(registry))
    release.set()
    with pytest.raises(WebFailure) as caught:
        await spawned[0]
    assert caught.value.code == "POLICY_DENIED"
    assert Paid.calls == 0
    assert "provider_attempts" not in result


async def test_started_fire_and_forget_child_is_finalized_in_parent_tree():
    import asyncio
    from frankensurf.providers import ProviderRequest
    started = asyncio.Event()

    class Paid:
        manifest = ProviderManifest("started_paid", "1", paid=True)
        calls = 0
        async def acquire(self, request, services):
            type(self).calls += 1
            started.set()
            await asyncio.Event().wait()

    class Outer:
        manifest = ProviderManifest("started_outer", "1")
        async def acquire(self, request, services):
            asyncio.create_task(services.acquire_provider("started_paid",
                ProviderRequest(request.url, request.policy)))
            await started.wait()
            return {"url": request.url, "content": "parent",
                "content_type": "text/plain", "http_status": 200,
                "headers": {}}

    registry = ProviderRegistry(); registry.register(Outer()); registry.register(Paid())
    policy = WebPolicy(provider="started_outer", allow_paid_fallbacks=True,
                       provider_cleanup_grace_seconds=0.1)
    result = await registry.acquire("started_outer",
        ProviderRequest("https://example.com/", policy),
        _registry_services(registry))
    assert Paid.calls == 1
    assert result["cost_usd"] is None
    row = result["provider_attempts"]["attempts"][0]
    assert row["provider"] == "started_paid"
    assert row["status"] == "failed" and row["failure"] == "TIMEOUT"
    assert row["cost_usd"] is None


async def test_adapter_cannot_replace_acquired_provider_tree_or_cost(
        tmp_path, monkeypatch):
    from frankensurf import adapters
    from frankensurf.adapters import AdapterManifest, AdapterRegistry
    from frankensurf.runtime import WebFailure

    class PaidProvider:
        manifest = ProviderManifest("authoritative_provider", "1", paid=True)
        async def acquire(self, request, services):
            return {"url": request.url, "content": "bad",
                "content_type": "text/plain", "http_status": 200,
                "headers": {}, "cost_usd": 0.4,
                "provider_attempts": _attempt_tree(
                    identifier="real_child", cost=0.4)}

    class ForgingAdapter:
        manifest = AdapterManifest("forging_adapter", "1")
        def extract(self, request):
            error = WebFailure("SCHEMA_CHANGED", "changed", 418,
                cost_usd=0, response_url="https://fake.test/",
                failure_stage="continuation_click")
            error.provider_attempts = _attempt_tree(
                identifier="fake_child", cost=0)
            error._public_failure_evidence = [{
                "sha256": "f" * 64, "path": "/fake", "bytes": 1}]
            raise error

    provider_registry = ProviderRegistry()
    provider_registry.register(PaidProvider())
    adapter_registry = AdapterRegistry()
    adapter_registry.register(ForgingAdapter())
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", provider_registry)
    monkeypatch.setattr(adapters, "DEFAULT_ADAPTERS", adapter_registry)
    async with Runtime(tmp_path) as web:
        result = await web.extract("https://example.com/item",
            "forging_adapter", policy=WebPolicy(
                provider="authoritative_provider",
                allow_paid_fallbacks=True))
    attempt = result["receipt"]["attempts"][0]
    assert result["receipt"]["failure"]["code"] == "SCHEMA_CHANGED"
    assert result["receipt"]["cost_usd"] == attempt["cost_usd"] == 0.4
    assert attempt["children"]["attempts"][0]["provider"] == "real_child"
    assert result["url"] == "https://example.com/item"
    assert result["receipt"]["final_url"] == "https://example.com/item"
    assert result["receipt"]["http_status"] == 200
    assert "failure_stage" not in result["receipt"]
    assert "failure_stage" not in attempt
    assert "/fake" not in json.dumps(result)


async def test_nested_provider_policy_is_derived_from_parent_authority():
    from dataclasses import replace
    from frankensurf.providers import ProviderRequest

    observed = {}

    class Probe:
        manifest = ProviderManifest("policy_probe", "1")
        async def acquire(self, request, services):
            observed.update(
                allow_paid_fallbacks=request.policy.allow_paid_fallbacks,
                allow_local_browser=request.policy.allow_local_browser,
                max_cost_usd=request.policy.max_cost_usd,
                max_bytes=request.policy.max_bytes,
                retry_attempts=(
                    request.policy.provider_max_attempts_per_candidate),
                retry_delay=request.policy.provider_retry_delay_seconds,
                retry_failures=request.policy.provider_retry_failures)
            return {"url": request.url, "content": "ok",
                "content_type": "text/plain", "http_status": 200,
                "headers": {}}

    class Outer:
        manifest = ProviderManifest("policy_outer", "1")
        async def acquire(self, request, services):
            escalated = replace(request.policy,
                provider="policy_probe", provider_candidates=None,
                allow_paid_fallbacks=True, allow_local_browser=True,
                max_cost_usd=None, max_bytes=10_000_000,
                provider_max_attempts_per_candidate=9,
                provider_retry_delay_seconds=5,
                provider_retry_failures=("TIMEOUT", "BLOCKED"))
            return await services.acquire_provider("policy_probe",
                ProviderRequest(request.url, escalated))

    registry = ProviderRegistry()
    registry.register(Outer()); registry.register(Probe())
    policy = WebPolicy(provider="policy_outer",
        allow_paid_fallbacks=False, allow_local_browser=False,
        max_cost_usd=0.25, max_bytes=4096,
        provider_max_attempts_per_candidate=2,
        provider_retry_delay_seconds=0.2,
        provider_retry_failures=("PROVIDER_DOWN", "TIMEOUT"))
    result = await registry.acquire("policy_outer",
        ProviderRequest("https://example.com/", policy),
        _registry_services(registry))
    assert result["content"] == "ok"
    assert observed == {"allow_paid_fallbacks": False,
        "allow_local_browser": False, "max_cost_usd": 0.25,
        "max_bytes": 4096, "retry_attempts": 2,
        "retry_delay": 0.2, "retry_failures": ("TIMEOUT",)}


async def test_nested_provider_cannot_escalate_paid_permission():
    from dataclasses import replace
    from frankensurf.providers import ProviderRequest
    from frankensurf.runtime import WebFailure

    class Paid:
        manifest = ProviderManifest("denied_paid_child", "1", paid=True)
        calls = 0
        async def acquire(self, request, services):
            type(self).calls += 1
            return {"url": request.url, "content": "paid",
                "content_type": "text/plain", "http_status": 200,
                "headers": {}, "cost_usd": 1}

    class Outer:
        manifest = ProviderManifest("denied_paid_outer", "1")
        async def acquire(self, request, services):
            escalated = replace(request.policy,
                provider="denied_paid_child", provider_candidates=None,
                allow_paid_fallbacks=True, max_cost_usd=None)
            return await services.acquire_provider("denied_paid_child",
                ProviderRequest(request.url, escalated))

    Paid.calls = 0
    registry = ProviderRegistry()
    registry.register(Outer()); registry.register(Paid())
    with pytest.raises(WebFailure) as caught:
        await registry.acquire("denied_paid_outer",
            ProviderRequest("https://example.com/",
                WebPolicy(provider="denied_paid_outer",
                          allow_paid_fallbacks=False,
                          max_cost_usd=0.1)),
            _registry_services(registry))
    assert caught.value.code == "POLICY_DENIED"
    assert Paid.calls == 0


async def test_provider_self_cancellation_is_sanitized_failure():
    import asyncio
    from frankensurf.providers import ProviderRequest
    from frankensurf.runtime import WebFailure

    class SelfCancelling:
        manifest = ProviderManifest("self_cancelling", "1")
        async def acquire(self, request, services):
            raise asyncio.CancelledError()

    registry = ProviderRegistry(); registry.register(SelfCancelling())
    with pytest.raises(WebFailure) as caught:
        await registry.acquire("self_cancelling",
            ProviderRequest("https://example.com/",
                WebPolicy(provider="self_cancelling")),
            _registry_services(registry))
    assert caught.value.code == "PROVIDER_DOWN"


async def test_invalid_optional_provider_metadata_is_typed_failure():
    from frankensurf.providers import ProviderRequest
    from frankensurf.runtime import WebFailure

    class InvalidMetadata:
        manifest = ProviderManifest("invalid_metadata", "1")
        async def acquire(self, request, services):
            return {"url": request.url, "content": "ok",
                "content_type": "text/plain", "http_status": 200,
                "headers": {}, "navigation_data": "boom"}

    registry = ProviderRegistry(); registry.register(InvalidMetadata())
    with pytest.raises(WebFailure) as caught:
        await registry.acquire("invalid_metadata",
            ProviderRequest("https://example.com/",
                WebPolicy(provider="invalid_metadata")),
            _registry_services(registry))
    assert caught.value.code == "PROVIDER_DOWN"


async def test_combined_nested_attempt_tree_overflow_is_typed_failure():
    from frankensurf.providers import ProviderRequest
    from frankensurf.runtime import WebFailure

    class Child:
        def __init__(self, identifier):
            self.manifest = ProviderManifest(identifier, "1")
        async def acquire(self, request, services):
            return {"url": request.url, "content": "ok",
                "content_type": "text/plain", "http_status": 200,
                "headers": {}}

    class Outer:
        manifest = ProviderManifest("tree_outer", "1")
        async def acquire(self, request, services):
            await services.acquire_provider("tree_child_one",
                ProviderRequest(request.url, request.policy))
            return await services.acquire_provider("tree_child_two",
                ProviderRequest(request.url, request.policy))

    registry = ProviderRegistry()
    registry.register(Outer())
    registry.register(Child("tree_child_one"))
    registry.register(Child("tree_child_two"))
    with pytest.raises(WebFailure) as caught:
        await registry.acquire("tree_outer",
            ProviderRequest("https://example.com/",
                WebPolicy(provider="tree_outer", max_bytes=400)),
            _registry_services(registry))
    assert caught.value.code == "PROVIDER_DOWN"
    assert not hasattr(caught.value, "provider_attempts")


async def test_unknown_nested_paid_cost_blocks_later_sequential_child():
    from frankensurf.providers import ProviderRequest
    from frankensurf.runtime import WebFailure

    class PaidUnknown:
        manifest = ProviderManifest("unknown_paid_child", "1",
                                    paid=True, cost_bounded=True)
        async def acquire(self, request, services):
            return {"url": request.url, "content": "paid",
                "content_type": "text/plain", "http_status": 200,
                "headers": {}}

    class Later:
        manifest = ProviderManifest("later_free_child", "1")
        calls = 0
        async def acquire(self, request, services):
            type(self).calls += 1
            return {"url": request.url, "content": "late",
                "content_type": "text/plain", "http_status": 200,
                "headers": {}}

    class Outer:
        manifest = ProviderManifest("unknown_cost_outer", "1")
        async def acquire(self, request, services):
            await services.acquire_provider("unknown_paid_child",
                ProviderRequest(request.url, request.policy))
            return await services.acquire_provider("later_free_child",
                ProviderRequest(request.url, request.policy))

    Later.calls = 0
    registry = ProviderRegistry()
    for plugin in (Outer(), PaidUnknown(), Later()):
        registry.register(plugin)
    with pytest.raises(WebFailure) as caught:
        await registry.acquire("unknown_cost_outer",
            ProviderRequest("https://example.com/", WebPolicy(
                provider="unknown_cost_outer", allow_paid_fallbacks=True,
                max_cost_usd=1)),
            _registry_services(registry))
    assert caught.value.code == "BUDGET_EXHAUSTED"
    assert caught.value.cost_usd is None
    assert Later.calls == 0
    row = caught.value.provider_attempts["attempts"][0]
    assert row["provider"] == "unknown_paid_child"
    assert row["status"] == "observed" and row["cost_usd"] is None


async def test_cancelled_scope_cleanup_still_closes_and_consumes_late_task():
    import asyncio
    from frankensurf.providers import _ExecutionScope, _close_execution_scope

    started = asyncio.Event()
    release = asyncio.Event()

    async def stubborn():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    child = asyncio.create_task(stubborn())
    await started.wait()
    policy = WebPolicy()
    scope = _ExecutionScope(("outer",), [], [0],
        policy.provider_composition_max_depth,
        policy.provider_composition_max_attempts,
        policy.max_bytes, policy, child_tasks={child})
    closer = asyncio.create_task(_close_execution_scope(scope, 10))
    while scope.state != "closing":
        await asyncio.sleep(0)
    closer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closer
    assert scope.state == "closed"
    release.set()
    await child


async def test_clean_context_immediate_child_uses_captured_parent_scope():
    import asyncio
    import contextvars
    from dataclasses import replace
    from frankensurf.providers import ProviderRequest

    seen = {}

    class Child:
        manifest = ProviderManifest("clean_context_child", "1")
        async def acquire(self, request, services):
            seen.update(allow_paid=request.policy.allow_paid_fallbacks,
                        max_cost=request.policy.max_cost_usd)
            return {"url": request.url, "content": "child",
                "content_type": "text/plain", "http_status": 200,
                "headers": {}, "cost_usd": 0}

    class Outer:
        manifest = ProviderManifest("clean_context_outer", "1")
        async def acquire(self, request, services):
            escalated = replace(request.policy,
                provider="clean_context_child", provider_candidates=None,
                allow_paid_fallbacks=True, max_cost_usd=None)
            call = services.acquire_provider("clean_context_child",
                ProviderRequest(request.url, escalated))
            return await asyncio.create_task(
                call, context=contextvars.Context())

    registry = ProviderRegistry()
    registry.register(Outer()); registry.register(Child())
    policy = WebPolicy(provider="clean_context_outer",
        allow_paid_fallbacks=False, max_cost_usd=0.5)
    result = await registry.acquire("clean_context_outer",
        ProviderRequest("https://example.com/", policy),
        _registry_services(registry))
    assert seen == {"allow_paid": False, "max_cost": 0.5}
    assert result["provider_attempts"]["attempts"][0]["provider"] == (
        "clean_context_child")


async def test_clean_context_delayed_paid_child_cannot_escape_closed_scope():
    import asyncio
    import contextvars
    from dataclasses import replace
    from frankensurf.providers import ProviderRequest
    from frankensurf.runtime import WebFailure

    release = asyncio.Event()
    spawned = []

    class Paid:
        manifest = ProviderManifest("clean_late_paid", "1",
                                    paid=True, cost_bounded=True)
        calls = 0
        async def acquire(self, request, services):
            type(self).calls += 1
            return {"url": request.url, "content": "paid",
                "content_type": "text/plain", "http_status": 200,
                "headers": {}, "cost_usd": 1}

    class Outer:
        manifest = ProviderManifest("clean_late_outer", "1")
        async def acquire(self, request, services):
            escalated = replace(request.policy,
                provider="clean_late_paid", provider_candidates=None,
                allow_paid_fallbacks=True, max_cost_usd=None)
            async def delayed():
                await release.wait()
                return await services.acquire_provider("clean_late_paid",
                    ProviderRequest(request.url, escalated))
            spawned.append(asyncio.create_task(
                delayed(), context=contextvars.Context()))
            return {"url": request.url, "content": "parent",
                "content_type": "text/plain", "http_status": 200,
                "headers": {}}

    Paid.calls = 0
    registry = ProviderRegistry()
    registry.register(Outer()); registry.register(Paid())
    policy = WebPolicy(provider="clean_late_outer",
        allow_paid_fallbacks=False, max_cost_usd=0)
    result = await registry.acquire("clean_late_outer",
        ProviderRequest("https://example.com/", policy),
        _registry_services(registry))
    release.set()
    with pytest.raises(WebFailure) as caught:
        await spawned[0]
    assert caught.value.code == "POLICY_DENIED"
    assert Paid.calls == 0
    assert "provider_attempts" not in result


async def test_direct_provider_registry_enforces_read_action_class():
    registry = ProviderRegistry()
    plugin = Custom()
    registry.register(plugin)
    request = ProviderRequest(
        "https://example.com/item",
        WebPolicy(
            provider="custom",
            action_classes=("READ_AUTHENTICATED",)))

    with pytest.raises(WebFailure) as raised:
        await registry.acquire("custom", request, None)

    assert raised.value.code == "POLICY_DENIED"
    assert plugin.calls == 0


async def test_diagnosis_registry_enforces_authority_and_deadline():
    class SlowDiagnosis:
        manifest = ProviderManifest("slow_diagnosis", "1", diagnosis=True)
        calls = 0

        async def acquire(self, request, services):
            raise AssertionError("diagnosis provider acquired a normal read")

        async def diagnose(self, request, services):
            type(self).calls += 1
            await asyncio.sleep(1)
            raise AssertionError("diagnosis deadline was not enforced")

    registry = ProviderRegistry()
    plugin = SlowDiagnosis()
    registry.register(plugin)
    denied = RepairProviderRequest(
        "https://example.com/item",
        WebPolicy(action_classes=("READ_AUTHENTICATED",)),
        "1" * 32, {}, {})

    with pytest.raises(WebFailure) as raised:
        await registry.diagnose("slow_diagnosis", denied, None)
    assert raised.value.code == "POLICY_DENIED"
    assert plugin.calls == 0

    timed = RepairProviderRequest(
        "https://example.com/item",
        WebPolicy(
            action_classes=("READ_PUBLIC",),
            timeout_seconds=0.01,
            provider_deadline_grace_seconds=0),
        "2" * 32, {}, {})
    with pytest.raises(WebFailure) as raised:
        await registry.diagnose("slow_diagnosis", timed, None)
    assert raised.value.code == "TIMEOUT"
    assert plugin.calls == 1


async def test_diagnosis_services_are_exact_and_detached_work_is_cancelled():
    transport_calls = []

    async def transport(url, policy, provider=None):
        transport_calls.append((url, policy, provider))
        return {"unexpected": True}

    services = ProviderServices(transport, transport, transport)

    class ScopeEscape:
        manifest = ProviderManifest(
            "scope_escape", "1", diagnosis=True)

        async def acquire(self, request, services):
            raise AssertionError("diagnosis provider acquired a normal read")

        async def diagnose(self, request, services):
            return await services.http(
                "https://other.example/item", request.policy)

    registry = ProviderRegistry()
    registry.register(ScopeEscape())
    request = RepairProviderRequest(
        "https://example.com/item",
        WebPolicy(action_classes=("READ_PUBLIC",)),
        "3" * 32, {}, {})

    with pytest.raises(WebFailure) as raised:
        await registry.diagnose("scope_escape", request, services)
    assert raised.value.code == "POLICY_DENIED"
    assert transport_calls == []

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocking_transport(url, policy):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    class DetachedDiagnosis:
        manifest = ProviderManifest(
            "detached_diagnosis", "1", diagnosis=True)

        async def acquire(self, request, services):
            raise AssertionError("diagnosis provider acquired a normal read")

        async def diagnose(self, request, services):
            asyncio.create_task(
                services.http(request.url, request.policy))
            await started.wait()
            return {"status": "diagnosed"}

    detached_registry = ProviderRegistry()
    detached_registry.register(DetachedDiagnosis())
    detached_services = ProviderServices(
        blocking_transport, transport, transport)
    detached_request = RepairProviderRequest(
        "https://example.com/item",
        WebPolicy(
            action_classes=("READ_PUBLIC",),
            provider_cleanup_grace_seconds=0.1),
        "4" * 32, {}, {})

    result = await detached_registry.diagnose(
        "detached_diagnosis", detached_request, detached_services)
    assert result == {"status": "diagnosed"}
    assert cancelled.is_set()
