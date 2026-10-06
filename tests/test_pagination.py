import pytest
from frankensurf import Runtime, WebPolicy


def page(ids, target=None):
    return {"receipt": {"status": "observed"}, "structured": {
        "listings": [{"listing_id": i, "title": i} for i in ids], "next_url": target}}


async def test_runtime_pagination_preserves_overlap_and_continuation_adapter(tmp_path, monkeypatch):
    calls = []
    responses = [page(["1", "2"], "https://example.com/p2"), page(["2", "3"])]
    async with Runtime(tmp_path) as web:
        async def extract(url, adapter, policy):
            calls.append((url, adapter, policy.identity))
            return responses[len(calls)-1]
        monkeypatch.setattr(web, "extract", extract)
        result = await web.paginate("https://example.com/p1", "html", WebPolicy(identity="owner"), continuation_adapter="json")
    assert [r["listing_id"] for r in result["listings"]] == ["1", "2", "3"]
    assert result["overlap_count"] == 1 and result["pages"] == responses
    assert [(p["raw_count"], p["new_count"], p["overlap_count"], p["unique_total"]) for p in result["page_progress"]] == [(2,2,0,2),(2,1,1,3)]
    assert result["status"] == "continuation_exhausted" and result["catalogue_complete"] is None
    assert calls[1][1:] == ("json", "owner")


@pytest.mark.parametrize("target,status", [("https://evil.example/p2", "invalid_continuation"),
    ("https://example.com/p1", "continuation_cycle"), ("https://example.com/p2", "failed")])
async def test_unsafe_cycles_and_failures_remain_explicit(tmp_path, monkeypatch, target, status):
    calls = []
    async with Runtime(tmp_path) as web:
        async def extract(url, adapter, policy):
            calls.append(url)
            return page(["1"], target) if len(calls) == 1 else {"receipt": {"status": "failed", "failure": {"code": "BLOCKED"}}}
        monkeypatch.setattr(web, "extract", extract)
        result = await web.paginate("https://example.com/p1", "html")
    assert result["status"] == status
    assert len(calls) == (2 if status == "failed" else 1)
    assert result["listings"] == [{"listing_id": "1", "title": "1"}]


async def test_budget_reports_unfinished_traversal(tmp_path, monkeypatch):
    async with Runtime(tmp_path) as web:
        async def extract(*args): return page(["1"], "https://example.com/p2")
        monkeypatch.setattr(web, "extract", extract)
        result = await web.paginate("https://example.com/p1", "html", WebPolicy(max_pages=1))
    assert result["status"] == "page_limit_reached" and result["next_url"] == "https://example.com/p2"


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_invalid_page_budget(value):
    with pytest.raises(ValueError): WebPolicy(max_pages=value)


async def test_repeated_page_is_reported_without_claiming_progress(tmp_path, monkeypatch):
    responses = [page(["1", "2"], "https://example.com/p2"), page(["1", "2"], "https://example.com/p3")]
    async with Runtime(tmp_path) as web:
        async def extract(*args): return responses.pop(0)
        monkeypatch.setattr(web, "extract", extract)
        result = await web.paginate("https://example.com/p1", "html", WebPolicy(max_pages=2))
    assert not result["page_progress"][1]["made_progress"]
    assert result["page_progress"][1]["new_count"] == 0
    assert result["next_url"] == "https://example.com/p3"
    assert result["status"] == "page_limit_reached" and result["catalogue_complete"] is None


async def test_overlap_evidence_preserves_claims_and_unknowns(tmp_path, monkeypatch):
    responses = [page(["1", "2"], "https://example.com/p2"), page(["1", "2"], "https://example.com/p3"), page(["1"])]
    responses[0]["structured"]["listings"][0]["promotion_claim"] = "sponsored"
    responses[1]["structured"]["listings"][0]["result_type_claim"] = "organic"
    async with Runtime(tmp_path) as web:
        calls = []
        async def extract(*args):
            calls.append(args)
            return responses[len(calls)-1]
        monkeypatch.setattr(web, "extract", extract)
        result = await web.paginate("https://example.com/p1", "html", WebPolicy(max_pages=3))
    assert result["pages"] == responses
    assert result["overlap_count"] == len(result["overlaps"]) == 3
    first, unknown, third = result["overlaps"]
    assert first["first_promotion_claim"] == "sponsored"
    assert first["repeated_promotion_claim"] is None
    assert first["repeated_result_type_claim"] == "organic"
    assert unknown["first_promotion_claim"] is None
    assert unknown["repeated_result_type_claim"] is None
    assert third["first_page_number"] == 1 and third["repeated_page_number"] == 3
    assert third["first_url"] == "https://example.com/p1"
    assert third["repeated_url"] == "https://example.com/p3"
    assert result["listings"][0]["promotion_claim"] == "sponsored"
    assert "result_type_claim" not in result["listings"][0]
