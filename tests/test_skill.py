"""The agent skill ships with the package, installs, and is the MCP server's instructions."""
import re

import pytest

from frankensurf import skill


def test_skill_has_front_matter_and_names_real_tools():
    text = skill.text()
    assert text.startswith("---\nname: frankensurf\ndescription: ")
    body = skill.body()
    assert not body.startswith("---")
    # Every tool the skill tells agents to call exists on the MCP server.
    pytest.importorskip("mcp")
    from frankensurf import mcp_server
    named = set(re.findall(r"`([a-z_]+)\(", body))
    for tool in named:
        assert hasattr(mcp_server, tool), tool
    assert mcp_server.server.instructions == body


def test_install_copies_into_a_skills_folder(tmp_path):
    path = skill.install(tmp_path / "skills")
    assert path == tmp_path / "skills" / "frankensurf" / "SKILL.md"
    assert path.read_text(encoding="utf-8") == skill.text()


def test_cli_skill_install(tmp_path, monkeypatch, capsys):
    import json
    import sys
    from frankensurf import cli
    monkeypatch.setattr(sys, "argv", ["frankensurf", "skill", "install", "--skill-dir", str(tmp_path)])
    cli.main()
    out = json.loads(capsys.readouterr().out)
    assert out["installed"].endswith("frankensurf/SKILL.md")
