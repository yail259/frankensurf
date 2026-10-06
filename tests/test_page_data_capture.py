import asyncio

import pytest

from frankensurf.runtime import WebPolicy, _JsonCapture, _parse_builtin_content

URL = "https://shop.example.com/item/1"


def test_embedded_next_data_is_returned_as_raw_material():
    html = ('<html><head><title>Item</title></head><body><h1>Item</h1>'
            '<script id="__NEXT_DATA__" type="application/json">{"props":{"price":42}}</script>'
            '<script type="application/json">not json</script></body></html>')
    parsed = _parse_builtin_content(html, "text/html", URL, None)
    assert parsed["structured"]["embedded_json"] == [{"id": "__NEXT_DATA__", "data": {"props": {"price": 42}}}]


def test_capture_keeps_json_and_ndjson_from_xhr_and_fetch_only():
    capture = _JsonCapture(max_items=5, max_bytes=1000)
    assert capture.wants("xhr", "application/json; charset=utf-8")
    assert capture.wants("fetch", "application/x-ndjson")
    assert not capture.wants("document", "application/json")
    assert capture.wants("xhr", "text/html")  # JSON is often mislabelled
    assert not capture.wants("fetch", "image/png")
    capture.add("https://shop.example.com/api/item", 200, "application/json", b'{"price": 42}')
    capture.add("https://shop.example.com/api/stream", 200, "application/json", b'{"a":1}\n{"b":2}\n')
    capture.add("https://shop.example.com/api/bad", 200, "application/json", b"{oops")
    capture.add("https://shop.example.com/api/big", 200, "application/json", b"[" + b"1," * 600 + b"1]")
    result = asyncio.run(capture.settle(0.1))
    assert [item["format"] for item in result["items"]] == ["json", "ndjson"]
    assert result["items"][0]["data"] == {"price": 42}
    assert result["skipped"] == 2


def test_capture_respects_item_limit_and_rejects_credential_urls():
    capture = _JsonCapture(max_items=1, max_bytes=1000)
    capture.add("https://user:pw@shop.example.com/api", 200, "application/json", b"{}")
    capture.add("https://shop.example.com/a", 200, "application/json", b"{}")
    capture.add("https://shop.example.com/b", 200, "application/json", b"{}")
    result = asyncio.run(capture.settle(0.1))
    assert [item["url"] for item in result["items"]] == ["https://shop.example.com/a"]
    assert result["skipped"] == 2


def test_capture_policy_is_opt_in_and_validated():
    assert WebPolicy().capture_json_responses is False
    with pytest.raises(ValueError):
        WebPolicy(capture_json_max_items=0)
    with pytest.raises(ValueError):
        WebPolicy(capture_json_responses="yes")


def test_capture_strips_anti_hijacking_prefixes_from_mislabelled_json():
    capture = _JsonCapture(max_items=5, max_bytes=10000)
    capture.add("https://www.facebook.com/api/graphql/", 200, "text/html; charset=utf-8",
                b'for (;;);{"data":{"marketplace_search":{"feed_units":{"edges":[]}}}}')
    capture.add("https://www.example.com/rpc", 200, "application/javascript", b")]}'\n[1,2,3]")
    capture.add("https://www.facebook.com/api/graphql/", 200, "text/html",
                b'{"data":{"a":1}}\r\n{"label":"stream","data":{"b":2}}')
    capture.add("https://www.example.com/page", 200, "text/html", b"<html>not json</html>")
    result = asyncio.run(capture.settle(0.1))
    assert [item["format"] for item in result["items"]] == ["json", "json", "ndjson"]
    assert result["items"][0]["data"]["data"]["marketplace_search"]
    assert result["items"][1]["data"] == [1, 2, 3]
    assert result["skipped"] == 1
