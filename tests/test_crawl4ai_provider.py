import json
import hashlib
import os
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading

import pytest

from frankensurf import Runtime, WebPolicy
from frankensurf import crawl4ai_config as config
import frankensurf.crawl4ai_provider as crawl4ai_provider
import frankensurf.providers as providers
from frankensurf.crawl4ai_provider import (
    Crawl4AIProvider, _worker_environment)
from frankensurf.providers import (
    ProviderRegistry, ProviderRequest, ProviderServices)
from frankensurf.runtime import WebFailure


def _configure(monkeypatch, root, python, browsers, health):
    monkeypatch.setenv("FRANKENSURF_CRAWL4AI_DIR", str(root))
    monkeypatch.setenv("FRANKENSURF_CRAWL4AI_PYTHON", str(python))
    monkeypatch.setenv("FRANKENSURF_CRAWL4AI_BROWSERS_PATH", str(browsers))
    monkeypatch.setenv("FRANKENSURF_CRAWL4AI_HEALTH", str(health))


def _fake_installation_identity(monkeypatch, dependency=None):
    def identity(root, python, browsers, browser):
        sites = list((Path(root) / "lib").glob("python*/site-packages"))
        if (not sites or any(not config._owned_directory_beneath(root, site)
                             for site in sites)):
            return None
        installed = (hashlib.sha256(dependency.read_bytes()).hexdigest()
                     if dependency is not None else "d" * 64)
        return {
            "worker_sha256": config._sha256_regular_file(
                Path(config.__file__).with_name("crawl4ai_worker.py")),
            "pyvenv_cfg_sha256": config._sha256_regular_file(
                Path(root) / "pyvenv.cfg"),
            "installed_distributions_sha256": installed,
            "python_executable_sha256": config._sha256_regular_file(
                Path(python).resolve(strict=True)),
            "browser_executable_sha256": config._sha256_regular_file(
                Path(browser)),
            "browser_tree_sha256": config._owned_tree_identity(
                Path(browsers)),
        }
    monkeypatch.setattr(config, "_installation_identity", identity)
    return identity


def _health_fixture(monkeypatch, tmp_path):
    root = tmp_path / "provider"
    python = root / "bin/python"
    browsers = root / "browsers"
    browser = browsers / "chromium-1/chrome"
    browser_asset = browsers / "chromium-1/resources/runtime.pak"
    health = root / "frankensurf-health.json"
    site = root / "lib/python3.14/site-packages"
    python.parent.mkdir(parents=True)
    browser.parent.mkdir(parents=True)
    browser_asset.parent.mkdir(parents=True)
    site.mkdir(parents=True)
    (root / "pyvenv.cfg").write_text("fixture = true\n")
    python.write_text("#!/bin/sh\n")
    browser.write_text("#!/bin/sh\n")
    browser_asset.write_bytes(b"fixture-browser-asset-v1\n")
    python.chmod(0o700)
    browser.chmod(0o700)
    root.chmod(0o700)
    browsers.chmod(0o700)
    identity = _fake_installation_identity(monkeypatch)
    payload = {
        "schema": config.HEALTH_SCHEMA,
        "status": "ok",
        "sdk_version": config.SDK_VERSION,
        "python": str(python.resolve()),
        "browsers_path": str(browsers.resolve()),
        "browser_executable": str(browser.resolve()),
        "requirements_sha256": config.LOCK_SHA256,
        **identity(root, python, browsers, browser),
    }
    health.write_text(json.dumps(payload))
    health.chmod(0o600)
    root.chmod(0o700)
    _configure(monkeypatch, root, python, browsers, health)
    return root, python, browsers, health, browser, payload


def test_manifest_and_catalog_provenance_are_exact(tmp_path):
    provider = Crawl4AIProvider()
    assert provider.manifest == provider.manifest.__class__(
        "crawl4ai", "0.9.4", rendering=True, requires_local_browser=True,
        paid=False, authentication=False, navigation=False,
        cost_bounded=True, operations=("read", "extract"))
    runtime = Runtime(tmp_path / "state")
    entry = next(item for item in runtime.inspect_plugins()["plugins"]
                 if item["kind"] == "provider"
                 and item["id"] == "crawl4ai")
    assert entry["source"] == entry["trust_basis"] == "bundled"
    assert entry["manifest"]["operations"] == ("read", "extract")
    assert entry["manifest"]["authentication"] is False
    assert entry["manifest"]["paid"] is False
    assert len(entry["binding_id"]) == 64


