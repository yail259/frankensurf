import pytest
from frankensurf import Runtime, WebPolicy
from frankensurf.runtime import WebFailure


async def test_public_block_can_escalate_when_policy_allows(tmp_path, monkeypatch):
    calls = []
    async with Runtime(tmp_path) as web:
        async def http(url, policy):
            calls.append("http")
            raise WebFailure("BLOCKED", "blocked")
        async def browser(url, policy, provider):
            calls.append(provider)
            return {"url": url, "content": "<h1>Recovered</h1><p>Item page rendered by the browser, with its full description.</p>",
                    "content_type": "text/html", "http_status": 200}
        monkeypatch.setattr(web, "_get_http", http)
        monkeypatch.setattr(web, "_get_browser", browser)
        web.steel_api_url = None
        first = await web.read("https://example.com/item", WebPolicy(terminal_failures=("BLOCKED",), context_stop_failures=("BLOCKED",)))
        assert first["receipt"]["failure"]["code"] == "BLOCKED"
        assert calls == ["http"]
        second = await web.read("https://example.com/item", WebPolicy())
    assert second["receipt"]["status"] == "observed"
    assert calls == ["http", "http", "local"]
    assert second["receipt"]["attempts"][0]["failure"] == "BLOCKED"


@pytest.mark.parametrize("value", [[], "BLOCKED", ("",), (None,)])
@pytest.mark.parametrize("field", ["terminal_failures", "context_stop_failures"])
def test_failure_policy_is_immutable_code_tuple(field, value):
    with pytest.raises(ValueError):
        WebPolicy(**{field: value})


async def test_registered_candidate_order_and_explicit_override(tmp_path, monkeypatch):
    from frankensurf import providers
    from frankensurf.providers import ProviderRegistry, ProviderManifest
    calls = []
    class Plugin:
        def __init__(self, name):
            self.manifest = ProviderManifest(name, "1")
        async def acquire(self, request, services):
            calls.append(self.manifest.id)
            if self.manifest.id == "first":
                raise WebFailure("BLOCKED", "blocked")
            return {"url": request.url, "content": "<h1>Recovered</h1>", "content_type": "text/html", "http_status": 200}
    registry = ProviderRegistry()
    registry.register(Plugin("first"))
    registry.register(Plugin("second"))
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    # Site seeds must not replace caller-selected candidates.
    monkeypatch.setattr("frankensurf.experimental.installed", lambda name: True)
    async with Runtime(tmp_path) as web:
        result = await web.read("https://www.ebay.com.au/sch/i.html", WebPolicy(provider_candidates=("first", "second"), terminal_failures=()))
        assert result["receipt"]["status"] == "observed"
        assert calls == ["first", "second"]
        calls.clear()
        result = await web.read("https://example.com/item", WebPolicy(provider="second", provider_candidates=("first", "second")))
        assert result["receipt"]["status"] == "observed"
        assert calls == ["second"]


@pytest.mark.parametrize("value", [(), [], "http", ("http", "http"),
                                   ("Bad",), ("bad/provider",), (None,)])
def test_candidate_configuration_rejects_invalid_lists(value):
    with pytest.raises(ValueError):
        WebPolicy(provider_candidates=value)


def test_candidate_configuration_accepts_safe_deferred_plugin_ids():
    policy = WebPolicy(provider_candidates=("installed_later",))
    assert policy.provider_candidates == ("installed_later",)
