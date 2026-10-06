import base64
import json

import pytest

from frankensurf import BrowserAction, Runtime, WebIntent, WebPolicy
from frankensurf.identity import IdentityRegistry
from frankensurf import providers
from frankensurf import cli, mcp_server
from frankensurf.providers import (
    ProviderActionRequest, ProviderManifest, ProviderRegistry)
from frankensurf.runtime import WebFailure
from frankensurf.web_do import (
    LOCAL_REVERSIBLE_DRAFT_CONTRACT, RAW_BROWSER_CONTROL_CONTRACT,
    RAW_CONTROL_REQUIRED_ACTION_CLASSES, require_action_contract)


URL = "http://127.0.0.1:8123/drafts/new"


class FixtureActionProvider:
    manifest = ProviderManifest("browser_use_local_cdp_do", "fixture-1",
        rendering=True, requires_local_browser=True, authentication=True,
        navigation=True, operations=("do",),
        action_classes=("WRITE_REVERSIBLE",),
        action_contracts=(LOCAL_REVERSIBLE_DRAFT_CONTRACT,))
    calls = 0
    packet_json = "{}"

    def available(self, configured):
        return True

    async def acquire(self, request, services):
        raise AssertionError("do provider reached acquisition")

    async def perform(self, request, services):
        return await services.authenticated_action(request, self.execute)

    async def execute(self, request, grant):
        type(self).calls += 1
        return json.loads(type(self).packet_json)


def install(monkeypatch, packet):
    FixtureActionProvider.calls = 0
    FixtureActionProvider.packet_json = json.dumps(packet)
    registry = ProviderRegistry()
    registry.register(FixtureActionProvider())
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)


def identity(tmp_path, actions=("READ_AUTHENTICATED", "WRITE_REVERSIBLE")):
    path = tmp_path / "authority" / "identities.json"
    profile = tmp_path / "profile"
    profile.mkdir()
    registry = IdentityRegistry(path)
    registry.enroll_executor("desktop", endpoint="http://127.0.0.1:9331",
        user_data_dir=str(profile))
    registry.enroll_identity("owner", executor_id="desktop",
        domains=["127.0.0.1"], allowed_actions=actions,
        auth_check={"url": "http://127.0.0.1:8123/account",
                    "authenticated_selector": "#owner"})
    return path


def intent(key="draft-one"):
    return WebIntent(URL, LOCAL_REVERSIBLE_DRAFT_CONTRACT,
        "WRITE_REVERSIBLE", key,
        "Save the listing as a reversible draft", (
            BrowserAction("fill", "#title", "Fixture bicycle"),
            BrowserAction("click", "#save"),
            BrowserAction("assert_text", "#status", "Draft saved"),
        ))


def packet(*, status="completed", failure=None, effect=True):
    snapshot = {"url": URL, "html": "<main>local fixture</main>",
                "screenshot": base64.b64encode(b"fixture-png").decode()}
    value = {"status": status, "effect_started": effect,
        "steps": ([{"index": index, "tool": tool, "status": "completed"}
                  for index, tool in enumerate(("fill", "click", "assert_text"))]
                  if status == "completed" else []),
        "sdk_version": "fixture", "before": snapshot, "after": snapshot,
        "url": URL}
    if failure:
        value["failure"] = failure
    return value


async def prepared_runtime(tmp_path, monkeypatch, response):
    install(monkeypatch, response)
    web = Runtime(tmp_path / "state", identity_registry=identity(tmp_path))

    async def context(resolved, policy):
        return object()

    async def health(context, resolved, policy, **kwargs):
        return "verified"

    web._identity_context = context
    web._identity_health = health
    return web


def test_typed_intent_requires_explicit_non_read_authority_and_mutation():
    with pytest.raises(ValueError):
        WebIntent(URL, LOCAL_REVERSIBLE_DRAFT_CONTRACT,
            "READ_AUTHENTICATED", "read-one", "Read", (
            BrowserAction("assert_text", "h1", "Item"),))
    with pytest.raises(ValueError):
        WebIntent.from_dict({"url": URL,
            "contract": LOCAL_REVERSIBLE_DRAFT_CONTRACT,
            "idempotency_key": "missing-class", "description": "Missing",
            "actions": []})
    with pytest.raises(ValueError):
        BrowserAction.from_dict({"tool": "evaluate", "selector": "body"})
    with pytest.raises(ValueError, match="loopback fixtures"):
        WebIntent("https://market.example/drafts/new",
            LOCAL_REVERSIBLE_DRAFT_CONTRACT, "WRITE_REVERSIBLE",
            "external", "External draft", (
                BrowserAction("fill", "#title", "blocked"),
                BrowserAction("click", "#save"),))