def test_provider_reuses_startup_binding_with_cheap_guard(monkeypatch):
    monkeypatch.setattr(
        config, "runtime_binding_snapshot", lambda: ("a" * 64, ("guard",)))
    provider = Crawl4AIProvider()
    monkeypatch.setattr(
        config, "runtime_binding_snapshot",
        lambda: pytest.fail("availability must not repeat the full scan"))
    monkeypatch.setattr(
        config, "runtime_guard_matches",
        lambda guard: guard == ("guard",))
    assert provider.available(configured=()) is True


def test_crawl4ai_does_not_extend_core_policy():
    for field in (
            "crawl4ai_packet_max_bytes",
            "crawl4ai_readiness_poll_ms",
            "crawl4ai_enable_stealth"):
        with pytest.raises(TypeError):
            WebPolicy(**{field: 1})


def test_packet_bound_derives_from_core_content_budget():
    maximum = crawl4ai_provider._packet_max_bytes(1024)
    assert maximum == 6 * 1024 + 64 * 1024
    packet = {
        "schema": config.WORKER_SCHEMA,
        "provider_version": config.SDK_VERSION,
        "status": "ok",
        "url": "https://example.com/",
        "content": "\x00" * 1024,
        "content_type": "text/html; rendered=1",
        "http_status": 200,
        "cost_usd": 0,
    }
    encoded = json.dumps(
        packet, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode()
    assert len(encoded) <= maximum


def test_health_requires_private_exact_pinned_stamp(monkeypatch, tmp_path):
    _, _, _, health, _, payload = _health_fixture(monkeypatch, tmp_path)
    healthy = config.health()
    assert healthy["available"] is True
    assert healthy["code"] == "ok"
    assert healthy["sdk_version"] == "0.9.4"
    assert len(healthy["runtime_binding_id"]) == 64

    browser_tree = payload.pop("browser_tree_sha256")
    health.write_text(json.dumps(payload))
    health.chmod(0o600)
    assert config.installed() is False

    payload["browser_tree_sha256"] = browser_tree
    payload["requirements_sha256"] = "0" * 64
    health.write_text(json.dumps(payload))
    health.chmod(0o600)
    assert config.installed() is False

    payload["requirements_sha256"] = config.LOCK_SHA256
    health.write_text(json.dumps(payload))
    health.chmod(0o644)
    assert config.installed() is False


def test_health_binds_virtualenv_entry_path_not_shared_symlink_target(
        monkeypatch, tmp_path):
    root = tmp_path / "provider"
    target = root / "bin/python3.14"
    python = root / "bin/python"
    browsers = root / "browsers"
    browser = browsers / "chromium-1/chrome"
    health = root / "frankensurf-health.json"
    site = root / "lib/python3.14/site-packages"
    target.parent.mkdir(parents=True)
    browser.parent.mkdir(parents=True)
    site.mkdir(parents=True)
    (root / "pyvenv.cfg").write_text("fixture = true\n")
    target.write_text("#!/bin/sh\n")
    browser.write_text("#!/bin/sh\n")
    target.chmod(0o700)
    browser.chmod(0o700)
    python.symlink_to(target.name)
    root.chmod(0o700)
    browsers.chmod(0o700)
    identity = _fake_installation_identity(monkeypatch)
    health.write_text(json.dumps({
        "schema": config.HEALTH_SCHEMA,
        "status": "ok",
        "sdk_version": config.SDK_VERSION,
        "python": str(python.absolute()),
        "browsers_path": str(browsers.resolve()),
        "browser_executable": str(browser.resolve()),
        "requirements_sha256": config.LOCK_SHA256,
        **identity(root, python, browsers, browser),
    }))
    health.chmod(0o600)
    root.chmod(0o700)
    _configure(monkeypatch, root, python, browsers, health)

    assert config.installed() is True
    monkeypatch.setenv("FRANKENSURF_CRAWL4AI_PYTHON", str(target))
    assert config.installed() is False


def test_python314_unicode_alias_is_excluded_from_worker_execution(
        monkeypatch, tmp_path):
    root, python, _, _, _, _ = _health_fixture(monkeypatch, tmp_path)
    python3 = root / "bin/python3"
    alias = root / "bin/𝜋thon"
    python3.symlink_to(python.name)
    alias.symlink_to("python3")

    before = config.runtime_binding_id()
    assert before is not None
    assert config.worker_python() == python
    assert config.worker_python() != alias
    alias.unlink()
    assert config.runtime_binding_id() == before


def test_health_and_guard_detect_bound_runtime_drift(monkeypatch, tmp_path):
    root, python, browsers, health, browser, payload = _health_fixture(
        monkeypatch, tmp_path)
    binding, guard = config.runtime_binding_snapshot()
    assert binding is not None
    assert config.runtime_guard_matches(guard) is True

    browser.write_text("#!/bin/sh\necho changed\n")
    browser.chmod(0o700)
    assert config.runtime_guard_matches(guard) is False
    assert config.health()["available"] is False


def test_explicit_health_detects_deep_dependency_drift(
        monkeypatch, tmp_path):
    root, python, browsers, health, browser, payload = _health_fixture(
        monkeypatch, tmp_path)
    dependency = root / "lib/python3.14/site-packages/example/runtime.py"
    dependency.parent.mkdir(parents=True)
    dependency.write_text("VERSION = 1\n")
    identity = _fake_installation_identity(monkeypatch, dependency)
    payload.update(identity(root, python, browsers, browser))
    health.write_text(json.dumps(payload))
    health.chmod(0o600)
    binding, guard = config.runtime_binding_snapshot()
    assert binding is not None
    provider = Crawl4AIProvider()

    dependency.write_text("VERSION = 2\n")
    assert config.health()["available"] is False
    assert provider.health()["available"] is False


async def test_runtime_acquire_rechecks_deep_dependency_before_subprocess(
        monkeypatch, tmp_path):
    real_identity = config._installation_identity
    root, python, browsers, health, browser, payload = _health_fixture(
        monkeypatch, tmp_path)
    site = root / "lib/python3.14/site-packages"
    dependency = site / "example_runtime.py"
    metadata = site / "example_runtime-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: example-runtime\nVersion: 1.0\n")
    (metadata / "RECORD").write_text(
        "example_runtime.py,,\nexample_runtime-1.0.dist-info/METADATA,,\n")
    dependency.write_text("VALUE = 1\n")
    monkeypatch.setattr(config, "_installation_identity", real_identity)
    monkeypatch.setattr(
        config, "_expected_distributions",
        lambda: {"example-runtime": "1.0"})
    identity = real_identity(root, python, browsers, browser)
    assert identity is not None
    payload.update(identity)
    health.write_text(json.dumps(payload))
    health.chmod(0o600)

    plugin = Crawl4AIProvider()
    registry = ProviderRegistry()
    registry.register(plugin)
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)

    async def forbidden_subprocess(*args, **kwargs):
        pytest.fail("drift must fail before worker startup")

    monkeypatch.setattr(
        crawl4ai_provider.asyncio, "create_subprocess_exec",
        forbidden_subprocess)
    async with Runtime(tmp_path / "state") as web:
        catalog_binding = web.providers.binding_id("crawl4ai")
        before = dependency.stat()
        dependency.write_text("VALUE = 2\n")
        after = dependency.stat()
        assert after.st_size == before.st_size
        assert plugin.available(configured=()) is True
        result = await web.read(
            "https://example.com/item",
            WebPolicy(provider="crawl4ai"),
        )

    assert result["receipt"]["failure"]["code"] == "PROVIDER_UNAVAILABLE"
    assert len(result["receipt"]["attempts"]) == 1
    attempt = result["receipt"]["attempts"][0]
    assert attempt["provider"] == "crawl4ai"
    assert attempt["provider_version"] == config.SDK_VERSION
    assert attempt["provider_binding_id"] == catalog_binding
    assert attempt["status"] == "failed"
    assert attempt["failure"] == "PROVIDER_UNAVAILABLE"


