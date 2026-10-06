import base64
import json

import pytest

from frankensurf import (
    BrowserAction,
    RAW_BROWSER_CONTROL_CONTRACT,
    RAW_CONTROL_REQUIRED_ACTION_CLASSES,
    Runtime,
    WebIntent,
    WebPolicy,
)
from frankensurf import browser_use_config, providers
from frankensurf.browser_use_action_provider import BrowserUseActionProvider
from frankensurf.identity import IdentityRegistry
from frankensurf.providers import (
    ProviderActionRequest,
    ProviderManifest,
    ProviderRegistry,
)
from frankensurf.runtime import WebFailure


PROVIDER = "raw_core_test_do"
ALTERNATE_PROVIDER = "raw_core_test_do_alternate"
ORIGIN = "http://127.0.0.1:8129"
URL = ORIGIN + "/actions/new"
TOOLS = ("click", "assert_text", "fill")
PROVIDER_CALLS = []


def raw_intent(key="raw-core-one"):
    return WebIntent(
        URL,
        RAW_BROWSER_CONTROL_CONTRACT,
        "WRITE_EXTERNAL",
        key,
        "Exercise explicitly authorized raw controls",
        (
            BrowserAction("fill", "#private-title", "Private value"),
            BrowserAction("click", "#private-save"),
            BrowserAction("assert_text", "#private-status", "Saved privately"),
        ),
    )


def raw_policy(**changes):
    values = {
        "identity": "owner",
        "action_classes": RAW_CONTROL_REQUIRED_ACTION_CLASSES,
        "provider": PROVIDER,
        "browser_do_allowed_contracts": (RAW_BROWSER_CONTROL_CONTRACT,),
        "browser_do_allowed_origins": (ORIGIN,),
        # Deliberately differs from intent order: equality is order-insensitive.
        "browser_do_allowed_tools": TOOLS,
    }
    values.update(changes)
    return WebPolicy(**values)


def completed_packet():
    snapshot = {
        "url": URL,
        "html": "<main>private action fixture</main>",
        "screenshot": base64.b64encode(b"synthetic-png").decode(),
    }
    return {
        "status": "completed",
        "effect_started": True,
        "steps": [
            {"index": 0, "tool": "fill", "status": "completed"},
            {"index": 1, "tool": "click", "status": "completed"},
            {"index": 2, "tool": "assert_text", "status": "completed"},
        ],
        "sdk_version": "raw-core-test-v1",
        "before": snapshot,
        "after": snapshot,
        "url": URL,
    }


def failed_post_effect_packet():
    packet = completed_packet()
    packet.update(status="failed", failure="SCHEMA_CHANGED",
                  steps=packet["steps"][:2])
    return packet


def validation_packet():
    packet = completed_packet()
    packet["sdk_version"] = browser_use_config.SDK_VERSION
    return packet


class RawActionProvider:
    manifest = ProviderManifest(
        PROVIDER,
        "raw-core-test-v1",
        rendering=True,
        requires_local_browser=True,
        authentication=True,
        navigation=True,
        operations=("do",),
        action_classes=RAW_CONTROL_REQUIRED_ACTION_CLASSES,
        action_contracts=(RAW_BROWSER_CONTROL_CONTRACT,),
    )
    packet_json = "{}"

    def available(self, configured):
        return True

    async def acquire(self, request, services):
        raise AssertionError("action provider reached read acquisition")

    async def perform(self, request, services):
        return await services.authenticated_action(request, self.execute)

    async def execute(self, request, grant):
        PROVIDER_CALLS.append(request.intent.idempotency_key)
        return json.loads(type(self).packet_json)


class AlternateIdRawActionProvider(RawActionProvider):
    manifest = ProviderManifest(
        ALTERNATE_PROVIDER,
        "raw-core-test-v1",
        rendering=True,
        requires_local_browser=True,
        authentication=True,
        navigation=True,
        operations=("do",),
        action_classes=RAW_CONTROL_REQUIRED_ACTION_CLASSES,
        action_contracts=(RAW_BROWSER_CONTROL_CONTRACT,),
    )


