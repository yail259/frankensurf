import json
import httpx
import pytest
from frankensurf import Runtime, WebPolicy
from frankensurf.recovery import recovery_handoff

@pytest.mark.parametrize("code,step", [("BLOCKED", "agent_browser_diagnosis"),
    ("SCHEMA_CHANGED", "adapter_repair"), ("TIMEOUT", "readiness_and_budget_diagnosis"),
    ("PROVIDER_UNAVAILABLE", "provider_health"), ("RATE_LIMITED", "rate_limit_diagnosis")])
def test_typed_next_step(code, step):
    packet = recovery_handoff({"failure": {"code": code}, "trace_id": "trace"})
    assert packet["next_step"] == step
    assert packet["executed"] is False
    assert packet["authority"] == "public_read"

@pytest.mark.parametrize("identity,code", [("owner", "BLOCKED"), (None, "IDENTITY_POLICY_DENIED"), (None, "AUTH_REQUIRED")])
def test_authority_failure_does_not_offer_public_bypass(identity, code):
    packet = recovery_handoff({"identity": identity, "failure": {"code": code},
        "attempts": [{"provider": "local_cdp", "final_url": "https://example.com/?secret=value",
                      "evidence": [{"path": "private-artifact", "content": "secret"}]}]})
    assert packet["next_step"] == "owner_authority"
    assert packet["local_artifacts"] == []
    assert "secret" not in str(packet) and "private-artifact" not in str(packet)

async def test_runtime_trace_contains_handoff(tmp_path):
    async with Runtime(tmp_path, identity_registry=tmp_path / "identities.json",
            transport=httpx.MockTransport(lambda _: httpx.Response(403, text="Denied"))) as web:
        result = await web.read("https://example.com", WebPolicy(provider="http"))
    packet = result["receipt"]["recovery"]
    assert packet["next_step"] == "agent_browser_diagnosis"
    assert packet["attempts"][0]["failure"] == "BLOCKED"
    trace = json.loads((tmp_path / "traces" / (packet["trace_id"] + ".json")).read_text())
    assert trace["receipt"]["recovery"] == packet
    assert result["field_status"]["availability"] == "unknown"


async def test_typed_failure_stage_reaches_receipt_and_trace(tmp_path,monkeypatch):
    from frankensurf.runtime import WebFailure
    import frankensurf.runtime as runtime_module
    def fail(*args,**kwargs):
        raise WebFailure('TIMEOUT','Controlled timeout',failure_stage='filter_panel_apply')
    monkeypatch.setattr(runtime_module,'parse_content',fail)
    async with Runtime(tmp_path,identity_registry=tmp_path/'identities.json',transport=httpx.MockTransport(lambda _: httpx.Response(200,text='Public page'))) as web:
        result=await web.read('https://example.com',WebPolicy(provider='http'))
    r=result['receipt']
    assert r['failure_stage']=='filter_panel_apply'
    assert r['attempts'][0]['failure_stage']=='filter_panel_apply'
    assert r['recovery']['failure_stage']=='filter_panel_apply'
    assert r['recovery']['attempts'][0]['failure_stage']=='filter_panel_apply'


def test_unknown_stage_never_enters_failure_metadata():
    from frankensurf.runtime import WebFailure
    assert WebFailure('TIMEOUT','Failure',failure_stage='secret=value').failure_stage is None