async def test_runtime_acquire_binds_pyvenv_contents_before_subprocess(
        monkeypatch, tmp_path):
    root, python, browsers, _, browser, payload = _health_fixture(
        monkeypatch, tmp_path)
    pyvenv = root / "pyvenv.cfg"
    plugin = Crawl4AIProvider()
    registry = ProviderRegistry()
    registry.register(plugin)
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)

    async def forbidden_subprocess(*args, **kwargs):
        pytest.fail("pyvenv drift must fail before worker startup")

    monkeypatch.setattr(
        crawl4ai_provider.asyncio, "create_subprocess_exec",
        forbidden_subprocess)
    async with Runtime(tmp_path / "state") as web:
        before = pyvenv.stat()
        pyvenv.write_bytes(pyvenv.read_bytes().replace(b"true", b"faux"))
        os.utime(pyvenv, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = pyvenv.stat()
        assert after.st_size == before.st_size
        assert after.st_mtime_ns == before.st_mtime_ns
        changed = config._installation_identity(
            root, python, browsers, browser)
        assert changed["pyvenv_cfg_sha256"] != payload[
            "pyvenv_cfg_sha256"]
        # Exercise the full executor identity independently of the metadata
        # guard, which can also detect ordinary ctime drift.
        monkeypatch.setattr(
            config, "runtime_guard_matches", lambda expected: True)
        result = await web.read(
            "https://example.com/item",
            WebPolicy(provider="crawl4ai"),
        )

    assert result["receipt"]["failure"]["code"] == "PROVIDER_UNAVAILABLE"
    assert result["receipt"]["attempts"][0]["failure"] == (
        "PROVIDER_UNAVAILABLE")


async def test_runtime_acquire_rechecks_deep_browser_asset_before_subprocess(
        monkeypatch, tmp_path):
    root, _, browsers, _, _, _ = _health_fixture(monkeypatch, tmp_path)
    browser_asset = browsers / "chromium-1/resources/runtime.pak"
    plugin = Crawl4AIProvider()
    registry = ProviderRegistry()
    registry.register(plugin)
    monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)

    async def forbidden_subprocess(*args, **kwargs):
        pytest.fail("browser-tree drift must fail before worker startup")

    monkeypatch.setattr(
        crawl4ai_provider.asyncio, "create_subprocess_exec",
        forbidden_subprocess)
    async with Runtime(tmp_path / "state") as web:
        catalog_binding = web.providers.binding_id("crawl4ai")
        before = browser_asset.stat()
        browser_asset.write_bytes(b"fixture-browser-asset-v2\n")
        after = browser_asset.stat()
        assert after.st_size == before.st_size
        assert config.runtime_guard_matches(plugin._executor_guard) is False
        # Force routing past the cheap guard to prove acquire's exact identity
        # check independently catches the deep change before process creation.
        monkeypatch.setattr(
            config, "runtime_guard_matches", lambda expected: True)
        result = await web.read(
            "https://example.com/item",
            WebPolicy(provider="crawl4ai"),
        )

    assert result["receipt"]["failure"]["code"] == "PROVIDER_UNAVAILABLE"
    assert len(result["receipt"]["attempts"]) == 1
    attempt = result["receipt"]["attempts"][0]
    assert attempt["provider"] == "crawl4ai"
    assert attempt["provider_binding_id"] == catalog_binding
    assert attempt["status"] == "failed"
    assert attempt["failure"] == "PROVIDER_UNAVAILABLE"


