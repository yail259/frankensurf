#!/usr/bin/env python3
"""Run a loopback JS acquisition and retain shared-cache before/after hashes."""
from __future__ import annotations

import argparse
import asyncio
from functools import partial
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone


REPOSITORY = Path(__file__).resolve().parents[1]


def _source_snapshot(repository=REPOSITORY):
    """Capture the exact Git source state at one canary boundary."""
    def git(*arguments, strip=True):
        output = subprocess.run(
            ["git", *arguments], cwd=repository, check=True,
            capture_output=True, text=True).stdout
        return output.strip() if strip else output

    commit = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD^{tree}")
    status = git(
        "status", "--porcelain=v1", "--untracked-files=all", strip=False)
    repeated = (
        git("rev-parse", "HEAD"),
        git("rev-parse", "HEAD^{tree}"),
        git("status", "--porcelain=v1", "--untracked-files=all",
            strip=False),
    )
    if repeated != (commit, tree, status):
        raise RuntimeError("Git source changed while taking a canary snapshot")
    return {
        "commit": commit,
        "tree": tree,
        "status": status,
        "clean": not status,
    }


def _valid_source_snapshot(snapshot):
    return (
        type(snapshot) is dict
        and set(snapshot) == {"commit", "tree", "status", "clean"}
        and isinstance(snapshot.get("commit"), str)
        and re.fullmatch(r"[0-9a-f]{40,64}", snapshot["commit"]) is not None
        and isinstance(snapshot.get("tree"), str)
        and re.fullmatch(r"[0-9a-f]{40,64}", snapshot["tree"]) is not None
        and isinstance(snapshot.get("status"), str)
        and type(snapshot.get("clean")) is bool
        and snapshot["clean"] == (not snapshot["status"])
    )


def _source_snapshot_report(snapshot):
    status = snapshot.get("status") if isinstance(snapshot, dict) else None
    return {
        "commit": snapshot.get("commit") if isinstance(snapshot, dict) else None,
        "tree": snapshot.get("tree") if isinstance(snapshot, dict) else None,
        "clean": snapshot.get("clean") if isinstance(snapshot, dict) else None,
        "status_sha256": (
            hashlib.sha256(status.encode()).hexdigest()
            if isinstance(status, str) else None),
        "status_entry_count": (
            len(status.splitlines()) if isinstance(status, str) else None),
    }


def _tree_identity(root):
    digest = hashlib.sha256()
    entries = 0
    regular_file_bytes = 0
    if root.exists():
        for directory, names, files in os.walk(root, followlinks=False):
            names.sort()
            files.sort()
            base = Path(directory)
            for name in [*names, *files]:
                path = base / name
                details = path.lstat()
                relative = str(path.relative_to(root))
                if stat.S_ISLNK(details.st_mode):
                    kind = "l"
                    content = os.readlink(path).encode()
                elif stat.S_ISDIR(details.st_mode):
                    kind = "d"
                    content_hash = hashlib.sha256(b"").digest()
                elif stat.S_ISREG(details.st_mode):
                    kind = "f"
                    content_digest = hashlib.sha256()
                    with path.open("rb") as stream:
                        while chunk := stream.read(1024 * 1024):
                            content_digest.update(chunk)
                    content_hash = content_digest.digest()
                    regular_file_bytes += details.st_size
                else:
                    kind = "o"
                    content_hash = hashlib.sha256(b"").digest()
                if kind == "l":
                    content_hash = hashlib.sha256(content).digest()
                digest.update(kind.encode() + b"\0")
                digest.update(relative.encode() + b"\0")
                digest.update(content_hash)
                entries += 1
    return {
        "sha256": digest.hexdigest(),
        "entries": entries,
        "regular_file_bytes": regular_file_bytes,
    }