class AlternateVersionRawActionProvider(RawActionProvider):
    manifest = ProviderManifest(
        PROVIDER,
        "raw-core-test-v2",
        rendering=True,
        requires_local_browser=True,
        authentication=True,
        navigation=True,
        operations=("do",),
        action_classes=RAW_CONTROL_REQUIRED_ACTION_CLASSES,
        action_contracts=(RAW_BROWSER_CONTROL_CONTRACT,),
    )


class AlternateBindingRawActionProvider(RawActionProvider):
    # Same public identity and version, different pinned implementation.
    def available(self, configured):
        return True


def install(monkeypatch, plugin=None):
    PROVIDER_CALLS.clear()
    RawActionProvider.packet_json = json.dumps(completed_packet())
    registry = ProviderRegistry()
    registry.register(plugin or RawActionProvider())
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    return registry


def identity_registry(tmp_path, allowed=RAW_CONTROL_REQUIRED_ACTION_CLASSES):
    path = tmp_path / "authority" / "identities.json"
    profile = tmp_path / "profile"
    profile.mkdir()
    registry = IdentityRegistry(path)
    registry.enroll_executor(
        "desktop",
        endpoint="http://127.0.0.1:9331",
        user_data_dir=str(profile),
    )
    registry.enroll_identity(
        "owner",
        executor_id="desktop",
        domains=["127.0.0.1"],
        allowed_actions=("READ_AUTHENTICATED", *allowed),
        auth_check={"url": ORIGIN + "/account",
                    "authenticated_selector": "#owner"},
    )
    return path


async def prepared_runtime(tmp_path, monkeypatch, *, allowed=None):
    install(monkeypatch)
    identity_path = identity_registry(
        tmp_path,
        RAW_CONTROL_REQUIRED_ACTION_CLASSES if allowed is None else allowed,
    )
    web = Runtime(tmp_path / "state", identity_registry=identity_path)
    return stub_identity_checks(web)


def stub_identity_checks(web):

    async def context(resolved, policy):
        return object()

    async def health(context, resolved, policy, **kwargs):
        return "verified"

    web._identity_context = context
    web._identity_health = health
    return web


@pytest.mark.parametrize("missing", RAW_CONTROL_REQUIRED_ACTION_CLASSES)
def test_registry_rejects_manifest_missing_each_raw_contract_grant(missing):
    class IncompleteProvider(RawActionProvider):
        manifest = ProviderManifest(
            PROVIDER,
            "raw-core-test-v1",
            rendering=True,
            requires_local_browser=True,
            authentication=True,
            operations=("do",),
            action_classes=tuple(value for value in
                RAW_CONTROL_REQUIRED_ACTION_CLASSES if value != missing),
            action_contracts=(RAW_BROWSER_CONTROL_CONTRACT,),
        )

    with pytest.raises(ValueError, match="Invalid provider plugin contract"):
        ProviderRegistry().register(IncompleteProvider())


@pytest.mark.parametrize("missing", RAW_CONTROL_REQUIRED_ACTION_CLASSES)
async def test_registry_execution_requires_each_raw_policy_grant(missing):
    registry = ProviderRegistry()
    registry.register(RawActionProvider())
    policy = raw_policy(action_classes=tuple(value for value in
        RAW_CONTROL_REQUIRED_ACTION_CLASSES if value != missing))
    request = ProviderActionRequest(
        raw_intent("registry-" + missing.replace("/", "-")), policy)

    with pytest.raises(WebFailure) as raised:
        await registry.perform(PROVIDER, request, None)

    assert raised.value.code == "POLICY_DENIED"


