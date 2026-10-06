"""The calling agent's "try harder" lever: resume from a trace, strongest first."""
import pytest

from frankensurf import providers
from frankensurf.providers import ProviderManifest, ProviderRegistry
from frankensurf.runtime import Runtime

URL = "https://shop.example.com/search?q=lamp"
PAGE = "<html><title>Search</title><body>" + "<p>Lamp $49, brass, in stock.</p>" * 40 + "</body></html>"


def plugin(identifier, **manifest):
    class Plugin:
        calls = 0

        def __init__(self):
            self.manifest = ProviderManifest(identifier, "1", **manifest)

        def available(self, configured):
            return True

        async def acquire(self, request, services):
            type(self).calls += 1
            return {"url": request.url, "content": PAGE, "content_type": "text/html", "http_status": 200}
    return Plugin()


@pytest.fixture
def tools(monkeypatch):
    items = [plugin("cheap"), plugin("middle", rendering=True), plugin("strong", rendering=True)]
    registry = ProviderRegistry()
    for item in items:
        registry.register(item)
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    return items


POLICY = {"origin_route_hint_ttl_seconds": 0, "second_opinion_text_chars": 0,
          "completeness_ladder": ["strong", "middle"]}


async def test_retry_of_skips_tried_tools_and_starts_strongest(tmp_path, tools):
    async with Runtime(tmp_path) as web:
        first = await web.read(URL, policy_overrides=POLICY)
        hint = first["receipt"]["if_not_right"]
        assert first["receipt"]["method"] == "cheap"
        assert hint["retry_of"] == first["receipt"]["trace_id"] and hint["untried"] == ["strong", "middle"]
        harder = await web.read(URL, policy_overrides=POLICY, retry_of=hint["retry_of"])
        assert harder["receipt"]["method"] == "strong"
        assert harder["receipt"]["try_harder"] == {"retry_of": hint["retry_of"], "excluded": ["cheap"]}
        assert harder["receipt"]["if_not_right"]["untried"] == ["middle"]
        hardest = await web.read(URL, policy_overrides=POLICY, retry_of=harder["receipt"]["trace_id"])
        assert hardest["receipt"]["method"] == "middle"
        assert "untried" in hardest["receipt"]["if_not_right"] and hardest["receipt"]["if_not_right"]["untried"] == []
        assert "handoff" in hardest["receipt"]["if_not_right"]["how"]
        nothing = await web.read(URL, policy_overrides=POLICY, retry_of=hardest["receipt"]["trace_id"])
    assert nothing["receipt"]["status"] == "failed" and nothing["receipt"]["attempts"] == []
    assert "already tried" in nothing["receipt"]["failure"]["message"]


async def test_retry_of_needs_a_real_trace(tmp_path, tools):
    async with Runtime(tmp_path) as web:
        with pytest.raises(ValueError):
            await web.read(URL, retry_of="0" * 32)
