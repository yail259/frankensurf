import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import toolbench
import verify_benchmark_evidence


class FakeWeb:
    def __init__(self): self.calls = 0
    async def read(self, url, policy_overrides=None):
        self.calls += 1
        return {"url": url, "text": "python " * 200, "content": "<p>python</p>",
                "content_type": "text/html", "receipt": {"status": "observed", "trace_id": "trace",
                "authorization": "never-save-this"}}


def case():
    return {"set": "heldout7", "site": "case-one", "wall": "dev", "kind": "article",
            "url": "https://example.org/", "expect": "python", "expect_min": 3}


async def test_retained_content_reproduces_validity_and_detects_tampering(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOLBENCH_EVIDENCE_DIR", str(tmp_path))
    web = FakeWeb()
    row = await toolbench.read(web, "http", case())
    artifact = tmp_path / row["evidence"]["file"]
    assert web.calls == 1 and row["valid"] is True
    assert artifact.stat().st_mode & 0o077 == 0
    assert "never-save-this" not in artifact.read_text()
    assert verify_benchmark_evidence.verify([row], tmp_path) == {"checked": 1, "observed_valid": 1,
        "observed_invalid": 0, "not_observed": 0}
    artifact.write_text(artifact.read_text() + " ")
    with pytest.raises(ValueError, match="digest"):
        verify_benchmark_evidence.verify([row], tmp_path)


async def test_capture_rejects_identity_and_insecure_directory_before_read(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOLBENCH_EVIDENCE_DIR", str(tmp_path))
    web = FakeWeb()
    with pytest.raises(ValueError, match="Identity"):
        await toolbench.read(web, "http", {**case(), "identity": "personal"})
    assert web.calls == 0
    tmp_path.chmod(0o755)
    with pytest.raises(ValueError, match="owner-only"):
        await toolbench.read(web, "http", case())
    assert web.calls == 0


async def test_capture_without_opt_in_writes_no_evidence(tmp_path, monkeypatch):
    monkeypatch.delenv("TOOLBENCH_EVIDENCE_DIR", raising=False)
    row = await toolbench.read(FakeWeb(), "http", case())
    assert "evidence" not in row and not list(tmp_path.glob("*.json"))


async def test_duplicate_evidence_is_rejected_before_second_read(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOLBENCH_EVIDENCE_DIR", str(tmp_path))
    web = FakeWeb()
    await toolbench.read(web, "http", case())
    with pytest.raises(FileExistsError): await toolbench.read(web, "http", case())
    assert web.calls == 1


async def test_failed_observation_can_be_rechecked_without_claiming_page_success(tmp_path, monkeypatch):
    class FailedWeb(FakeWeb):
        async def read(self, url, policy_overrides=None):
            return {"url": url, "receipt": {"status": "failed", "failure": {"code": "CAPTCHA"}}}
    monkeypatch.setenv("TOOLBENCH_EVIDENCE_DIR", str(tmp_path))
    row = await toolbench.read(FailedWeb(), "http", case())
    assert row["valid"] is False
    assert verify_benchmark_evidence.verify([row], tmp_path)["not_observed"] == 1


async def test_oversized_evidence_is_not_silently_truncated(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOLBENCH_EVIDENCE_DIR", str(tmp_path))
    monkeypatch.setenv("TOOLBENCH_EVIDENCE_MAX_BYTES", "100")
    web = FakeWeb()
    with pytest.raises(ValueError, match="exceeds"):
        await toolbench.read(web, "http", case())
    assert web.calls == 1 and not list(tmp_path.glob("*.json"))