def _binding_timings(provider_root):
    """Measure a fresh module's full binding validation and cached repeat."""
    config_path = REPOSITORY / "src/frankensurf/crawl4ai_config.py"
    spec = importlib.util.spec_from_file_location(
        "frankensurf_crawl4ai_canary_config", config_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load Crawl4AI identity validator")
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)
    measurements = []
    bindings = []
    for _ in range(2):
        started = time.perf_counter()
        binding, guard = config.runtime_binding_snapshot()
        measurements.append(time.perf_counter() - started)
        bindings.append(binding)
        if binding is None or guard is None:
            raise RuntimeError("Crawl4AI binding validation failed")
    browser_snapshot = config._owned_tree_snapshot(
        provider_root / "browsers", hash_contents=False)
    if browser_snapshot is None:
        raise RuntimeError("Crawl4AI browser-tree validation failed")
    _, browser_paths = browser_snapshot
    return {
        "fresh_module_first_seconds": round(measurements[0], 6),
        "cached_digest_repeat_seconds": round(measurements[1], 6),
        "browser_tree_entries": len(browser_paths),
        "binding_stable": bindings[0] == bindings[1],
        "scope": (
            "Full pinned dependency metadata and complete browser-tree "
            "metadata/content binding; timings are machine-local diagnostics."),
    }


def _evidence_binding(source_before, source_after, observation):
    """Qualify only an unchanged run bounded by two clean source snapshots."""
    provider_binding = observation.get("provider_binding_id")
    executor_binding = observation.get("executor_binding_id")
    source_valid = (_valid_source_snapshot(source_before)
                    and _valid_source_snapshot(source_after))
    source_unchanged = source_valid and source_before == source_after
    valid = (
        source_valid
        and isinstance(provider_binding, str)
        and re.fullmatch(r"[0-9a-f]{64}", provider_binding) is not None
        and isinstance(executor_binding, str)
        and re.fullmatch(r"[0-9a-f]{64}", executor_binding) is not None
    )
    eligible = (valid and source_unchanged
                and source_before["clean"] is True
                and source_after["clean"] is True)
    return {
        "eligible": eligible,
        "source_commit": source_before.get("commit"),
        "source_tree": source_before.get("tree"),
        "source_clean_before": source_before.get("clean"),
        "source_clean_after": source_after.get("clean"),
        "source_state_unchanged": source_unchanged,
        "source_status_before_sha256": _source_snapshot_report(
            source_before)["status_sha256"],
        "source_status_after_sha256": _source_snapshot_report(
            source_after)["status_sha256"],
        "provider_binding_id": provider_binding,
        "executor_binding_id": executor_binding,
        "basis": (
            "Run began and ended at the identical clean source commit, tree "
            "and status and records the Core provider-catalog and isolated "
            "executor bindings."
            if eligible else
            "Dirty, changed or incompletely bound runs are diagnostic only "
            "and cannot qualify current behavior, routing, coverage or "
            "reliability."),
    }


class _Handler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


