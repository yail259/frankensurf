"""The weekly benchmark script: which arms run, and how a week is summarised (no reads here)."""
import importlib.util
import asyncio
import io
import json
import sys
from pathlib import Path
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "weekly_bench.py"


def load():
    sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("weekly_bench", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_paid_arms_run_only_with_their_keys(monkeypatch):
    weekly = load()
    for name in ("FIRECRAWL_API_KEY", "ZYTE_API_KEY", "FRANKENSURF_FIRECRAWL_API_KEY", "FRANKENSURF_ZYTE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert weekly.arms() == weekly.FREE
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")
    assert weekly.arms() == [*weekly.FREE, "firecrawl", "stitched_paid"]


def test_a_week_is_summarised_per_arm():
    weekly = load()
    rows = [{"arm": "http", "valid": True, "latency_ms": 1000, "cost_usd": 0},
            {"arm": "http", "valid": False, "latency_ms": 9000, "cost_usd": 0},
            {"arm": "stitched_free", "valid": True, "latency_ms": 3000, "cost_usd": None}]
    out = weekly.summarize(rows)
    assert out["http"] == {"label": "Plain HTTP", "sites": 2, "read": 1, "rate": 0.5, "median_s": 1.0,
                           "p90_s": 1.0, "cost_usd": 0, "cost_unknown_rows": 0}
    assert out["stitched_free"]["label"] == "Frankensurf, free tools only" and out["stitched_free"]["rate"] == 1.0
    assert out["stitched_free"]["cost_usd"] is None
    assert out["stitched_free"]["cost_unknown_rows"] == 1


def test_mixed_known_and_unknown_cost_is_not_reported_as_a_total():
    weekly = load()
    rows = [{"arm": "zyte", "valid": True, "latency_ms": 1000, "cost_usd": 0.01},
            {"arm": "zyte", "valid": False, "latency_ms": 2000, "cost_usd": None}]
    out = weekly.summarize(rows)["zyte"]
    assert out["cost_usd"] is None
    assert out["cost_unknown_rows"] == 1


def test_invalid_costs_are_unknown():
    weekly = load()
    rows = [{"arm": "zyte", "valid": True, "latency_ms": 1000, "cost_usd": value}
            for value in (float("nan"), -1, True, "0.1")]
    out = weekly.summarize(rows)["zyte"]
    assert out["cost_usd"] is None
    assert out["cost_unknown_rows"] == 4


def test_benchmark_state_can_be_retained_without_reusing_old_state(tmp_path):
    weekly = load()
    retained = tmp_path / "new-run"
    with weekly.benchmark_state(retained) as state:
        (state / "evidence.txt").write_text("observed")
    assert (retained / "evidence.txt").read_text() == "observed"
    with pytest.raises(FileExistsError):
        with weekly.benchmark_state(retained):
            pass
    with weekly.benchmark_state() as ephemeral:
        assert ephemeral.is_dir()
    assert not ephemeral.exists()


def test_opt_in_evidence_can_replay_content_check_without_provider_receipt():
    weekly = load()
    toolbench = weekly.toolbench
    text = "observed page text " * 50
    class Web:
        async def read(self, url, policy_overrides):
            return {"url": url, "title": "Page", "text": text,
                    "receipt": {"status": "observed", "secret": "do-not-store"}}
    case = {"site": "test", "url": "https://example.org/", "kind": "article", "set": "heldout7", "wall": "none",
            "expect": "observed", "expect_min": 3}
    stream = io.StringIO()
    row = asyncio.run(toolbench.read(Web(), "http", case, evidence=stream))
    evidence = json.loads(stream.getvalue())
    assert row["valid"] and evidence["valid"]
    assert evidence["text"] == text
    assert evidence["url"] == case["url"]
    assert toolbench.check(evidence["case"], {"url": evidence["final_url"], "text": evidence["text"]}) == (True, None)
    assert "do-not-store" not in stream.getvalue()


def test_recheck_detects_missing_and_changed_evidence(tmp_path):
    load()  # makes scripts available for the verifier's toolbench import
    spec = importlib.util.spec_from_file_location("recheck_weekly_evidence", SCRIPT.with_name("recheck_weekly_evidence.py"))
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    case = {"site": "test", "url": "https://example.org/", "kind": "article", "expect": "observed", "expect_min": 3}
    row = {"arm": "http", "site": "test", "url": case["url"], "status": "observed", "valid": True, "invalid_reason": None}
    saved = {**row, "case": case, "text": "observed " * 100, "final_url": case["url"]}
    rows, evidence = tmp_path / "rows.jsonl", tmp_path / "evidence.jsonl"
    rows.write_text(json.dumps(row) + "\n")
    evidence.write_text(json.dumps(saved) + "\n")
    assert verifier.recheck(rows, evidence)["mismatched"] == 0
    saved["text"] = "blocked"
    evidence.write_text(json.dumps(saved) + "\n")
    assert verifier.recheck(rows, evidence)["mismatched"] == 1
    evidence.write_text("")
    assert verifier.recheck(rows, evidence)["missing_evidence"] == 1
    evidence.write_text(json.dumps(saved) + "\n" + json.dumps(saved) + "\n")
    with pytest.raises(ValueError, match="duplicate"):
        verifier.recheck(rows, evidence)


def test_repeat_read_distinguishes_requested_freshness_from_cache_hit():
    load()
    spec = importlib.util.spec_from_file_location("repeat_read_benchmark", SCRIPT.with_name("repeat_read_benchmark.py"))
    repeat = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(repeat)
    calls, emitted = [], []
    class Web:
        async def read(self, url, policy_overrides):
            calls.append(policy_overrides)
            # A request for cached data may still miss and perform a fresh read.
            return {"url": url, "text": "observed " * 100,
                    "receipt": {"status": "observed", "cache_hit": False}}
    async def no_wait(gap):
        pass
    case = {"site": "test", "url": "https://example.org/", "kind": "article", "expect": "observed", "expect_min": 3}
    rows = asyncio.run(repeat.measure_case(Web(), case, 2, sleep=no_wait, emit=emitted.append))
    assert [row["phase"] for row in rows] == ["first", "fresh_repeat", "cached_repeat"]
    assert [call["freshness"] for call in calls] == ["now", "now", "cached"]
    assert all(call["allow_paid_fallbacks"] is False for call in calls)
    assert all(row["valid"] for row in rows)
    assert rows[-1]["cache_hit"] is False
    assert emitted == rows


def test_repeat_read_records_failures_without_exception_details():
    load()
    spec = importlib.util.spec_from_file_location("repeat_read_benchmark", SCRIPT.with_name("repeat_read_benchmark.py"))
    repeat = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(repeat)
    class Web:
        async def read(self, url, policy_overrides):
            raise RuntimeError("private error detail")
    async def no_wait(gap):
        pass
    case = {"site": "test", "url": "https://example.org/", "kind": "article"}
    rows = asyncio.run(repeat.measure_case(Web(), case, 0, sleep=no_wait))
    assert len(rows) == 3 and all(row["failure"] == "RuntimeError" for row in rows)
    assert "private error detail" not in json.dumps(rows)
