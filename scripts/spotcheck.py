"""Spot check: the stitched router on a handful of URLs, graded like the tool benchmark.

  PYTHONPATH=.:src python scripts/spotcheck.py --state /tmp/spot cases.json [--free]

cases.json is a list of {"site", "url", "expect"} (a search page passes with
800+ characters and the expect pattern at least 3 times). For quick checks
between full benchmark runs; it proves little on its own.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import toolbench  # noqa: E402


async def main(cases, state, paid, workers):
    from frankensurf import Runtime
    rows = []
    semaphore = asyncio.Semaphore(workers)
    async with Runtime(state_dir=state) as web:
        async def one(case):
            async with semaphore:
                case = {"set": "spot", "kind": "article", "expect_min": 3, **case}
                started = time.monotonic()
                result = await web.read(case["url"], policy_overrides={
                    "freshness": "now", "origin_cooldown_seconds": 0, "allow_paid_fallbacks": paid})
                receipt = result.get("receipt") or {}
                ok, reason = (toolbench.check(case, result) if receipt.get("status") == "observed"
                              else (False, (receipt.get("failure") or {}).get("code")))
                row = {"site": case["site"], "valid": ok, "why": reason, "provider": receipt.get("method"),
                       "seconds": round(time.monotonic() - started, 1),
                       "steps": [s["provider"] for s in (receipt.get("completeness") or {}).get("escalations") or ()]}
                rows.append(row)
                print(json.dumps(row), flush=True)
        await asyncio.gather(*(one(case) for case in cases))
    valid = sum(r["valid"] for r in rows)
    print(json.dumps({"valid": valid, "cases": len(rows), "rate": round(valid / len(rows), 3)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("cases", type=Path)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--free", action="store_true", help="free tools only")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    asyncio.run(main(json.loads(args.cases.read_text()), args.state, not args.free, args.workers))