async def _acquire(url, state):
    from frankensurf import Runtime, WebPolicy
    from frankensurf import crawl4ai_config

    async with Runtime(state) as web:
        result = await web.extract(
            url,
            "html",
            WebPolicy(
                provider="crawl4ai",
                freshness="now",
                timeout_seconds=20,
                wait_selector="#listing[data-ready='true']",
                content_ready_selector="#listing[data-ready='true']",
                content_ready_timeout_seconds=5,
                settle_ms=0,
                max_bytes=1024 * 1024,
            ),
        )
    receipt = result["receipt"]
    assert receipt["status"] == "observed"
    assert receipt["method"] == "crawl4ai"
    assert receipt["cost_usd"] == 0
    assert receipt["evidence"]
    assert result["title"] == "Local dynamic fixture"
    assert "JS rendered listing 842" in result["text"]
    return {
        "status": receipt["status"],
        "method": receipt["method"],
        "provider_version": receipt["provider_version"],
        "provider_binding_id": receipt["provider_binding_id"],
        "executor_binding_id": crawl4ai_config.runtime_binding_id(),
        "cost_usd": receipt["cost_usd"],
        "evidence_count": len(receipt["evidence"]),
        "content_readiness": receipt["content_readiness"],
        "title_assertion": True,
        "rendered_text_assertion": True,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args(argv)
    source_before = _source_snapshot()

    operator_home = Path.home()
    provider_root = operator_home / ".local/share/frankensurf/crawl4ai-venv"
    shared_cache = operator_home / ".cache/ms-playwright"
    os.environ.update({
        "FRANKENSURF_CRAWL4AI_DIR": str(provider_root),
        "FRANKENSURF_CRAWL4AI_PYTHON": str(provider_root / "bin/python"),
        "FRANKENSURF_CRAWL4AI_BROWSERS_PATH": str(
            provider_root / "browsers"),
        "FRANKENSURF_CRAWL4AI_HEALTH": str(
            provider_root / "frankensurf-health.json"),
    })
    binding_timings = _binding_timings(provider_root)
    before = _tree_identity(shared_cache)

    with tempfile.TemporaryDirectory(
            prefix="frankensurf-crawl4ai-canary-") as temporary:
        temporary = Path(temporary)
        caller_home = temporary / "caller-home"
        fixture = temporary / "fixture"
        caller_home.mkdir(mode=0o700)
        fixture.mkdir(mode=0o700)
        (fixture / "index.html").write_text(
            "<!doctype html><html><head><title>Local dynamic fixture</title>"
            "</head><body><h1 id='listing'>Loading</h1>"
            "<p id='description'>Waiting for JavaScript.</p><script>"
            "setTimeout(()=>{const e=document.getElementById('listing');"
            "e.textContent='JS rendered listing 842';"
            "e.dataset.ready='true';"
            "document.getElementById('description').textContent="
            "'Current public fixture content rendered by the isolated worker "
            "with enough exact text for Core evidence validation.';"
            "},150);</script></body></html>",
            encoding="utf-8",
        )
        os.environ["HOME"] = str(caller_home)
        os.environ["OPENAI_API_KEY"] = "canary-must-not-cross"
        sys.path.insert(0, str(REPOSITORY / "src"))
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(_Handler, directory=str(fixture)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/index.html"
            observation = asyncio.run(_acquire(url, temporary / "state"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        caller_shared_cache_created = (
            caller_home / ".cache/ms-playwright").exists()

    after = _tree_identity(shared_cache)
    unchanged = before == after
    if not unchanged or caller_shared_cache_created:
        raise SystemExit("Crawl4AI isolation canary changed a shared cache")
    source_after = _source_snapshot()
    evidence_binding = _evidence_binding(
        source_before, source_after, observation)
    source_state = {
        "before": _source_snapshot_report(source_before),
        "after": _source_snapshot_report(source_after),
        "unchanged": source_before == source_after,
    }
    report = {
        "schema": "frankensurf.crawl4ai-isolation-canary/v2",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "source_commit": source_before["commit"],
        "source_tree": source_before["tree"],
        "source_tree_clean": (
            source_before["clean"] and source_after["clean"]
            and source_before == source_after),
        "source_state": source_state,
        "evidence_binding": evidence_binding,
        "fixture": "loopback delayed JavaScript",
        "observation": observation,
        "runtime_binding_validation": binding_timings,
        "shared_playwright_cache": {
            "path": "~/.cache/ms-playwright",
            "before": before,
            "after": after,
            "unchanged": unchanged,
            "caller_home_cache_created": caller_shared_cache_created,
        },
        "limits": (
            "One local fixture run proves this execution and cache isolation; "
            "it does not prove marketplace coverage or reliability."),
    }
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if arguments.output is None:
        print(encoded, end="")
    else:
        destination = arguments.output.resolve()
        destination.write_text(encoded, encoding="utf-8")
        destination.chmod(0o644)


if __name__ == "__main__":
    main()
