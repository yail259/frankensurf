"""Opt-in, local-only evidence for public benchmark observations.

Artifacts contain public page content and URLs. Do not use with identities or
commit the resulting files. An operator chooses a new private directory per run.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat


class EvidenceSink:
    def __init__(self, directory: Path, max_bytes: int):
        if not hasattr(os, "geteuid") or not hasattr(os, "O_NOFOLLOW"):
            raise ValueError("Evidence capture requires POSIX ownership and no-follow support")
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("Evidence directory must be an existing non-symlink directory")
        if directory.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
            raise ValueError("Evidence directory must be outside the source checkout")
        info = directory.stat()
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("Evidence directory must be owner-only and owned by this user")
        if max_bytes < 1:
            raise ValueError("Evidence byte limit must be positive")
        self.directory, self.max_bytes = directory, max_bytes

    @classmethod
    def from_environment(cls, case, arm):
        path = os.environ.get("TOOLBENCH_EVIDENCE_DIR")
        if not path:
            return None
        if case.get("identity"):
            raise ValueError("Identity-backed cases cannot use public evidence capture")
        maximum = int(os.environ.get("TOOLBENCH_EVIDENCE_MAX_BYTES", "16777216"))
        sink = cls(Path(path), maximum)
        sink.filename = hashlib.sha256(json.dumps([case.get("set"), case.get("site"), case.get("url"), arm],
                                                  separators=(",", ":"), ensure_ascii=False).encode()).hexdigest() + ".json"
        if os.path.lexists(sink.directory / sink.filename):
            raise FileExistsError("Evidence for this case and arm already exists; choose a new run directory")
        return sink

    def save(self, case, arm, result):
        receipt = result.get("receipt") or {}
        payload = {"schema": "frankensurf.benchmark-evidence/v1", "case": case, "arm": arm,
                   "observation": {"url": result.get("url"), "text": result.get("text"),
                                   "content": result.get("content"), "content_type": result.get("content_type"),
                                   "structured": result.get("structured"),
                                   "receipt": {"status": receipt.get("status"), "trace_id": receipt.get("trace_id")}}}
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        if len(data) > self.max_bytes:
            raise ValueError("Evidence exceeds the configured byte limit; no truncated artifact was written")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.directory / self.filename, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
        except BaseException:
            (self.directory / self.filename).unlink(missing_ok=True)
            raise
        return {"file": self.filename, "sha256": hashlib.sha256(data).hexdigest()}
