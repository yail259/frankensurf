"""Recheck benchmark rows against retained public observations without network access."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

import toolbench


def verify(rows, directory):
    seen = set()
    counts = {"observed_valid": 0, "observed_invalid": 0, "not_observed": 0}
    for row in rows:
        ref = row.get("evidence")
        if not isinstance(ref, dict) or not re.fullmatch(r"[0-9a-f]{64}\.json", ref.get("file", "")):
            raise ValueError("Missing or invalid evidence reference")
        if ref["file"] in seen:
            raise ValueError("Duplicate evidence reference")
        seen.add(ref["file"])
        path = directory / ref["file"]
        if path.is_symlink(): raise ValueError("Evidence cannot be a symlink")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != ref.get("sha256"):
            raise ValueError("Evidence digest does not match the row")
        payload = json.loads(data)
        case, observation = payload.get("case"), payload.get("observation")
        if payload.get("schema") != "frankensurf.benchmark-evidence/v1" or not isinstance(case, dict) or not isinstance(observation, dict):
            raise ValueError("Invalid evidence format")
        if case.get("identity") or payload.get("arm") != row.get("arm") or any(case.get(k) != row.get(k) for k in ("site", "set", "url")):
            raise ValueError("Evidence does not match the public benchmark row")
        status = (observation.get("receipt") or {}).get("status")
        if status != row.get("status"):
            raise ValueError("Evidence status does not match the row")
        if status == "observed":
            valid, reason = toolbench.check(case, observation)
            if valid != row.get("valid") or reason != row.get("invalid_reason"):
                raise ValueError("Retained content does not reproduce the benchmark check")
            counts["observed_valid" if valid else "observed_invalid"] += 1
        else:
            if row.get("valid") is not False or row.get("invalid_reason") is not None:
                raise ValueError("Unobserved row has an inconsistent result")
            counts["not_observed"] += 1
    if not seen:
        raise ValueError("No rows to verify")
    return {"checked": len(seen), **counts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rows", type=Path)
    parser.add_argument("evidence_dir", type=Path)
    args = parser.parse_args()
    try:
        rows = [json.loads(line) for line in args.rows.read_text(encoding="utf-8").splitlines() if line.strip()]
        print(json.dumps(verify(rows, args.evidence_dir), sort_keys=True))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        print("Cannot verify evidence:", error, file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
