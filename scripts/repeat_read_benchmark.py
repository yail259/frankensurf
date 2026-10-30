"""Measure first, fresh-repeat and cached reads on the same public sites.

No reads occur until this script is explicitly run. Results depend on the
runtime, network and sites at the time; a cached request is only a cache hit
when its receipt says so. This free-only harness is not a paid-provider test.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import tempfile
import time

import toolbench


async def measure_case(web, case, gap, sleep=asyncio.sleep, emit=None):
    rows = []
    for index, (phase, freshness) in enumerate((("first", "now"), ("fresh_repeat", "now"),
                                                 ("cached_repeat", "cached"))):
        if index:
            await sleep(gap)
        started = time.monotonic()
        try:
            result = await web.read(case["url"], policy_overrides={"freshness": freshness,
                                                                   "allow_paid_fallbacks": False})
        except Exception as error:
            result = {"receipt": {"status": "failed", "failure": {"code": type(error).__name__}}}
        elapsed_ms = round((time.monotonic() - started) * 1000)
        receipt = result.get("receipt") or {}
        observed = receipt.get("status") == "observed"
        valid, reason = toolbench.check(case, result) if observed else (False, None)
        row = {"site": case["site"], "url": case["url"], "phase": phase,
               "requested_freshness": freshness, "latency_ms": elapsed_ms,
               "status": receipt.get("status"), "valid": valid, "invalid_reason": reason,
               "cache_hit": receipt.get("cache_hit"),
               "failure": (receipt.get("failure") or {}).get("code")}
        rows.append(row)
        if emit is not None:
            emit(row)
    return rows


async def run(cases, output, gap, where):
    from frankensurf import Runtime
    def emit(row):
        output.write(json.dumps({**row, "where": where}, sort_keys=True) + "\n")
        output.flush()
    for case in cases:
        if case.get("identity"):
            raise ValueError("repeat benchmark accepts public cases only")
        # Each site gets its own fresh state, avoiding cross-site cache and
        # route-memory carryover. Results within the site share that state.
        with tempfile.TemporaryDirectory() as state:
            async with Runtime(state_dir=Path(state)) as web:
                await measure_case(web, case, gap, emit=emit)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=int, required=True, help="number of held-out public sites")
    parser.add_argument("--output", type=Path, required=True, help="new output JSONL file")
    parser.add_argument("--where", required=True, help="describe the execution environment")
    parser.add_argument("--gap", type=float, default=2.0, help="seconds between reads of the same site")
    args = parser.parse_args()
    cases = [case for case in toolbench.heldout() if case["set"] == "heldout7"]
    if args.sample < 1 or args.sample > len(cases) or not 0 <= args.gap < float("inf"):
        parser.error("sample must fit the held-out set and gap must be nonnegative and finite")
    step = len(cases) / args.sample
    cases = [cases[int(index * step)] for index in range(args.sample)]
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        asyncio.run(run(cases, output, args.gap, args.where))


if __name__ == "__main__":
    main()
