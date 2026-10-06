"""Public extraction failures retain evidence without claiming semantic success."""
from pathlib import Path
import httpx
import pytest
from frankensurf import Runtime, WebPolicy
import frankensurf.runtime as runtime_module

@pytest.mark.parametrize("code,retain", [("SCHEMA_CHANGED", True), ("IDENTITY_POLICY_DENIED", False)])
async def test_failure_evidence(tmp_path, monkeypatch, code, retain):
    body = b"<html><body>Wrong catalogue page</body></html>"
    def fail(*args, **kwargs):
        raise runtime_module.WebFailure(code, "Controlled extraction failure")
    monkeypatch.setattr(runtime_module, "parse_content", fail)
    async with Runtime(tmp_path, transport=httpx.MockTransport(lambda _: httpx.Response(
            200, content=body, headers={"content-type": "text/html"}))) as web:
        result = await web.read("https://catalogue.test/page/2", WebPolicy(provider="http"))
    receipt = result["receipt"]
    assert receipt["status"] == "failed" and receipt["failure"]["code"] == code
    assert result["structured"] is None and result["content"] == ""
    evidence = receipt["attempts"][0].get("evidence", [])
    if retain:
        assert len(evidence) == 1
        assert Path(evidence[0]["path"]).read_bytes() == body
    else:
        assert evidence == [] and receipt["evidence"] == []
        assert not list((tmp_path / "evidence").iterdir())
    assert not list((tmp_path / "cache").glob("*.json"))

async def test_failure_evidence_can_be_disabled(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise runtime_module.WebFailure("SCHEMA_CHANGED", "Mismatch")
    monkeypatch.setattr(runtime_module, "parse_content", fail)
    async with Runtime(tmp_path, transport=httpx.MockTransport(lambda _: httpx.Response(
            200, text="Public page", headers={"content-type": "text/html"}))) as web:
        result = await web.read("https://catalogue.test/page", WebPolicy(
            provider="http", retain_public_failure_evidence=False))
    assert not result["receipt"]["attempts"][0].get("evidence")
    assert not list((tmp_path / "evidence").iterdir())