@pytest.mark.parametrize("missing", RAW_CONTROL_REQUIRED_ACTION_CLASSES)
async def test_core_requires_each_raw_policy_grant(tmp_path, monkeypatch,
                                                   missing):
    web = await prepared_runtime(tmp_path, monkeypatch)
    policy = raw_policy(action_classes=tuple(value for value in
        RAW_CONTROL_REQUIRED_ACTION_CLASSES if value != missing))

    async with web:
        result = await web.do(
            raw_intent("policy-" + missing.replace("/", "-")), policy)

    assert result["receipt"]["failure"]["code"] == "POLICY_DENIED"
    assert PROVIDER_CALLS == []
    assert not (tmp_path / "state" / "actions" / "journal.jsonl").exists()


@pytest.mark.parametrize("missing", RAW_CONTROL_REQUIRED_ACTION_CLASSES)
async def test_core_requires_each_raw_identity_grant(tmp_path, monkeypatch,
                                                     missing):
    allowed = tuple(value for value in RAW_CONTROL_REQUIRED_ACTION_CLASSES
                    if value != missing)
    web = await prepared_runtime(tmp_path, monkeypatch, allowed=allowed)

    async with web:
        result = await web.do(
            raw_intent("identity-" + missing.replace("/", "-")),
            raw_policy())

    assert result["receipt"]["failure"]["code"] == "IDENTITY_ACTION_DENIED"
    assert PROVIDER_CALLS == []
    assert not (tmp_path / "state" / "actions" / "journal.jsonl").exists()


async def test_raw_requires_explicit_provider_origins_and_exact_tools(
        tmp_path, monkeypatch):
    registry = install(monkeypatch)
    web = Runtime(tmp_path / "state",
        identity_registry=identity_registry(tmp_path))
    intent = raw_intent("explicit-boundaries")

    async with web:
        implicit_provider = await web.do(intent, raw_policy(
            provider=None, provider_candidates=(PROVIDER,)))
        implicit_origins = await web.do(intent, raw_policy(
            browser_do_allowed_origins=None))
        broad_tools = await web.do(intent, raw_policy(
            browser_do_allowed_tools=(
                "fill", "click", "wait_for", "assert_text", "assert_value")))

    assert implicit_provider["receipt"]["failure"]["code"] == "POLICY_DENIED"
    assert implicit_origins["receipt"]["failure"]["code"] == "POLICY_DENIED"
    assert broad_tools["receipt"]["failure"]["code"] == "POLICY_DENIED"
    assert registry.action_candidates(
        raw_policy(provider=None), "WRITE_EXTERNAL",
        RAW_BROWSER_CONTROL_CONTRACT) == []
    assert PROVIDER_CALLS == []


async def test_raw_receipt_and_journal_bind_full_ceiling_without_secrets(
        tmp_path, monkeypatch):
    web = await prepared_runtime(tmp_path, monkeypatch)
    intent = raw_intent("raw-idempotent")
    policy = raw_policy()
    resolved = web.identities.resolve(
        "owner", URL, action="WRITE_EXTERNAL")

    async with web:
        first = await web.do(intent, policy)
        reused = await web.do(intent, policy)

    assert first["receipt"]["status"] == "completed"
    assert first["receipt"]["required_action_classes"] == list(
        RAW_CONTROL_REQUIRED_ACTION_CLASSES)
    assert first["receipt"]["semantic_result"] == "unknown"
    assert first["receipt"]["verification"] == {
        "status": "steps_observed",
        "completed_steps": 3,
        "steps": completed_packet()["steps"],
    }
    assert first["outcome"] == {
        "state": "completed",
        "certainty": "steps_observed",
        "reversible": None,
        "reconciliation_required": False,
    }
    assert reused["receipt"]["idempotent_reuse"] is True
    assert PROVIDER_CALLS == ["raw-idempotent"]

    journal_path = tmp_path / "state" / "actions" / "journal.jsonl"
    journal_text = journal_path.read_text()
    records = [json.loads(line) for line in journal_text.splitlines()]
    expected_fingerprint = intent.fingerprint({
        "identity_scope": resolved.cache_scope,
        "action_class": "WRITE_EXTERNAL",
        "required_action_classes": RAW_CONTROL_REQUIRED_ACTION_CLASSES,
        "action_contract": RAW_BROWSER_CONTROL_CONTRACT,
        "provider_id": PROVIDER,
        "provider_version": "raw-core-test-v1",
        "provider_binding_id": web.providers.binding_id(PROVIDER),
        "origins": (ORIGIN,),
        "tools": TOOLS,
    })
    assert {row["request_fingerprint"] for row in records} == {
        expected_fingerprint}
    public = json.dumps(first, sort_keys=True)
    for private in ("#private-title", "#private-save", "#private-status",
                    "Private value", "Saved privately"):
        assert private not in public
        assert private not in journal_text