def test_health_rejects_wrong_owner_and_symlinked_artifacts(
        monkeypatch, tmp_path):
    root, python, browsers, health, browser, payload = _health_fixture(
        monkeypatch, tmp_path)

    def refresh_identity():
        payload.update(config._installation_identity(
            root, python, browsers, browser))
        health.write_text(json.dumps(payload))
        health.chmod(0o600)
        assert config.installed() is True

    actual_getuid = os.getuid
    monkeypatch.setattr(config.os, "getuid", lambda: actual_getuid() + 1)
    assert config.installed() is False
    monkeypatch.setattr(config.os, "getuid", actual_getuid)

    browser.chmod(0o722)
    assert config.installed() is False
    browser.chmod(0o700)
    refresh_identity()

    browser_asset = browsers / "chromium-1/resources/runtime.pak"
    asset_target = browser_asset.with_name("runtime-target.pak")
    browser_asset.rename(asset_target)
    browser_asset.symlink_to(asset_target.name)
    assert config.installed() is False
    browser_asset.unlink()
    asset_target.rename(browser_asset)
    refresh_identity()

    browser_asset.chmod(0o666)
    assert config.installed() is False
    browser_asset.chmod(0o644)
    refresh_identity()

    health.parent.chmod(0o750)
    assert config.installed() is False
    health.parent.chmod(0o700)

    dependency_parent = root / "lib/python3.14"
    dependency_parent.chmod(0o777)
    assert config.installed() is False
    dependency_parent.chmod(0o755)

    stamp_target = health.with_name("health-target.json")
    health.rename(stamp_target)
    health.symlink_to(stamp_target.name)
    assert config.installed() is False
    health.unlink()
    stamp_target.rename(health)

    browser_target = browser.with_name("chrome-target")
    browser.rename(browser_target)
    browser.symlink_to(browser_target.name)
    assert config.installed() is False
    browser.unlink()
    browser_target.rename(browser)
    refresh_identity()

    browsers_target = browsers.with_name("browsers-target")
    browsers.rename(browsers_target)
    browsers.symlink_to(browsers_target.name, target_is_directory=True)
    assert config.installed() is False


