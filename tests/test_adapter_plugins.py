import httpx
import pytest
from frankensurf import Runtime,WebPolicy
from frankensurf import adapters
from frankensurf.adapters import AdapterManifest,AdapterRegistry,AdapterRequest
from frankensurf.runtime import WebFailure


class ExactAdapter:
    manifest=AdapterManifest("example_exact","1")
    def extract(self,request):
        assert request.url == "https://example.com/item"
        if request.content != "item:123": raise WebFailure("SCHEMA_CHANGED","Wrong exact item")
        return {"title":"Item","text":"123","structured":{"listing_id":"123"},"image_urls":[]}


async def test_registered_adapter_executes_through_public_runtime(tmp_path,monkeypatch):
    registry=AdapterRegistry();registry.register(ExactAdapter())
    monkeypatch.setattr(adapters,"DEFAULT_ADAPTERS",registry)
    async with Runtime(tmp_path,transport=httpx.MockTransport(lambda _:httpx.Response(200,text="item:123"))) as web:
        result=await web.extract("https://example.com/item","example_exact",WebPolicy(provider="http"))
    assert result["receipt"]["status"] == "observed"
    assert result["structured"]["listing_id"] == "123"
    assert registry.inspect()==[{"id":"example_exact","version":"1","enabled":True}]


def test_disabled_plugin_cannot_execute_and_duplicate_registration_fails():
    registry=AdapterRegistry();registry.register(ExactAdapter());registry.enable("example_exact",False)
    with pytest.raises(WebFailure) as error:registry.project("example_exact",AdapterRequest("item:123","text/plain","https://example.com/item"))
    assert error.value.code == "PLUGIN_DISABLED"
    with pytest.raises(ValueError):registry.register(ExactAdapter())


def test_plugin_exception_data_never_reaches_typed_failure():
    class Broken(ExactAdapter):
        def extract(self,request):raise RuntimeError("secret-do-not-export")
    registry=AdapterRegistry();registry.register(Broken())
    with pytest.raises(WebFailure) as error:registry.project("example_exact",AdapterRequest("item:123","text/plain","https://example.com/item"))
    assert error.value.code == "SCHEMA_CHANGED" and "secret-do-not-export" not in str(error.value)


def test_malformed_projection_fails_instead_of_becoming_success():
    class Broken(ExactAdapter):
        def extract(self,request):return {"text":"123","structured":{},"image_urls":"not-a-list"}
    registry=AdapterRegistry();registry.register(Broken())
    with pytest.raises(WebFailure):registry.project("example_exact",AdapterRequest("item:123","text/plain","https://example.com/item"))


async def test_runtime_snapshot_ignores_later_adapter_disable(tmp_path,monkeypatch):
    registry=AdapterRegistry();registry.register(ExactAdapter())
    monkeypatch.setattr(adapters,"DEFAULT_ADAPTERS",registry)
    calls=[]
    def response(request):
        calls.append(request.url)
        assert len(calls)==1
        return httpx.Response(200,text="item:123")
    async with Runtime(tmp_path,transport=httpx.MockTransport(response)) as web:
        first=await web.extract("https://example.com/item","example_exact",WebPolicy(provider="http"))
        assert first["receipt"]["adapter_version"] == "1"
        registry.enable("example_exact",False)
        second=await web.extract("https://example.com/item","example_exact",WebPolicy(provider="http",freshness="cached"))
    assert second["receipt"]["status"] == "observed" and second["receipt"]["cache_hit"]
    assert len(calls)==1


async def test_adapter_version_scope_changes_on_next_runtime_snapshot(tmp_path,monkeypatch):
    registry=AdapterRegistry();registry.register(ExactAdapter());monkeypatch.setattr(adapters,"DEFAULT_ADAPTERS",registry)
    async with Runtime(tmp_path) as web:
        first=web._cache_key("https://example.com/item","example_exact",WebPolicy())
        class NewVersion(ExactAdapter):manifest=AdapterManifest("example_exact","2")
        newer=AdapterRegistry();newer.register(NewVersion());monkeypatch.setattr(adapters,"DEFAULT_ADAPTERS",newer)
        second=web._cache_key("https://example.com/item","example_exact",WebPolicy())
    async with Runtime(tmp_path) as web:
        third=web._cache_key("https://example.com/item","example_exact",WebPolicy())
    assert first == second and first != third


async def test_short_validated_html_projection_stays_on_http(tmp_path,monkeypatch):
    class ShortHtml(ExactAdapter):
        def extract(self,request):
            if request.content != '<script>window.example=1</script><p>item:123</p>':
                raise WebFailure("SCHEMA_CHANGED","Wrong item")
            return {"title":"Item","text":"123","structured":{"listing_id":"123"},"image_urls":[]}
    registry=AdapterRegistry();registry.register(ShortHtml());monkeypatch.setattr(adapters,"DEFAULT_ADAPTERS",registry)
    async with Runtime(tmp_path,transport=httpx.MockTransport(lambda _:httpx.Response(200,text='<script>window.example=1</script><p>item:123</p>',headers={"content-type":"text/html"}))) as web:
        async def browser(*args): raise AssertionError("Validated projection should not open a browser")
        web._get_browser=browser
        result=await web.extract("https://example.com/item","example_exact")
    assert result["receipt"]["status"] == "observed"
    assert result["receipt"]["method"] == "http"
    assert len(result["receipt"]["attempts"]) == 1
