"""Recheck retained weekly page text against its original benchmark rows.

This reads local files only. It does not prove provenance, freshness, or a
site's current behavior. Evidence can contain sensitive page text and URLs.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))
import toolbench  # noqa: E402


def _read_unique(path: Path) -> dict:
    records = {}
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict) or not isinstance(record.get("arm"), str) or not isinstance(record.get("url"), str):
                raise ValueError(f"invalid record at {path.name}:{number}")
            key = (record["arm"], record["url"])
            if key in records:
                raise ValueError(f"duplicate arm and URL at {path.name}:{number}")
            records[key] = record
    return records


def recheck(rows_path: Path, evidence_path: Path) -> dict:
    rows, evidence = _read_unique(rows_path), _read_unique(evidence_path)
    missing = len(rows.keys() - evidence.keys())
    extra = len(evidence.keys() - rows.keys())
    checked = mismatched = unsupported = 0
    for key in rows.keys() & evidence.keys():
        row, saved = rows[key], evidence[key]
        case = saved.get("case")
        if (not isinstance(case, dict) or case.get("url") != key[1] or case.get("identity")
                or case.get("site") != row.get("site") or saved.get("site") != row.get("site")):
            raise ValueError("invalid or non-public saved case")
        if case.get("kind") != "article":
            # Other kinds may depend on links or structured data, neither of
            # which is retained by this evidence format.
            unsupported += 1
            continue
        status = saved.get("status")
        result = {"url": saved.get("final_url"), "title": saved.get("title"), "text": saved.get("text")}
        valid, reason = toolbench.check(case, result) if status == "observed" else (False, None)
        checked += 1
        if ((row.get("status"), row.get("valid"), row.get("invalid_reason")) != (status, valid, reason)
                or (saved.get("valid"), saved.get("invalid_reason")) != (valid, reason)):
            mismatched += 1
    return {"row_count": len(rows), "evidence_count": len(evidence), "checked": checked,
            "missing_evidence": missing, "extra_evidence": extra,
            "unsupported_cases": unsupported, "mismatched": mismatched}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rows", type=Path)
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    try:
        result = recheck(args.rows, args.evidence)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.exit(2, f"Cannot recheck evidence: {error}\n")
    print(json.dumps(result, indent=2))
    return 1 if any(result[key] for key in ("missing_evidence", "extra_evidence", "unsupported_cases", "mismatched")) else 0


if __name__ == "__main__":
    raise SystemExit(main())
