import asyncio
import pytest
from frankensurf import mcp_server


@pytest.mark.parametrize("field", ["identity", "provider", "freshness", "include_images"])
def test_options_cannot_silently_replace_explicit_authority(field):
    with pytest.raises(ValueError):
        asyncio.run(mcp_server.extract("https://example.com", identity="owner", acquisition_policy={field: None}))


def test_stdio_json_policy_and_authority(tmp_path):
    import os
    import json
    import sys
    from pathlib import Path
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    child = """
from frankensurf import mcp_server
class Web:
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def extract(self, url, adapter, policy=None, *, policy_overrides=None):
        from frankensurf.routes import request_policy
        policy, _ = request_policy(policy, policy_overrides)
        result = {'candidates': list(policy.compound_source_candidates),
                  'readiness': policy.scrapling_navigation_wait_until,
                  'content': 'must be removed'}
        return result
mcp_server.runtime = Web
mcp_server.main()
"""
    async def check():
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
        params = StdioServerParameters(command=sys.executable, args=['-c', child], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tool = next(t for t in (await session.list_tools()).tools if t.name == 'extract')
                assert 'acquisition_policy' in tool.inputSchema['properties']
                assert 'adapter' in tool.inputSchema['properties']
                assert 'adapter_contract' not in tool.inputSchema['properties']
                result = await session.call_tool('extract', {'url': 'https://example.com',
                    'acquisition_policy': {'compound_source_candidates': ['scrapling'],
                                           'scrapling_navigation_wait_until': 'commit'}})
                assert not result.isError
                assert (result.structuredContent or json.loads(result.content[0].text)) == {'candidates': ['scrapling'], 'readiness': 'commit'}
                denied = await session.call_tool('extract', {'url': 'https://example.com',
                    'acquisition_policy': {'identity': 'owner'}})
                assert denied.isError
    asyncio.run(check())
