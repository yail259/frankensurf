import httpx
import pytest

from frankensurf import Runtime, WebPolicy
from frankensurf.actions import ACTION_CLASSES
from frankensurf.routes import PublicRouteRecipe, RouteRecipeError


def test_policy_exposes_exact_spec_action_classes_and_defaults_to_reads():
    assert ACTION_CLASSES == (
        "READ_PUBLIC",
        "READ_AUTHENTICATED",
        "WRITE_REVERSIBLE",
        "WRITE_EXTERNAL",
        "PURCHASE/FINANCIAL",
        "ACCOUNT_SECURITY",
    )
    assert WebPolicy().action_classes == (
        "READ_PUBLIC", "READ_AUTHENTICATED")
    assert WebPolicy(action_classes=("WRITE_EXTERNAL",)).action_classes == (
        "WRITE_EXTERNAL",)


@pytest.mark.parametrize("value", [
    [], (), ("READ_PUBLIC", "READ_PUBLIC"), ("write",),
    ("READ_PUBLIC", 1),
])
def test_invalid_action_policy_is_rejected(value):
    with pytest.raises(ValueError):
        WebPolicy(action_classes=value)


def test_public_route_recipe_cannot_grant_action_authority():
    with pytest.raises(RouteRecipeError):
        PublicRouteRecipe(
            id="unsafe", version="1", origin="https://market.test",
            path_pattern=r"/item", operation="read", provider="http",
            provider_version="1",
            policy_defaults={"action_classes": ["WRITE_EXTERNAL"]})


async def test_public_read_requires_public_read_authority_before_transport(
        tmp_path):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, text="must not run")

    async with Runtime(tmp_path, transport=httpx.MockTransport(handle)) as web:
        result = await web.read(
            "https://market.test/item",
            WebPolicy(provider="http", action_classes=("WRITE_EXTERNAL",)))

    assert calls == []
    assert result["receipt"]["action_class"] == "READ_PUBLIC"
    assert result["receipt"]["failure"]["code"] == "POLICY_DENIED"


async def test_named_read_requires_authenticated_read_before_identity_resolution(
        tmp_path):
    async with Runtime(tmp_path) as web:
        result = await web.read(
            "https://market.test/item",
            WebPolicy(identity="missing",
                      action_classes=("READ_PUBLIC",)))

    assert result["receipt"]["action_class"] == "READ_AUTHENTICATED"
    assert result["receipt"]["failure"]["code"] == "POLICY_DENIED"


async def test_search_requires_public_read_authority_before_source_execution(
        tmp_path):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, text="must not run")

    async with Runtime(tmp_path, transport=httpx.MockTransport(handle)) as web:
        result = await web.search(
            "used bicycle",
            policy=WebPolicy(action_classes=("WRITE_EXTERNAL",)))

    assert calls == []
    assert result["receipt"]["action_class"] == "READ_PUBLIC"
    assert result["receipt"]["failure"]["code"] == "POLICY_DENIED"


async def test_image_read_requires_read_authority_before_transport(tmp_path):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, content=b"must not run")

    async with Runtime(tmp_path, transport=httpx.MockTransport(handle)) as web:
        result = await web.download_images(
            ["https://market.test/image.jpg"],
            WebPolicy(action_classes=("WRITE_EXTERNAL",)))

    assert calls == []
    assert result == [{
        "url": "https://market.test/image.jpg",
        "status": "failed",
        "failure": "POLICY_DENIED",
        "action_class": "READ_PUBLIC",
        "identity": None,
    }]


async def test_success_receipt_declares_effective_read_class(tmp_path):
    async with Runtime(tmp_path, transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, text="<title>Item</title><p>Current listing</p>",
                headers={"content-type": "text/html"}))) as web:
        result = await web.read(
            "https://market.test/item", WebPolicy(provider="http"))

    assert result["receipt"]["status"] == "observed"
    assert result["receipt"]["action_class"] == "READ_PUBLIC"
