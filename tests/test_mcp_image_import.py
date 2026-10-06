import asyncio
import io
from PIL import Image
from frankensurf import mcp_server

def test_mcp_import_local_reference(tmp_path, monkeypatch):
    monkeypatch.setenv("FRANKENSURF_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FRANKENSURF_IDENTITIES", str(tmp_path / "identities.json"))
    image = tmp_path / "photo.png"
    Image.new("RGB", (7, 8)).save(image)
    result = asyncio.run(mcp_server.import_image_evidence(str(image), "https://example.com/photo", "2020-01-01T00:00:00Z"))
    assert (result["width"], result["height"]) == (7, 8)
    assert result["automation_verified"] is False
    assert "raw" not in result
    assert result["source_binding"] == "operator_supplied"
    limited = asyncio.run(mcp_server.import_image_evidence(str(image), "https://example.com/photo", "2020-01-01T00:00:00Z", 1))
    assert limited["failure"]["code"] == "LIMIT_EXCEEDED"

def test_mcp_bad_file_is_sanitized(tmp_path):
    result = asyncio.run(mcp_server.import_image_evidence(str(tmp_path / "private-missing-file"), "https://example.com/a", "2020-01-01T00:00:00Z"))
    assert result["failure"]["code"] == "INVALID_EVIDENCE"
    assert "private-missing-file" not in str(result)


def test_real_stdio_tool_discovery_and_import(tmp_path):
    import os
    import sys
    import json
    from pathlib import Path
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    image = tmp_path / "export.png"
    Image.new("RGB", (9, 10)).save(image)
    async def check():
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
                   FRANKENSURF_STATE=str(tmp_path / "state"),
                   FRANKENSURF_IDENTITIES=str(tmp_path / "identities.json"))
        params = StdioServerParameters(command=sys.executable,
            args=["-c", "from frankensurf.mcp_server import main; main()"], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert "import_image_evidence" in {tool.name for tool in tools.tools}
                result = await session.call_tool("import_image_evidence", {
                    "path": str(image), "url": "https://example.com/export.png",
                    "observed_at": "2020-01-01T00:00:00Z"})
                assert not result.isError
                data = result.structuredContent
                if data is None:
                    data = json.loads(next(item.text for item in result.content if item.type == "text"))
                assert (data["width"], data["height"]) == (9, 10)
                assert data["automation_verified"] is False
                assert data["source_binding"] == "operator_supplied"
                assert "raw" not in data
                failed = await session.call_tool("import_image_evidence", {
                    "path": str(tmp_path / "private-name-missing"),
                    "url": "https://example.com/export.png", "observed_at": "2020-01-01T00:00:00Z"})
                assert "private-name-missing" not in str(failed.content)
    asyncio.run(check())