async def test_uncertain_failure_retains_reconciliation_evidence_and_step_prefix(
        tmp_path, monkeypatch):
    install(monkeypatch)
    RawActionProvider.packet_json = json.dumps(failed_post_effect_packet())
    web = stub_identity_checks(Runtime(
        tmp_path / "state", identity_registry=identity_registry(tmp_path)))
    intent = raw_intent("raw-uncertain-evidence")

    async with web:
        result = await web.do(intent, raw_policy())

    assert result["receipt"]["status"] == "failed"
    assert result["receipt"]["failure"]["code"] == (
        "EXECUTION_OUTCOME_UNKNOWN")
    assert result["receipt"]["underlying_failure"] == "SCHEMA_CHANGED"
    assert result["receipt"]["verification"] == {
        "status": "partial_steps_observed",
        "completed_steps": 2,
        "steps": failed_post_effect_packet()["steps"],
    }
    assert [(item["phase"], item["kind"])
            for item in result["receipt"]["evidence"]] == [
        ("before", "html"),
        ("before", "screenshot"),
        ("after", "html"),
        ("after", "screenshot"),
    ]
    assert all((tmp_path / "state" / item["path"]).is_file()
               for item in result["receipt"]["evidence"])
    assert result["outcome"] == {
        "state": "unknown",
        "certainty": "unknown",
        "reconciliation_required": True,
    }
    assert PROVIDER_CALLS == ["raw-uncertain-evidence"]
    assert '"state":"uncertain"' in (
        tmp_path / "state" / "actions" / "journal.jsonl").read_text()


@pytest.mark.parametrize(("packet_factory", "key"), [
    (failed_post_effect_packet, "failed-evidence-persistence"),
    (completed_packet, "completed-evidence-persistence"),
])
async def test_post_effect_evidence_persistence_failure_is_typed_and_fenced(
        tmp_path, monkeypatch, packet_factory, key):
    install(monkeypatch)
    RawActionProvider.packet_json = json.dumps(packet_factory())
    web = stub_identity_checks(Runtime(
        tmp_path / "state", identity_registry=identity_registry(tmp_path)))
    retention_calls = []

    def fail_retention(packet):
        retention_calls.append(packet["status"])
        raise OSError("synthetic private evidence failure")

    monkeypatch.setattr(web, "_retain_action_evidence", fail_retention)
    intent = raw_intent(key)
    async with web:
        first = await web.do(intent, raw_policy())
        fenced = await web.do(intent, raw_policy())

    assert first["receipt"]["failure"]["code"] == (
        "EXECUTION_OUTCOME_UNKNOWN")
    assert first["receipt"]["underlying_failure"] == "PROVIDER_DOWN"
    assert first["receipt"]["evidence"] == []
    assert first["outcome"] == {
        "state": "unknown",
        "certainty": "unknown",
        "reconciliation_required": True,
    }
    assert fenced["receipt"]["failure"]["code"] == (
        "EXECUTION_OUTCOME_UNKNOWN")
    assert PROVIDER_CALLS == [key]
    assert retention_calls == [packet_factory()["status"]]
    assert not any((tmp_path / "state" / "evidence").iterdir())
    assert '"state":"uncertain"' in (
        tmp_path / "state" / "actions" / "journal.jsonl").read_text()


