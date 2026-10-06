from frankensurf import provider_worker


def fresh():
    return {"json_items": [], "json_skipped": 0}


def test_worker_keeps_json_and_ndjson_and_counts_rejects():
    capture, request = fresh(), {"capture_json_max_items": 3, "capture_json_max_bytes": 100}
    provider_worker._capture_json(capture, request, "https://a.example/api", 200, "application/json", b'{"x": 1}')
    provider_worker._capture_json(capture, request, "https://a.example/s", 200, "application/json", b'{"a":1}\n{"b":2}')
    provider_worker._capture_json(capture, request, "https://a.example/bad", 200, "application/json", b"{oops")
    provider_worker._capture_json(capture, request, "https://a.example/big", 200, "application/json", b"[" + b"1," * 80 + b"1]")
    provider_worker._capture_json(capture, request, "https://u:p@a.example/x", 200, "application/json", b"{}")
    assert [i["format"] for i in capture["json_items"]] == ["json", "ndjson"]
    assert capture["json_skipped"] == 3


def test_worker_respects_item_limit():
    capture, request = fresh(), {"capture_json_max_items": 1, "capture_json_max_bytes": 100}
    for n in range(3):
        provider_worker._capture_json(capture, request, f"https://a.example/{n}", 200, "application/json", b"{}")
    assert len(capture["json_items"]) == 1 and capture["json_skipped"] == 2