async def test_retention_io_error_preserves_typed_acquisition_failure(
        monkeypatch):
    monkeypatch.setattr(
        config, "runtime_binding_snapshot", lambda: ("a" * 64, ()))
    monkeypatch.setattr(config, "runtime_guard_matches", lambda guard: True)
    monkeypatch.setattr(
        crawl4ai_provider, "_worker_environment", lambda *args: {})

    async def create_process(*args, **kwargs):
        return object()

    async def read_packet(process, request, maximum):
        assert maximum == crawl4ai_provider._packet_max_bytes(
            request["max_bytes"])
        assert not {
            "packet_max_bytes", "readiness_poll_ms", "enable_stealth",
        } & set(request)
        return {
            "schema": config.WORKER_SCHEMA,
            "provider_version": config.SDK_VERSION,
            "status": "failed",
            "failure": "BLOCKED",
            "url": "https://example.com/blocked",
            "content": "<html>public challenge</html>",
            "content_type": "text/html; rendered=1",
            "http_status": 403,
            "cost_usd": 0,
        }

    async def terminate(*args, **kwargs):
        return None

    def fail_retention(*args, **kwargs):
        raise OSError("simulated evidence I/O failure")

    monkeypatch.setattr(
        crawl4ai_provider.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(crawl4ai_provider, "_read_packet", read_packet)
    monkeypatch.setattr(crawl4ai_provider, "_terminate", terminate)

    registry = ProviderRegistry()
    registry.register(Crawl4AIProvider())
    services = ProviderServices(None, None, None)
    with pytest.raises(WebFailure) as caught:
        await registry.acquire(
            "crawl4ai",
            ProviderRequest(
                "https://example.com/blocked",
                WebPolicy(
                    provider="crawl4ai",
                    max_bytes=1024,
                    retain_public_failure_evidence=True,
                ),
            ),
            services,
            _evidence_preparer=fail_retention,
        )

    assert caught.value.code == "BLOCKED"
    assert caught.value.http_status == 403
    assert caught.value.response_url == "https://example.com/blocked"
    assert caught.value._public_failure_evidence == []


def test_worker_environment_is_job_local_and_secret_free(
        monkeypatch, tmp_path):
    _, _, browsers, _, _, _ = _health_fixture(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-cross")
    monkeypatch.setenv("HTTP_PROXY", "http://name:secret@proxy.test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-cross")
    job = tmp_path / "job"
    job.mkdir()
    environment = _worker_environment(job, WebPolicy())
    assert environment["PLAYWRIGHT_BROWSERS_PATH"] == str(
        browsers.resolve())
    assert environment["PATCHRIGHT_BROWSERS_PATH"] == str(
        browsers.resolve())
    assert environment["HOME"] == str(job / "home")
    assert environment["XDG_CACHE_HOME"] == str(job / "home/.cache")
    assert not {"OPENAI_API_KEY", "HTTP_PROXY", "AWS_SECRET_ACCESS_KEY"} & set(
        environment)
    assert not (Path(environment["HOME"]) / ".cache/ms-playwright").exists()


async def test_authority_fails_before_install_health(monkeypatch):
    provider = Crawl4AIProvider()
    monkeypatch.setattr(
        config, "runtime_guard_matches",
        lambda guard: pytest.fail("health must not be consulted"))

    with pytest.raises(WebFailure) as caught:
        await provider.acquire(ProviderRequest(
            "https://example.com/", WebPolicy(
                provider="crawl4ai", allow_local_browser=False)), None)
    assert caught.value.code == "POLICY_DENIED"

    with pytest.raises(WebFailure) as caught:
        await provider.acquire(ProviderRequest(
            "https://example.com/", WebPolicy(
                provider="crawl4ai", identity="owner")), None)
    assert caught.value.code == "IDENTITY_POLICY_DENIED"


@pytest.fixture
def javascript_server(tmp_path):
    root = tmp_path / "site"
    root.mkdir()
    (root / "index.html").write_text(
        "<!doctype html><html><head><title>Local dynamic fixture</title></head>"
        "<body><main><h1 id='listing'>Loading fixture</h1>"
        "<p id='description'>Waiting for JavaScript rendering.</p></main>"
        "<script>setTimeout(()=>{"
        "document.getElementById('listing').textContent="
        "'JS rendered listing 842';"
        "document.getElementById('listing').dataset.ready='true';"
        "document.getElementById('description').textContent="
        "'Current public fixture content rendered by the isolated worker "
        "with enough exact text for Core evidence and adapter validation.';"
        "},150);</script></body></html>",
        encoding="utf-8")
    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/blocked.html":
                body = (
                    b"<html><head><title>Access denied</title></head>"
                    b"<body><p>Controlled public challenge evidence.</p></body>"
                    b"</html>"
                )
                self.send_response(403)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            super().do_GET()

        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(Handler, directory=str(root)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:" + str(server.server_port) + "/index.html"
    server.shutdown()
    server.server_close()
    thread.join()


_sdk_health = config.health()
sdk = pytest.mark.skipif(
    _sdk_health["available"] is not True,
    reason="Run scripts/install-crawl4ai-provider.sh for actual SDK coverage")


@sdk
async def test_actual_isolated_worker_renders_js_through_core_extract(
        monkeypatch, tmp_path, javascript_server):
    python = config.worker_python()
    browsers = config.browsers_path().resolve()
    health = config.health_path().resolve()
    root = config.environment_dir().resolve()
    _configure(monkeypatch, root, python, browsers, health)
    fake_home = tmp_path / "caller-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("OPENAI_API_KEY", "never-send-this-secret")

    async with Runtime(tmp_path / "state") as web:
        result = await web.extract(
            javascript_server,
            "html",
            WebPolicy(
                provider="crawl4ai",
                freshness="now",
                timeout_seconds=20,
                settle_ms=0,
                wait_selector="#listing[data-ready='true']",
                content_ready_selector="#listing[data-ready='true']",
                content_ready_timeout_seconds=5,
                max_bytes=1024 * 1024,
            ),
        )

    assert result["receipt"]["status"] == "observed"
    assert result["receipt"]["method"] == "crawl4ai"
    assert result["receipt"]["provider_version"] == "0.9.4"
    assert result["receipt"]["cost_usd"] == 0
    assert result["receipt"]["evidence"]
    assert result["receipt"]["content_readiness"] == {
        "status": "satisfied", "timeout_seconds": 5}
    assert result["title"] == "Local dynamic fixture"
    assert "JS rendered listing 842" in result["text"]
    assert result["receipt"]["attempts"][0]["provider"] == "crawl4ai"
    assert result["receipt"]["attempts"][0]["provider_binding_id"]
    assert not (fake_home / ".cache/ms-playwright").exists()


@sdk
async def test_actual_isolated_worker_retains_bounded_public_failure(
        monkeypatch, tmp_path, javascript_server):
    python = config.worker_python()
    browsers = config.browsers_path().resolve()
    health = config.health_path().resolve()
    root = config.environment_dir().resolve()
    _configure(monkeypatch, root, python, browsers, health)
    fake_home = tmp_path / "caller-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    blocked_url = javascript_server.rsplit("/", 1)[0] + "/blocked.html"

    async with Runtime(tmp_path / "state") as web:
        result = await web.read(
            blocked_url,
            WebPolicy(
                provider="crawl4ai",
                freshness="now",
                timeout_seconds=20,
                max_bytes=1024 * 1024,
                retain_public_failure_evidence=True,
            ),
        )

    receipt = result["receipt"]
    assert receipt["status"] == "failed"
    assert receipt["failure"]["code"] == "BLOCKED"
    assert result["content"] == ""
    evidence = receipt["attempts"][0]["evidence"]
    assert len(evidence) == 1
    artifact = Path(evidence[0]["path"])
    assert artifact.is_relative_to(tmp_path / "state/evidence")
    assert not artifact.is_relative_to(fake_home)
    assert artifact.stat().st_mode & 0o777 == 0o600
    assert "Controlled public challenge evidence" in artifact.read_text()
