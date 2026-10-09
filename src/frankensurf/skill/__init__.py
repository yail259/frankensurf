"""The FrankenSurf agent skill: how an agent should use the tools (SKILL.md)."""
from __future__ import annotations

from pathlib import Path

SKILL = Path(__file__).with_name("SKILL.md")


def text() -> str:
    return SKILL.read_text(encoding="utf-8")


def body() -> str:
    """The guidance without its YAML front matter (for MCP server instructions)."""
    content = text()
    if content.startswith("---"):
        content = content.split("---", 2)[2]
    return content.strip()


def install(directory: str | Path | None = None) -> Path:
    """Copy SKILL.md into a skills folder (default ~/.claude/skills/frankensurf)."""
    target = Path(directory).expanduser() if directory else Path.home() / ".claude" / "skills"
    folder = target / "frankensurf"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "SKILL.md"
    path.write_text(text(), encoding="utf-8")
    return path
