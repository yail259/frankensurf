import io
from datetime import datetime, timezone
import pytest
from PIL import Image
from frankensurf.runtime import Runtime, WebPolicy, WebFailure

def payload():
    buffer = io.BytesIO()
    Image.new("RGB", (3, 4)).save(buffer, format="PNG")
    return buffer.getvalue()

def test_import_preserves_time_and_decodes(tmp_path):
    (tmp_path / "evidence").mkdir()
    runtime = Runtime(state_dir=tmp_path)
    result = runtime.import_image_evidence(payload(), "https://example.com/photo.png", "2020-01-01T00:00:00+00:00")
    assert (result["width"], result["height"], result["format"]) == (3, 4, "PNG")
    assert result["observed_at"] == "2020-01-01T00:00:00+00:00"
    assert result["freshness_seconds"] > 0
    assert result["automation_verified"] is False
    assert result["source_binding"] == "operator_supplied"
    assert result["http_status"] is None

@pytest.mark.parametrize("policy,code", [(WebPolicy(identity="owner"), "IDENTITY_POLICY_DENIED"), (WebPolicy(max_image_bytes=1), "LIMIT_EXCEEDED")])
def test_policy_rejected_before_save(tmp_path, policy, code):
    with pytest.raises(WebFailure) as error:
        Runtime(state_dir=tmp_path).import_image_evidence(payload(), "https://example.com/a", "2020-01-01T00:00:00Z", policy)
    assert error.value.code == code
    assert not list(tmp_path.rglob("*.image"))

def test_invalid_bytes_and_time(tmp_path):
    runtime = Runtime(state_dir=tmp_path)
    with pytest.raises(WebFailure) as error:
        runtime.import_image_evidence(b"not an image", "https://example.com/a", "2020-01-01T00:00:00Z")
    assert error.value.code == "INVALID_IMAGE"
    with pytest.raises(ValueError):
        runtime.import_image_evidence(payload(), "https://example.com/a", "2020-01-01")