@pytest.mark.parametrize(("replacement_type", "changed_fields"), [
    (AlternateIdRawActionProvider, {"provider_id", "provider_binding_id"}),
    (AlternateVersionRawActionProvider,
     {"provider_version", "provider_binding_id"}),
    (AlternateBindingRawActionProvider, {"provider_binding_id"}),
])
async def test_completed_outcome_is_not_reused_across_provider_authority(
        tmp_path, monkeypatch, replacement_type, changed_fields):
    identity_path = identity_registry(tmp_path)
    state_path = tmp_path / "state"
    intent = raw_intent("provider-authority-change")

    install(monkeypatch)
    first_web = stub_identity_checks(Runtime(
        state_path, identity_registry=identity_path))
    first_authority = {
        "provider_id": PROVIDER,
        "provider_version": "raw-core-test-v1",
        "provider_binding_id": first_web.providers.binding_id(PROVIDER),
    }
    async with first_web:
        first = await first_web.do(intent, raw_policy())
    assert first["receipt"]["status"] == "completed"
    assert PROVIDER_CALLS == ["provider-authority-change"]

    replacement = replacement_type()
    install(monkeypatch, replacement)
    second_web = stub_identity_checks(Runtime(
        state_path, identity_registry=identity_path))
    replacement_id = replacement.manifest.id
    second_authority = {
        "provider_id": replacement_id,
        "provider_version": replacement.manifest.version,
        "provider_binding_id": second_web.providers.binding_id(replacement_id),
    }
    for field in changed_fields:
        assert first_authority[field] != second_authority[field]

    async with second_web:
        second = await second_web.do(
            intent, raw_policy(provider=replacement_id))

    assert second["receipt"]["status"] == "failed"
    assert second["receipt"]["failure"]["code"] == "POLICY_DENIED"
    assert second["receipt"]["idempotent_reuse"] is False
    assert PROVIDER_CALLS == []


@pytest.mark.parametrize("bad_step", [
    {"index": 1, "tool": "fill", "status": "completed"},
    {"index": 0, "tool": "click", "status": "completed"},
])
def test_worker_packet_steps_must_be_an_exact_positional_prefix(bad_step):
    intent = raw_intent("tampered-prefix")
    policy = raw_policy()
    packet = {
        "status": "failed",
        "failure": "TIMEOUT",
        "effect_started": True,
        "steps": [bad_step],
        "sdk_version": browser_use_config.SDK_VERSION,
    }

    with pytest.raises(WebFailure) as raised:
        BrowserUseActionProvider._validate_packet(
            packet, intent, policy, (ORIGIN,), True)

    assert raised.value.code == "PROVIDER_DOWN"


def test_worker_packet_rejects_mutating_prefix_without_effect_marker():
    packet = {
        "status": "failed",
        "failure": "TIMEOUT",
        "effect_started": False,
        "steps": [
            {"index": 0, "tool": "fill", "status": "completed"},
        ],
        "sdk_version": browser_use_config.SDK_VERSION,
    }

    with pytest.raises(WebFailure) as raised:
        BrowserUseActionProvider._validate_packet(
            packet, raw_intent("missing-effect-marker"), raw_policy(),
            (ORIGIN,), False)

    assert raised.value.code == "PROVIDER_DOWN"


def test_completed_worker_packet_binds_final_url_to_after_snapshot():
    packet = validation_packet()
    packet["url"] = ORIGIN + "/different-final"

    with pytest.raises(WebFailure) as raised:
        BrowserUseActionProvider._validate_packet(
            packet, raw_intent("mismatched-final-url"), raw_policy(),
            (ORIGIN,), True)

    assert raised.value.code == "PROVIDER_DOWN"


def test_completed_worker_packet_allows_scoped_redirect_url_chain():
    packet = validation_packet()
    packet["before"]["url"] = ORIGIN + "/redirected-entry"
    packet["after"]["url"] = ORIGIN + "/completed"
    packet["url"] = packet["after"]["url"]

    BrowserUseActionProvider._validate_packet(
        packet, raw_intent("scoped-url-chain"), raw_policy(),
        (ORIGIN,), True)
