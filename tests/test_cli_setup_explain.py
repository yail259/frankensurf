"""frankensurf setup and read --explain."""
import subprocess

import pytest

from frankensurf import cli, experimental
from frankensurf.cli import _explain, parse_args

READ = {
    "url": "https://shop.example.com/search?q=lamp", "title": "Lamps | Example", "text": "Brass lamp $49 " * 40,
    "receipt": {
        "status": "observed", "requested_url": "https://shop.example.com/search?q=lamp", "method": "camoufox",
        "trace_id": "a" * 32, "cost_usd": 0.0,
        "attempts": [{"provider": "http", "status": "failed", "failure": "BLOCKED", "latency_ms": 412},
                     {"provider": "jina_reader", "status": "failed", "failure": "CAPTCHA", "latency_ms": 2100},
                     {"provider": "camoufox", "status": "observed", "latency_ms": 6200}],
        "completeness": {"complete": True, "item_links": 24, "escalations": []}}}


def test_explain_shows_each_tool_and_the_page():
    text = _explain(READ)
    assert "✗ http" in text and "BLOCKED" in text and "0.4s" in text
    assert "✓ camoufox" in text and "got the page" in text
    assert "Lamps | Example" in text and "complete (24 results)" in text and ", free" in text
    assert "a" * 32 in text


def test_explain_says_why_a_read_failed():
    failed = {"url": "https://x.example/", "receipt": {
        "status": "failed", "requested_url": "https://x.example/", "trace_id": "b" * 32,
        "attempts": [{"provider": "http", "status": "failed", "failure": "CAPTCHA"}],
        "failure": {"code": "CAPTCHA", "message": "Provider returned a challenge"},
        "next_step": {"provider": "handoff", "how": "read again with allow_handoff"}}}
    text = _explain(failed)
    assert "Failed: CAPTCHA" in text and "Next step: read again with allow_handoff" in text


def test_setup_parses_and_rejects_arguments():
    assert parse_args(["setup"]).no_stealth is False
    assert parse_args(["setup", "--no-stealth"]).no_stealth is True
    with pytest.raises(SystemExit):
        parse_args(["setup", "extra"])
    assert parse_args(["read", "https://example.com", "--explain"]).explain is True


def test_setup_installs_chromium_then_the_stealth_providers(monkeypatch):
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda command, **_: ran.append(command) or subprocess.CompletedProcess(command, 0))
    monkeypatch.setattr(experimental, "install_free_providers",
                        lambda log: {"camoufox": True, "scrapling": True, "patchright": True})
    result = cli._setup(parse_args(["setup"]))
    assert ran and ran[0][1:] == ["-m", "playwright", "install", "chromium"]
    assert result["chromium"] is True and result["stealth_providers"]["camoufox"] is True
    skipped = cli._setup(parse_args(["setup", "--no-stealth"]))
    assert skipped["stealth_providers"].startswith("skipped")


def test_the_stealth_requirements_ship_inside_the_package():
    from pathlib import Path
    requirements = Path(experimental.__file__).with_name("provider_requirements.txt").read_text()
    assert "camoufox==" in requirements and "scrapling" in requirements and "patchright==" in requirements
