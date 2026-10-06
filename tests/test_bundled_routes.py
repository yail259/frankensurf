"""Benchmark-earned routes are reachable through every ordinary local surface."""
import hashlib
import io
import json
from pathlib import Path
import runpy

import httpx
from PIL import Image
import pytest

from frankensurf import Runtime, WebPolicy, adapters, cli, mcp_server, providers
from frankensurf.adapters import AdapterManifest
from frankensurf.providers import ProviderManifest, ProviderRegistry
from frankensurf.routes import RouteRecipeError, RouteRecipeRegistry


URL = "https://www.depop.com/search/?q=camera"
CEX_SEARCH = "https://au.webuy.com/search/?stext=iphone"
CEX_DETAIL = "https://au.webuy.com/product-detail/?id=SAPPI15128GBLAUNLB"
REEBELO_DETAIL = "https://reebelo.com.au/collections/apple-iphone-15"


def image_transport():
    picture = io.BytesIO()
    Image.new("RGB", (3, 2), "blue").save(picture, format="PNG")
    return httpx.MockTransport(lambda _: httpx.Response(
        200, content=picture.getvalue(), headers={"content-type": "image/png"}))


def test_invalid_bundled_configuration_is_never_silently_ignored(tmp_path, monkeypatch):
    broken = tmp_path / "bundled.json"
    broken.write_text('{"schema":"wrong","recipes":[]}')
    registry = RouteRecipeRegistry(tmp_path / "state/routes.json")
    monkeypatch.setattr(registry, "_bundled_path", lambda: broken)
    with pytest.raises(RouteRecipeError):
        registry.inspect()


def test_cli_paginate_requires_requested_leaf_adapter():
    with pytest.raises(SystemExit):
        cli.parse_args(["paginate", URL])
