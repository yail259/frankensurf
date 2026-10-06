"""CLI omission reaches the ordinary route planner and registered plugin gates."""
import pytest

from frankensurf import Runtime, WebPolicy, adapters, cli, providers
from frankensurf.adapters import AdapterManifest, AdapterRegistry
from frankensurf.providers import ProviderManifest, ProviderRegistry
from frankensurf.routes import PublicRouteRecipe


TARGET = "https://cli.test/items/1"
PROVIDER = "cli_registered_browser"
ADAPTER = "cli_registered_projection"


@pytest.fixture
def plugins(monkeypatch):
    calls = []

    class Provider:
        def __init__(self, identifier):
            self.manifest = ProviderManifest(identifier, "1", rendering=True)

        def available(self, configured):
            return self.manifest.id == "http"

        async def acquire(self, request, services):
            calls.append((self.manifest.id, request.policy))
            return {"url": request.url, "content": "<html><title>CLI evidence</title><p>" +
                    "observed public content " * 12 + "</p></html>",
                    "content_type": "text/html", "http_status": 200}

    class Adapter:
        manifest = AdapterManifest(ADAPTER, "1")

        def extract(self, request):
            return {"text": "CLI projection", "image_urls": [],
                    "structured": {"source_url": request.url}}

    provider_registry = ProviderRegistry()
    provider_registry.register(Provider("http"))
    provider_registry.register(Provider(PROVIDER))
    adapter_registry = AdapterRegistry()
    adapter_registry.register(Adapter())
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", provider_registry)
    monkeypatch.setattr(adapters, "DEFAULT_ADAPTERS", adapter_registry)
    return provider_registry, adapter_registry, calls


def arguments(tmp_path, operation="extract", *flags):
    values = [operation, TARGET, "--state", str(tmp_path / "state"),
              "--identity-registry", str(tmp_path / "empty-identities.json")]
    if operation == "extract":
        values.extend(["--adapter", ADAPTER])
    return cli.parse_args([*values, *flags])


async def register_recipe(tmp_path, operation="extract"):
    item = PublicRouteRecipe(id="cli_operator_recipe", version="1", origin="https://cli.test",
        path_pattern=r"/items/[0-9]+", operation=operation, provider=PROVIDER, provider_version="1",
        adapter=ADAPTER if operation == "extract" else None,
        adapter_version="1" if operation == "extract" else None,
        policy_defaults={"timeout_seconds": 80, "settle_ms": 8000, "max_images": 12},
        provenance={"source_commit": "a" * 40, "report_sha256": "b" * 64,
                    "case_ids": ("cli-controlled-case",)})
    async with Runtime(state_dir=tmp_path / "state",
                       identity_registry=tmp_path / "empty-identities.json") as web:
        web.routes.register(item)


@pytest.mark.parametrize("operation", ["read", "extract"])
async def test_omitted_cli_flags_execute_scoped_80_second_recipe(tmp_path, plugins, operation, capsys):
    await register_recipe(tmp_path, operation)
    result = await cli.run(arguments(tmp_path, operation))
    assert result["receipt"]["status"] == "observed"
    assert result["receipt"]["route_recipe"]["id"] == "cli_operator_recipe"
    assert [(identifier, policy.timeout_seconds, policy.settle_ms) for identifier, policy in plugins[2]] == [
        (PROVIDER, 80, 8000)]
    assert "content" not in result
    assert "<html>" not in capsys.readouterr().out


@pytest.mark.parametrize("timeout", [5, 25])
async def test_explicit_timeout_and_zero_cap_are_preserved(tmp_path, plugins, timeout):
    await register_recipe(tmp_path)
    result = await cli.run(arguments(tmp_path, "extract", "--timeout", str(timeout), "--max-images", "0"))
    assert result["receipt"]["status"] == "observed"
    assert [(identifier, policy.timeout_seconds, policy.max_images) for identifier, policy in plugins[2]] == [
        ("http", timeout, 0)]
    assert result["receipt"]["routing"]["operator_recipes"]["skipped"][0]["reason"] == "EXPLICIT_POLICY_CONFLICT"


async def test_registered_provider_choice_is_accepted_then_runtime_checks_enablement(tmp_path, plugins):
    args = arguments(tmp_path, "extract", "--provider", PROVIDER)
    result = await cli.run(args)
    assert result["receipt"]["status"] == "observed"
    assert plugins[2][-1][0] == PROVIDER
    plugins[0].enable(PROVIDER, False)
    denied = await cli.run(arguments(tmp_path, "extract", "--provider", PROVIDER))
    assert denied["receipt"]["failure"]["code"] == "PLUGIN_DISABLED"
    assert len(plugins[2]) == 1


async def test_registered_adapter_choice_is_accepted_then_runtime_checks_enablement(tmp_path, plugins):
    result = await cli.run(arguments(tmp_path))
    assert result["structured"]["source_url"] == TARGET
    plugins[1].enable(ADAPTER, False)
    denied = await cli.run(arguments(tmp_path))
    assert denied["receipt"]["failure"]["code"] == "PLUGIN_DISABLED"
    assert len(plugins[2]) == 1


async def test_empty_registry_cli_defaults_match_existing_web_policy(tmp_path, plugins):
    result = await cli.run(arguments(tmp_path))
    assert result["receipt"]["status"] == "observed"
    actual = plugins[2][-1][1]
    expected = WebPolicy()
    assert actual.timeout_seconds == expected.timeout_seconds == 25
    assert actual.max_images == expected.max_images == 50
    assert actual.freshness == expected.freshness == "now"
    assert actual.render is False and actual.include_images is False
    assert actual.navigation_page == expected.navigation_page == 1
    assert "route_recipe" not in result["receipt"]


def test_cli_exposes_generic_provider_retry_policy(tmp_path, plugins):
    args = arguments(
        tmp_path, "extract",
        "--provider-max-attempts-per-candidate", "3",
        "--provider-retry-delay", "0.5",
        "--provider-retry-failure", "TIMEOUT",
        "--provider-retry-failure", "BLOCKED",
    )
    policy = WebPolicy(**cli._policy_kwargs(args))
    assert policy.provider_max_attempts_per_candidate == 3
    assert policy.provider_retry_delay_seconds == 0.5
    assert policy.provider_retry_failures == ("TIMEOUT", "BLOCKED")
