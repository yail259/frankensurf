"""Landing page scoreboard: measure what the landing page promises, one row per wall type.

Reads each corpus URL once through Runtime.read with default policy (polite
pacing on), records one JSON line per case under the evals directory, and prints
the summary: completion and failure rate per wall, p50 latency, tokens per
read and cost. The gap between models needs agent runs and stays "unmeasured"
until that harness exists. Unknown stays unknown.

  PYTHONPATH=.:src python scripts/scoreboard.py [--corpus scripts/scoreboard-corpus.json]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

EVALS = Path.home() / ".local/share/frankensurf/evals/scoreboard"
THIN_CHARS = 1000


def summarize(rows: list[dict]) -> dict:
    """Group per-case rows by wall and compute the landing page metrics."""
    walls: dict[str, list[dict]] = {}
    for row in rows:
        walls.setdefault(row["wall"], []).append(row)
    summary = {}
    for wall, group in sorted(walls.items()):
        ok = [r for r in group if r["status"] == "observed"]
        # A thin page (app shell, interstitial) is observed but rarely useful.
        thin = [r for r in ok if r.get("text_chars") is not None and r["text_chars"] < THIN_CHARS
                and not any(kind in (r.get("content_type") or "") for kind in ("json", "pdf"))]
        latencies = [r["latency_ms"] for r in ok if r.get("latency_ms") is not None]
        tokens = [r["tokens"] for r in ok if r.get("tokens") is not None]
        costs = [r["cost_usd"] for r in group if r.get("cost_usd") is not None]
        summary[wall] = {
            "cases": len(group),
            "completion": round(len(ok) / len(group), 3),
            "failure_rate": round(1 - len(ok) / len(group), 3),
            "failures": sorted({r["failure"] for r in group if r.get("failure")}),
            "thin_observed": len(thin),
            "p50_latency_ms": round(statistics.median(latencies)) if latencies else None,
            "median_tokens": round(statistics.median(tokens)) if tokens else None,
            "cost_usd": round(sum(costs), 4) if costs else None,
            "model_gap": "unmeasured",
        }
    return summary


def _tokens(text: str) -> int | None:
    try:
        import tiktoken
        return len(tiktoken.get_encoding("cl100k_base").encode(text or ""))
    except Exception:
        return None


async def run(corpus: Path, overrides: dict | None = None, state: Path | None = None) -> list[dict]:
    from frankensurf import Runtime
    cases = json.loads(corpus.read_text())["cases"]
    rows = []
    # Each arm should use its own state: one arm's blocks start origin
    # cool-downs and route hints that would skew the next arm.
    async with Runtime(state_dir=state or Path.home() / ".frankensurf") as web:
        for case in cases:
            started = time.monotonic()
            result = await web.read(case["url"], policy_overrides=overrides or None)
            receipt = result.get("receipt") or {}
            rows.append({
                "url": case["url"], "wall": case["wall"], "status": receipt.get("status"),
                "failure": (receipt.get("failure") or {}).get("code"),
                "method": receipt.get("method"),
                "provider": ((receipt.get("attempts") or [{}])[-1]).get("provider"),
                "content_type": result.get("content_type"),
                "latency_ms": receipt.get("latency_ms") or round((time.monotonic() - started) * 1000),
                "tokens": _tokens(result.get("text") or ""),
                "text_chars": len(result.get("text") or ""),
                "source": case.get("source", "general"),
                "escalated_to": (receipt.get("routing") or {}).get("escalated_to"),
                "origin_hint": ((receipt.get("routing") or {}).get("provider_plan") or {}).get("origin_hint"),
                "cost_usd": receipt.get("cost_usd"),
                "observed_at": receipt.get("observed_at"),
            })
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", type=Path, default=Path(__file__).with_name("scoreboard-corpus.json"))
    parser.add_argument("--overrides", type=json.loads, default=None,
                        help='Policy overrides for this arm, e.g. \'{"prefer_markdown": true}\'')
    parser.add_argument("--label", default="default", help="Arm name recorded in the output file")
    parser.add_argument("--state", type=Path, default=None,
                        help="State directory for this arm (default ~/.frankensurf); use a fresh one per arm")
    args = parser.parse_args(argv)
    rows = asyncio.run(run(args.corpus, args.overrides, args.state))
    EVALS.mkdir(parents=True, exist_ok=True)
    out = EVALS / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + args.label + ".jsonl")
    out.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
    print(json.dumps({"rows": str(out), "summary": summarize(rows)}, indent=2))


if __name__ == "__main__":
    main()
