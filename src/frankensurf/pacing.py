"""One site's turn, shared by every read on this machine.

Reads to one origin (per identity) take turns: one at a time, and the next
starts origin_min_interval_seconds after the last one finished. The turn lives
in the state directory's origin-pacing.json, updated under a file lock, so a
batch, two batches in one process and separate ``frankensurf read`` processes
all queue behind each other instead of reaching a site at once.

A turn is a lease: it names the holder's process and expires, so a read that
crashed never blocks the site for long.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path

try:
    import fcntl
except ImportError:  # Not POSIX: the in-process lock still orders this process's reads.
    fcntl = None

FILE = "origin-pacing.json"
LOCK = ".origin-pacing.lock"
# A turn outlives any one read (a handoff waits up to five minutes) but not a crash.
LEASE_SECONDS = 900.0
POLL_SECONDS = 0.1

_THREAD_LOCK = threading.Lock()
# The turns this task already holds, so a read inside a read never waits on itself.
_HELD: contextvars.ContextVar[frozenset] = contextvars.ContextVar("frankensurf_pacing_held", default=frozenset())


def load(state_dir: Path) -> dict:
    try:
        data = json.loads((Path(state_dir) / FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _live(entry: dict, now: float) -> bool:
    turn = entry.get("turn") if isinstance(entry.get("turn"), dict) else {}
    return max(entry.get("next_at", 0), entry.get("cooldown_until", 0), turn.get("until", 0)) > now


def _write(state_dir: Path, data: dict) -> None:
    now = time.time()
    data = {key: entry for key, entry in data.items() if isinstance(entry, dict) and _live(entry, now)}
    state_dir.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".origin-pacing-", dir=state_dir)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(data, stream, sort_keys=True)
    os.replace(temporary, state_dir / FILE)


@contextlib.contextmanager
def _locked(state_dir: Path):
    """Exclusive access to the pacing file for one short read-modify-write."""
    with _THREAD_LOCK:
        if fcntl is None:
            yield
            return
        state_dir.mkdir(parents=True, exist_ok=True)
        with open(state_dir / LOCK, "a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def update(state_dir: str | Path, key: str, change) -> None:
    """Apply change(entry) to one origin's entry under the lock. Never raises OSError."""
    state_dir = Path(state_dir)
    try:
        with _locked(state_dir):
            data = load(state_dir)
            entry = dict(data.get(key) or {})
            change(entry)
            data[key] = entry
            _write(state_dir, data)
    except OSError:
        pass


def _holder_alive(turn: dict, now: float) -> bool:
    if not isinstance(turn, dict) or turn.get("until", 0) <= now:
        return False
    pid = turn.get("pid")
    if not isinstance(pid, int) or pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def wait_seconds(entry: dict, interval: float, now: float) -> float:
    """How long until this origin is free: its turn is released and the gap has passed."""
    if _holder_alive(entry.get("turn"), now):
        return POLL_SECONDS
    return max(0.0, min(entry.get("next_at", 0) - now, interval))


async def ready(state_dir: str | Path, key: str, interval: float) -> None:
    """Wait until the origin looks free, without taking its turn (a batch's queue)."""
    if key in _HELD.get():
        return
    while True:
        delay = wait_seconds(load(Path(state_dir)).get(key) or {}, interval, time.time())
        if delay <= 0:
            return
        await asyncio.sleep(min(delay, max(POLL_SECONDS, interval)))


@contextlib.asynccontextmanager
async def turn(state_dir: str | Path, key: str | None, interval: float):
    """Hold this origin's turn for one read; the next read starts interval after it ends."""
    if key is None or interval <= 0 or key in _HELD.get():
        yield
        return
    state_dir = Path(state_dir)
    token = uuid.uuid4().hex
    while True:
        taken = {}

        def take(entry):
            now = time.time()
            delay = wait_seconds(entry, interval, now)
            if delay <= 0:
                entry["turn"] = {"token": token, "pid": os.getpid(), "until": now + LEASE_SECONDS}
            taken["delay"] = delay

        update(state_dir, key, take)
        if taken.get("delay", 0) <= 0:
            break
        await asyncio.sleep(min(taken["delay"], max(POLL_SECONDS, interval)))
    held = _HELD.set(_HELD.get() | {key})
    try:
        yield
    finally:
        _HELD.reset(held)

        def release(entry):
            if (entry.get("turn") or {}).get("token") == token:
                entry.pop("turn", None)
            entry["next_at"] = max(entry.get("next_at", 0), time.time() + interval)

        update(state_dir, key, release)
