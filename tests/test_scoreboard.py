import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "scoreboard", Path(__file__).resolve().parents[1] / "scripts" / "scoreboard.py")
scoreboard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scoreboard)


def test_summary_reports_completion_failures_latency_and_unmeasured_model_gap():
    rows = [
        {"wall": "static", "status": "observed", "latency_ms": 100, "tokens": 50, "cost_usd": 0.0},
        {"wall": "static", "status": "observed", "latency_ms": 300, "tokens": 70, "cost_usd": 0.0},
        {"wall": "peakhour", "status": "failed", "failure": "BLOCKED", "latency_ms": 40, "cost_usd": None},
        {"wall": "peakhour", "status": "observed", "latency_ms": 2400, "tokens": 900, "cost_usd": 0.0},
    ]
    summary = scoreboard.summarize(rows)
    assert summary["static"]["completion"] == 1.0 and summary["static"]["p50_latency_ms"] == 200
    assert summary["peakhour"]["failure_rate"] == 0.5 and summary["peakhour"]["failures"] == ["BLOCKED"]
    assert summary["peakhour"]["median_tokens"] == 900
    assert all(row["model_gap"] == "unmeasured" for row in summary.values())


def test_corpus_declares_a_wall_for_every_case():
    import json
    corpus = json.loads((Path(__file__).resolve().parents[1] / "scripts" / "scoreboard-corpus.json").read_text())
    assert corpus["cases"] and all(case["url"].startswith("https://") and case["wall"] for case in corpus["cases"])


def test_thin_observed_pages_are_counted_separately():
    rows = [
        {"wall": "js_app", "status": "observed", "latency_ms": 100, "text_chars": 300},
        {"wall": "js_app", "status": "observed", "latency_ms": 100, "text_chars": 5000},
    ]
    summary = scoreboard.summarize(rows)
    assert summary["js_app"]["completion"] == 1.0 and summary["js_app"]["thin_observed"] == 1