def test_raw_control_contract_has_full_ceiling_and_accepts_external_plan():
    contract = require_action_contract(RAW_BROWSER_CONTROL_CONTRACT)
    assert contract.required_action_classes == (
        "WRITE_REVERSIBLE",
        "WRITE_EXTERNAL",
        "PURCHASE/FINANCIAL",
        "ACCOUNT_SECURITY",
    )
    assert contract.required_action_classes == (
        RAW_CONTROL_REQUIRED_ACTION_CLASSES)
    assert contract.action_class == "WRITE_EXTERNAL"
    assert contract.explicit_provider_required is True
    assert contract.requires_explicit_origins is True
    assert contract.semantic_result == "unknown"

    external = WebIntent(
        "https://market.example/listings/new",
        RAW_BROWSER_CONTROL_CONTRACT,
        "WRITE_EXTERNAL",
        "raw-external-one",
        "Prepare an explicitly authorized browser form",
        (BrowserAction("wait_for", "form", state="visible"),
         BrowserAction("fill", "#account-specific-title", "Private title"),
         BrowserAction("click", "#save-draft"),
         BrowserAction("assert_text", "#status", "Saved")))

    contract.validate_origin("https://market.example")
    contract.validate_origin("http://127.0.0.1:8123")
    plan = external.public_plan()
    assert plan == {
        "contract": RAW_BROWSER_CONTROL_CONTRACT,
        "description": "Prepare an explicitly authorized browser form",
        "required_action_classes": list(
            RAW_CONTROL_REQUIRED_ACTION_CLASSES),
        "semantic_result": "unknown",
        "action_count": 4,
        "actions": [
            {"index": 0, "tool": "wait_for", "mutating": False},
            {"index": 1, "tool": "fill", "mutating": True},
            {"index": 2, "tool": "click", "mutating": True},
            {"index": 3, "tool": "assert_text", "mutating": False},
        ],
    }
    encoded = json.dumps(plan, sort_keys=True)
    assert "#account-specific-title" not in encoded
    assert "#save-draft" not in encoded
    assert "Private title" not in encoded
    assert "Saved" not in encoded

    with pytest.raises(PermissionError, match="HTTPS outside loopback"):
        contract.validate_origin("http://market.example")
    with pytest.raises(ValueError, match="HTTPS outside loopback"):
        WebIntent("http://market.example/listings/new",
            RAW_BROWSER_CONTROL_CONTRACT, "WRITE_EXTERNAL",
            "raw-insecure-external", "Reject insecure remote control", (
                BrowserAction("click", "#save-draft"),))


def test_fixture_contract_validation_and_public_plan_remain_bounded():
    fixture = intent("fixture-contract-compatibility")
    assert require_action_contract(
        LOCAL_REVERSIBLE_DRAFT_CONTRACT).required_action_classes == (
            "WRITE_REVERSIBLE",)
    plan = fixture.public_plan()
    assert plan["required_action_classes"] == ["WRITE_REVERSIBLE"]
    assert plan["semantic_result"] == "contract_bound"
    assert [action["tool"] for action in plan["actions"]] == [
        "fill", "click", "assert_text"]
    assert all("selector" not in action for action in plan["actions"])
    assert "Fixture bicycle" not in json.dumps(plan, sort_keys=True)

    with pytest.raises(ValueError, match="fills followed by one save"):
        WebIntent(URL, LOCAL_REVERSIBLE_DRAFT_CONTRACT,
            "WRITE_REVERSIBLE", "fixture-without-save",
            "Fixture contract remains strict", (
                BrowserAction("fill", "#title", "Unsaved"),))

    # The raw contract deliberately supports a bounded fill-only plan; it does
    # not inherit the synthetic fixture's marked-save shape.
    fill_only = WebIntent("https://market.example/listings/new",
        RAW_BROWSER_CONTROL_CONTRACT, "WRITE_EXTERNAL", "raw-fill-only",
        "Prepare a field for operator review", (
            BrowserAction("fill", "#title", "Prepared"),))
    assert fill_only.actions[0].tool == "fill"


