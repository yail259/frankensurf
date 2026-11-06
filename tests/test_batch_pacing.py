"""Batch reads different sites at once and one site in turn, with pacing
shared by every Runtime and process on the machine (pacing.py)."""
import asyncio
import io
import json
import subprocess
import sys
import time
from urllib.parse import urlparse

import httpx
import pytest

from frankensurf import Runtime, WebPolicy, cli, pacing
from frankensurf.runtime import Runtime as RuntimeClass

PAGE = "<html><head><title>t</title></head><body><h1>ok</h1><p>" + "words " * 60 + "</p></body></html>"


@pytest.fixture(autouse=True)
def _pacing_on(monkeypatch):
    monkeypatch.setattr(RuntimeClass, "pacing_enabled", True)


def _site(log, latency=0.1):
    async def handle(request):
        start = time.monotonic()
        await asyncio.sleep(latency)
        log.append((request.url.host, start, time.monotonic()))
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})
    return httpx.MockTransport(handle)


def _per_host(log):
    hosts = {}
    for host, start, end in log:
        hosts.setdefault(host, []).append((start, end))
    return {host: sorted(spans) for host, spans in hosts.items()}


def _gaps(spans):
    return [later[0] - earlier[1] for earlier, later in zip(spans, spans[1:])]


@pytest.mark.asyncio
async def test_batch_reads_sites_at_once_and_one_site_in_turn(tmp_path):
    log = []
    policy = WebPolicy(provider="http", origin_min_interval_seconds=0.3)
    urls = [f"https://a.example.test/{i}" for i in range(3)] + [f"https://s{i}.example.test/" for i in range(6)]
    async with Runtime(tmp_path, transport=_site(log)) as web:
        started = time.monotonic()
        results = await web.batch(urls, policy)
        elapsed = time.monotonic() - started
    assert [r["receipt"]["status"] for r in results] == ["observed"] * len(urls)
    assert [urlparse(r["url"]).path for r in results] == [urlparse(u).path for u in urls]
    spans = _per_host(log)
    assert all(gap >= 0.28 for gap in _gaps(spans["a.example.test"]))
    # Six one-page sites ran together, not one after another (6 x 0.1 s).
    singles = [spans[f"s{i}.example.test"][0] for i in range(6)]
    assert max(start for start, _ in singles) - min(start for start, _ in singles) < 0.3
    # The busy site's queue held no worker: the floor is its own three turns.
    assert elapsed < 3 * 0.1 + 2 * 0.3 + 0.6


@pytest.mark.asyncio
async def test_a_busy_site_does_not_starve_other_sites(tmp_path):
    log, finished = [], {}
    policy = WebPolicy(provider="http", origin_min_interval_seconds=0.3)
    urls = [f"https://busy.example.test/{i}" for i in range(5)] + [f"https://o{i}.example.test/" for i in range(4)]
    async with Runtime(tmp_path, transport=_site(log)) as web:
        started = time.monotonic()
        await web.batch(urls, policy, concurrency=2,
                        on_result=lambda index, result: finished.setdefault(index, time.monotonic() - started))
    # With two workers the four other sites are done long before busy's last turn.
    assert max(finished[i] for i in range(5, 9)) < 1.0
    assert max(finished[i] for i in range(5)) >= 4 * 0.3


@pytest.mark.asyncio
async def test_on_result_streams_each_url_once_and_concurrency_one_serializes(tmp_path):
    log, seen = [], []
    policy = WebPolicy(provider="http", origin_min_interval_seconds=0)
    urls = [f"https://h{i}.example.test/" for i in range(4)]
    async with Runtime(tmp_path, transport=_site(log, latency=0.05)) as web:
        results = await web.batch(urls, policy, concurrency=1,
                                  on_result=lambda index, result: seen.append((index, result["receipt"]["status"])))
        with pytest.raises(ValueError):
            await web.batch(urls, policy, concurrency=0)
    assert sorted(seen) == [(i, "observed") for i in range(4)] and len(results) == 4
    spans = sorted((start, end) for _, start, end in log)
    assert all(later[0] >= earlier[1] for earlier, later in zip(spans, spans[1:]))


@pytest.mark.asyncio
async def test_separate_runtimes_share_one_sites_turn(tmp_path):
    log = []
    policy = WebPolicy(provider="http", origin_min_interval_seconds=0.25)

    async def one(path):
        async with Runtime(tmp_path, transport=_site(log, latency=0.15)) as web:
            return await web.read("https://shop.example.test/" + path, policy)

    results = await asyncio.gather(*(one(str(i)) for i in range(3)))
    assert [r["receipt"]["status"] for r in results] == ["observed"] * 3
    gaps = _gaps(_per_host(log)["shop.example.test"])
    assert len(gaps) == 2 and all(gap >= 0.23 for gap in gaps)


def test_a_dead_holders_turn_is_not_waited_for(tmp_path):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    key = "https://shop.example.test|public"
    pacing.update(tmp_path, key, lambda entry: entry.update(
        turn={"token": "x", "pid": dead.pid, "until": time.time() + 600}))

    async def take():
        started = time.monotonic()
        async with pacing.turn(tmp_path, key, 2.0):
            held = pacing.load(tmp_path)[key]["turn"]
        return time.monotonic() - started, held

    elapsed, held = asyncio.run(take())
    assert elapsed < 0.5 and held["pid"] != dead.pid
    after = pacing.load(tmp_path)[key]
    assert "turn" not in after and after["next_at"] > time.time() + 1.5


def test_batch_urls_from_file_and_stdin(tmp_path, monkeypatch):
    listing = tmp_path / "urls.txt"
    listing.write_text("# shops\nhttps://a.example.test/1\n\n  https://b.example.test/2  \n", encoding="utf-8")
    args = cli.parse_args(["batch", "https://c.example.test/", "--urls-file", str(listing)])
    assert args.urls == ["https://c.example.test/", "https://a.example.test/1", "https://b.example.test/2"]
    monkeypatch.setattr(sys, "stdin", io.StringIO("https://d.example.test/\n"))
    assert cli.parse_args(["batch"]).urls == ["https://d.example.test/"]
    monkeypatch.setattr(sys, "stdin", io.StringIO("https://e.example.test/\n"))
    assert cli.parse_args(["batch", "--urls-file", "-", "--concurrency", "3"]).urls == ["https://e.example.test/"]
    with pytest.raises(SystemExit):
        cli.parse_args(["read", "https://a.example.test/", "--jsonl"])
    with pytest.raises(SystemExit):
        cli.parse_args(["batch", "https://a.example.test/", "--concurrency", "0"])


@pytest.mark.asyncio
async def test_batch_jsonl_prints_a_line_per_url_as_it_finishes(tmp_path, monkeypatch, capsys):
    log = []
    real = RuntimeClass

    def runtime(**options):
        return real(options["state_dir"], transport=_site(log, latency=0.05))
    monkeypatch.setattr(cli, "Runtime", runtime)
    args = cli.parse_args(["batch", "https://a.example.test/x", "https://b.example.test/y",
                           "--provider", "http", "--state", str(tmp_path), "--jsonl", "--items"])
    await cli.run(args)
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert sorted(line["url"] for line in lines) == ["https://a.example.test/x", "https://b.example.test/y"]
    for line in lines:
        assert line["status"] == "observed" and line["trace_id"]
        assert {"index", "grade", "failure", "items", "items_quality"} <= set(line)
