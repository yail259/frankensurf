import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.prepare_local_identity import run


def arguments(*, launch=False, login_url="https://www.facebook.com/login/"):
    return SimpleNamespace(
        identity="facebook-local",
        login_url=login_url,
        domain=["www.facebook.com"],
        image_domain=["*.fbcdn.net"],
        path_prefix=["/marketplace/"],
        snapshot_root=['[role="main"]'],
        port=9341,
        geography="Sydney",
        launch=launch,
    )


@pytest.mark.asyncio
async def test_plan_only_validates_without_mutating_profile_or_registry(
        tmp_path, monkeypatch, capsys):
    registry = tmp_path/"identities.json"
    monkeypatch.setenv("FRANKENSURF_IDENTITIES", str(registry))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    await run(arguments())

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "plan_only"
    assert output["mutation_requested"] is False
    assert output["automated_navigation"] is False
    assert output["profile_export"] is False
    assert not registry.exists()
    assert not (tmp_path/".frankensurf").exists()


@pytest.mark.asyncio
async def test_invalid_plan_fails_before_files_or_browser_launch(
        tmp_path, monkeypatch):
    registry = tmp_path/"identities.json"
    monkeypatch.setenv("FRANKENSURF_IDENTITIES", str(registry))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    with pytest.raises(ValueError):
        await run(arguments(
            launch=True,
            login_url="https://example.com/login/"))

    assert not registry.exists()
    assert not (tmp_path/".frankensurf").exists()