async def test_core_one_call_retains_evidence_and_reuses_completed_outcome(
        tmp_path, monkeypatch):
    response = packet()
    # The fixture provider version is intentionally independent from Browser Use.
    response["sdk_version"] = "fixture"
    web = await prepared_runtime(tmp_path, monkeypatch, response)
    policy = WebPolicy(identity="owner",
        action_classes=("WRITE_REVERSIBLE",),
        provider="browser_use_local_cdp_do")
    async with web:
        first = await web.do(intent(), policy)
        second = await web.do(intent(), policy)

    assert first["receipt"]["status"] == "completed"
    assert first["outcome"] == {"state": "completed", "certainty": "dom_assertions_passed",
        "reversible": True, "reconciliation_required": False}
    assert first["receipt"]["action_class"] == "WRITE_REVERSIBLE"
    assert first["receipt"]["identity"] == "owner"
    assert first["receipt"]["provider"] == "browser_use_local_cdp_do"
    assert first["receipt"]["verification"] == {
        "status": "dom_assertions_passed", "completed_steps": 3}
    assert {item["phase"] for item in first["receipt"]["evidence"]} == {
        "before", "after"}
    assert {item["kind"] for item in first["receipt"]["evidence"]} == {
        "html", "screenshot"}
    assert second["receipt"]["idempotent_reuse"] is True
    assert len((tmp_path / "state" / "actions" /
        "journal.jsonl").read_text().splitlines()) == 2
    assert "Fixture bicycle" not in str(first)
    assert "Fixture bicycle" not in (tmp_path / "state" / "actions" /
        "journal.jsonl").read_text()


async def test_read_policy_never_grants_write(tmp_path, monkeypatch):
    web = await prepared_runtime(tmp_path, monkeypatch, packet())
    async with web:
        result = await web.do(intent(), WebPolicy(identity="owner",
            provider="browser_use_local_cdp_do"))
    assert result["receipt"]["failure"]["code"] == "POLICY_DENIED"
    assert not (tmp_path / "state" / "actions" / "journal.jsonl").exists()


async def test_identity_must_separately_grant_write(tmp_path, monkeypatch):
    install(monkeypatch, packet())
    path = identity(tmp_path, actions=("READ_AUTHENTICATED",))
    async with Runtime(tmp_path / "state", identity_registry=path) as web:
        result = await web.do(intent(), WebPolicy(identity="owner",
            action_classes=("WRITE_REVERSIBLE",),
            provider="browser_use_local_cdp_do"))
    assert result["receipt"]["failure"]["code"] == "IDENTITY_ACTION_DENIED"
    assert not (tmp_path / "state" / "actions" / "journal.jsonl").exists()


async def test_uncertain_write_is_never_automatically_replayed(
        tmp_path, monkeypatch):
    response = packet(status="failed", failure="TIMEOUT", effect=True)
    web = await prepared_runtime(tmp_path, monkeypatch, response)
    policy = WebPolicy(identity="owner",
        action_classes=("WRITE_REVERSIBLE",),
        provider="browser_use_local_cdp_do")
    async with web:
        first = await web.do(intent("uncertain-one"), policy)
        second = await web.do(intent("uncertain-one"), policy)
    assert first["receipt"]["failure"]["code"] == "EXECUTION_OUTCOME_UNKNOWN"
    assert first["receipt"]["underlying_failure"] == "TIMEOUT"
    assert second["receipt"]["failure"]["code"] == "EXECUTION_OUTCOME_UNKNOWN"
    assert len((tmp_path / "state" / "actions" /
        "journal.jsonl").read_text().splitlines()) == 2


async def test_failure_before_effect_can_retry_same_idempotency_key(
        tmp_path, monkeypatch):
    response = packet(status="failed", failure="SCHEMA_CHANGED", effect=False)
    web = await prepared_runtime(tmp_path, monkeypatch, response)
    policy = WebPolicy(identity="owner",
        action_classes=("WRITE_REVERSIBLE",),
        provider="browser_use_local_cdp_do")
    async with web:
        first = await web.do(intent("safe-retry"), policy)
        second = await web.do(intent("safe-retry"), policy)
    assert first["receipt"]["failure"]["code"] == "SCHEMA_CHANGED"
    assert second["receipt"]["failure"]["code"] == "SCHEMA_CHANGED"
    assert len((tmp_path / "state" / "actions" /
        "journal.jsonl").read_text().splitlines()) == 4


