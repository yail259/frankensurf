"""HTTP errors are failed acquisitions, including previously cached false successes."""
import json

import httpx
import pytest

from frankensurf import Runtime, WebPolicy
import frankensurf.runtime as runtime_module


KNOWN = {401: "AUTH_REQUIRED", 403: "BLOCKED", 404: "NOT_FOUND", 410: "NOT_FOUND", 429: "RATE_LIMITED"}


def test_every_http_client_error_has_a_failure_classification():
    for status in range(400, 500):
        assert runtime_module._status_failure(status) == KNOWN.get(status, "UNKNOWN")
    assert runtime_module._status_failure(200) is None
    assert runtime_module._status_failure(302) is None
    assert runtime_module._status_failure(503) == "PROVIDER_DOWN"


@pytest.mark.parametrize("status,expected", [
    (400, "UNKNOWN"), (402, "UNKNOWN"), (405, "UNKNOWN"), (408, "UNKNOWN"),
    (409, "UNKNOWN"), (422, "UNKNOWN"), (451, "UNKNOWN"), (499, "UNKNOWN"),
    (401, "AUTH_REQUIRED"), (403, "BLOCKED"), (404, "NOT_FOUND"),
    (410, "NOT_FOUND"), (429, "RATE_LIMITED"), (503, "PROVIDER_DOWN"),
])
async def test_real_http_read_preserves_specific_and_generic_failure_semantics(tmp_path, status, expected):
    async with Runtime(tmp_path, transport=httpx.MockTransport(lambda _: httpx.Response(status))) as web:
        result = await web.read("https://status.test/product", WebPolicy(provider="http"))
    assert result["receipt"]["status"] == "failed"
    assert result["receipt"]["http_status"] == status
    assert result["receipt"]["failure"]["code"] == expected
    assert result["receipt"]["evidence"] == [] and result["structured"] is None


async def test_old_observed_http_418_cache_is_reacquired_as_failure(tmp_path):
    calls = []
    status = 200
    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(status, text="Body", headers={"content-type":"text/plain"})
    async with Runtime(tmp_path, transport=httpx.MockTransport(handle)) as web:
        await web.read("https://status.test/product", WebPolicy(provider="http"))
        for path in (tmp_path / "cache").glob("*.json"):
            old = json.loads(path.read_text())
            old["receipt"].update(status="observed", http_status=418, failure=None)
            path.write_text(json.dumps(old))
        status = 418
        result = await web.read("https://status.test/product", WebPolicy(provider="http", freshness="hour"))
    assert len(calls) == 2
    assert result["receipt"]["cache_hit"] is False
    assert result["receipt"]["status"] == "failed" and result["receipt"]["http_status"] == 418
    assert result["receipt"]["failure"]["code"] == "UNKNOWN"
    assert result["receipt"]["evidence"] == []
