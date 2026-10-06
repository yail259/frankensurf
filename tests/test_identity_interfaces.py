import json

import pytest

from frankensurf import cli

try:
    from frankensurf import mcp_server
except ModuleNotFoundError as exc:
    if not exc.name.startswith("mcp"):
        raise
    mcp_server = None


class RecordingRuntime:
    calls = []
    constructions = []
    registry = None

    def __init__(self, **kwargs):
        self.constructions.append(kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def read(self, url, policy=None, *, policy_overrides=None):
        from frankensurf.routes import request_policy
        policy, _ = request_policy(policy, policy_overrides)
        self.calls.append(("read", url, policy))
        return {"content": "private raw page", "receipt": {"identity": policy.identity}}

    async def extract(self, url, adapter, policy=None, *, policy_overrides=None):
        from frankensurf.routes import request_policy
        policy, _ = request_policy(policy, policy_overrides)
        self.calls.append(("extract", url, policy, adapter))
        return {"content": "private raw page", "receipt": {"identity": policy.identity}}

    async def batch(self, urls, policy, adapter=None):
        self.calls.append(("batch", urls, policy, adapter))
        return [{"content": "private raw page", "receipt": {"identity": policy.identity}}]

    async def search(self, query, **kwargs):
        self.calls.append(("search", query, kwargs["policy"], kwargs))
        return {"results": [], "receipt": {"identity": kwargs["policy"].identity}}

    async def download_images(self, urls, policy):
        self.calls.append(("images", urls, policy))
        return [{"status": "failed", "failure": "IDENTITY_EXECUTOR_OFFLINE"}]

    def identity_status(self, identity=None):
        return self.registry.status(identity)


@pytest.fixture
def recording(monkeypatch):
    RecordingRuntime.calls = []
    RecordingRuntime.constructions = []
    monkeypatch.setattr(cli, "Runtime", RecordingRuntime)
    if mcp_server is not None:
        monkeypatch.setattr(mcp_server, "Runtime", RecordingRuntime)
    return RecordingRuntime


@pytest.mark.parametrize("operation", ["read", "extract", "batch", "search", "images"])
async def test_cli_identity_and_registry_reach_each_web_operation(operation, recording, capsys):
    args = cli.parse_args([operation, "https://shop.test/item", "--identity", "personal",
                           "--identity-registry", "/operator/identities.json",
                           "--provider", "local_cdp"])
    await cli.run(args)
    call = recording.calls[-1]
    assert call[0] == operation
    assert call[2].identity == "personal"
    assert call[2].provider == "local_cdp"
    assert recording.constructions[-1]["identity_registry"] == "/operator/identities.json"
    assert "private raw page" not in capsys.readouterr().out


@pytest.mark.skipif(mcp_server is None, reason="MCP optional dependency is not installed")
@pytest.mark.parametrize("operation", ["read", "extract", "batch", "search", "images"])
async def test_mcp_identity_reaches_each_web_operation(operation, recording, monkeypatch):
    monkeypatch.setenv("FRANKENSURF_IDENTITIES", "/operator/identities.json")
    argument = ["https://shop.test/item"] if operation in {"batch", "images"} else "https://shop.test/item"
    result = await getattr(mcp_server, operation)(argument, identity="personal", provider="local_cdp")
    assert recording.calls[-1][2].identity == "personal"
    assert recording.calls[-1][2].provider == "local_cdp"
    assert recording.constructions[-1]["identity_registry"] == "/operator/identities.json"
    assert "private raw page" not in json.dumps(result)


async def enroll_cli(tmp_path, capsys):
    registry_path = tmp_path / "identities.json"
    common = ["--identity-registry", str(registry_path)]
    executor = cli.parse_args(["executor-enroll", "desktop", "--cdp-url", "http://127.0.0.1:9331",
        "--user-data-dir", str(tmp_path / "private-browser"), "--profile-ref", "private-profile-reference", *common])
    await cli.run(executor)
    identity = cli.parse_args(["identity-enroll", "personal", "--executor-id", "desktop",
        "--domain", "shop.test", "--image-domain", "cdn.shop.test", "--auth-url", "https://shop.test/account",
        "--authenticated-selector", "#private-logged-in-marker", "--login-selector", "#private-login-marker", *common])
    await cli.run(identity)
    output = capsys.readouterr().out
    for private in ["127.0.0.1", "private-browser", "private-profile-reference", "private-logged-in-marker", "private-login-marker"]:
        assert private not in output
    return registry_path


async def test_operator_enrollment_status_and_revoke_never_expose_profile_state(tmp_path, capsys):
    registry_path = await enroll_cli(tmp_path, capsys)
    await cli.run(cli.parse_args(["identity-status", "personal", "--identity-registry", str(registry_path)]))
    before = json.loads(capsys.readouterr().out)
    assert before["identities"][0]["authority_mode"] == "LOCAL_ONLY"
    assert before["identities"][0]["auth_check_configured"] is True
    await cli.run(cli.parse_args(["identity-revoke", "personal", "--identity-registry", str(registry_path)]))
    capsys.readouterr()
    await cli.run(cli.parse_args(["identity-status", "--identity-registry", str(registry_path)]))
    status = json.loads(capsys.readouterr().out)
    assert status["identities"][0]["revoked"] is True
    assert status["identities"][0]["generation"] > before["identities"][0]["generation"]
    serialized = json.dumps(status)
    for private in ["endpoint", "user_data_dir", "profile_directory", "private-profile-reference", "vault_refs", "private-logged-in-marker"]:
        assert private not in serialized


@pytest.mark.skipif(mcp_server is None, reason="MCP optional dependency is not installed")
async def test_mcp_status_is_safe_and_operator_mutations_are_not_tools(tmp_path, capsys, recording):
    from frankensurf.identity import IdentityRegistry
    registry_path = await enroll_cli(tmp_path, capsys)
    recording.registry = IdentityRegistry(registry_path)
    result = await mcp_server.identity_status("personal")
    assert result["identity_count"] == 1
    assert result["identities"][0]["id"] == "personal"
    for private in ["127.0.0.1", "private-browser", "private-profile-reference", "private-logged-in-marker"]:
        assert private not in json.dumps(result)
    tools = await mcp_server.server.list_tools()
    names = {tool.name for tool in tools}
    assert {"identity_status", "images"} <= names
    assert not any(word in name for name in names for word in ["enroll", "revoke", "vault", "export", "replicate"])


def test_cli_operator_failure_is_typed_and_does_not_echo_endpoint(tmp_path, capsys):
    with pytest.raises(SystemExit) as failure:
        cli.main(["executor-enroll", "desktop", "--cdp-url", "https://user:privatepassword@cloud.test/privateendpoint",
                  "--user-data-dir", str(tmp_path), "--identity-registry", str(tmp_path / "ids.json")])
    assert failure.value.code == 1
    output = capsys.readouterr().out
    assert "privatepassword" not in output and "privateendpoint" not in output
    assert json.loads(output)["failure"]["code"]


def test_cli_identity_cannot_be_attached_to_operator_import_or_trace(capsys):
    with pytest.raises(SystemExit):
        cli.parse_args(["trace", "trace-id", "--identity", "personal"])
    assert "supported only" in capsys.readouterr().err


def test_cli_auth_check_requires_both_scoped_url_and_authenticated_marker(capsys):
    with pytest.raises(SystemExit):
        cli.parse_args(["identity-enroll", "personal", "--executor-id", "desktop", "--domain", "shop.test",
                        "--auth-url", "https://shop.test/account"])
    assert "requires --auth-url" in capsys.readouterr().err


@pytest.mark.skipif(mcp_server is None, reason="MCP optional dependency is not installed")
async def test_mcp_unknown_identity_status_returns_safe_typed_failure(tmp_path, recording):
    from frankensurf.identity import IdentityRegistry
    recording.registry = IdentityRegistry(tmp_path / "empty-registry.json")
    result = await mcp_server.identity_status("not-registered")
    assert result["status"] == "failed"
    assert result["failure"]["code"] == "IDENTITY_UNKNOWN"
    assert "Traceback" not in json.dumps(result)