async def test_policy_scopes_tools_and_origins_before_provider(
        tmp_path, monkeypatch):
    web = await prepared_runtime(tmp_path, monkeypatch, packet())
    async with web:
        denied_tool = await web.do(intent("tool-denied"), WebPolicy(
            identity="owner", action_classes=("WRITE_REVERSIBLE",),
            provider="browser_use_local_cdp_do",
            browser_do_allowed_tools=("click",)))
        denied_origin = await web.do(intent("origin-denied"), WebPolicy(
            identity="owner", action_classes=("WRITE_REVERSIBLE",),
            provider="browser_use_local_cdp_do",
            browser_do_allowed_origins=("http://localhost:8123",)))
    assert denied_tool["receipt"]["failure"]["code"] == "POLICY_DENIED"
    assert denied_origin["receipt"]["failure"]["code"] == "IDENTITY_DOMAIN_DENIED"
    assert not (tmp_path / "state" / "actions" / "journal.jsonl").exists()


async def test_cli_and_mcp_use_the_same_core_do_contract(
        tmp_path, monkeypatch, capsys):
    intent_file = tmp_path / "intent.json"
    intent_file.write_text(json.dumps(intent("cli-one").private_dict()))
    cli_root = tmp_path / "cli"
    cli_root.mkdir()
    cli_web = await prepared_runtime(cli_root, monkeypatch, packet())
    monkeypatch.setattr(cli, "Runtime", lambda **kwargs: cli_web)
    args = cli.parse_args(["do", "--intent-file", str(intent_file),
        "--state", str(tmp_path / "cli-state"), "--identity", "owner",
        "--identity-registry", str(tmp_path / "unused.json"),
        "--provider", "browser_use_local_cdp_do",
        "--grant-action", "WRITE_REVERSIBLE",
        "--action-origin", "http://127.0.0.1:8123"])
    from_cli = await cli.run(args)
    assert from_cli["receipt"]["status"] == "completed"
    assert "Fixture bicycle" not in capsys.readouterr().out

    mcp_root = tmp_path / "mcp"
    mcp_root.mkdir()
    mcp_web = await prepared_runtime(mcp_root, monkeypatch, packet())
    monkeypatch.setattr(mcp_server, "runtime", lambda: mcp_web)
    from_mcp = await mcp_server.do(intent("mcp-one").private_dict(),
        "owner", ["WRITE_REVERSIBLE"],
        provider="browser_use_local_cdp_do",
        action_policy={"browser_do_allowed_origins": [
            "http://127.0.0.1:8123"]})
    assert from_mcp["receipt"]["status"] == "completed"
    assert from_mcp["receipt"]["action_contract"] == (
        LOCAL_REVERSIBLE_DRAFT_CONTRACT)


async def test_direct_action_registry_cannot_bypass_write_authority():
    FixtureActionProvider.calls = 0
    registry = ProviderRegistry()
    registry.register(FixtureActionProvider())
    request = ProviderActionRequest(
        intent(),
        WebPolicy(
            identity="owner",
            provider="browser_use_local_cdp_do",
            action_classes=("READ_AUTHENTICATED",)))

    with pytest.raises(WebFailure) as raised:
        await registry.perform(
            "browser_use_local_cdp_do", request, None)

    assert raised.value.code == "POLICY_DENIED"
    assert FixtureActionProvider.calls == 0


async def test_action_journal_rejects_impossible_completed_history(
        tmp_path, monkeypatch):
    web = await prepared_runtime(tmp_path, monkeypatch, packet())
    journal_dir = tmp_path / "state" / "actions"
    journal_dir.mkdir(parents=True, mode=0o700)
    journal_dir.chmod(0o700)
    journal = journal_dir / "journal.jsonl"
    journal.write_text(json.dumps({
        "schema": "frankensurf.action-journal/v1",
        "recorded_at": "2026-01-01T00:00:00+00:00",
        "key": "1" * 64,
        "request_fingerprint": "2" * 64,
        "trace_id": "3" * 32,
        "state": "completed",
        "result": {"receipt": {"status": "completed"}},
    }) + "\n")
    journal.chmod(0o600)
    policy = WebPolicy(
        identity="owner",
        action_classes=("WRITE_REVERSIBLE",),
        provider="browser_use_local_cdp_do")

    async with web:
        result = await web.do(intent("journal-corrupt"), policy)

    assert result["receipt"]["failure"]["code"] == (
        "EXECUTION_OUTCOME_UNKNOWN")
    assert FixtureActionProvider.calls == 0
