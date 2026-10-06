"""Pinned isolated Crawl4AI ProviderPlugin for anonymous public acquisition."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile

from . import crawl4ai_config as config
from .crawl4ai_worker import _packet_max_bytes
from .runtime import WebFailure, _challenge, _status_failure, _validate_url, parse_content


_FAILURES = frozenset({
    "AUTH_REQUIRED", "BLOCKED", "CAPTCHA", "LIMIT_EXCEEDED", "NOT_FOUND",
    "POLICY_DENIED", "PROVIDER_DOWN", "PROVIDER_UNAVAILABLE",
    "RATE_LIMITED", "TIMEOUT",
})


def _retain_failure(content, policy, services):
    """Stage bounded public bytes; Core alone owns evidence persistence."""
    if (not policy.retain_public_failure_evidence or policy.identity
            or not isinstance(content, str)):
        return []
    try:
        raw = content.encode()
    except UnicodeError:
        return []
    if len(raw) > policy.max_bytes:
        return []
    callback = getattr(services, "retain_failure_evidence", None)
    if callback is None:
        return []
    try:
        callback(raw, "text/html; rendered=1")
    except Exception:
        # Retention is optional diagnostics and never replaces acquisition truth.
        pass
    return []


def _worker_environment(job, policy):
    job = Path(job)
    home = job / "home"
    temporary = job / "tmp"
    runtime = job / "runtime"
    crawl4ai = job / "crawl4ai"
    for directory in (home, temporary, runtime, crawl4ai):
        directory.mkdir(mode=0o700)
    python = config.worker_python()
    browsers = config.browsers_path().resolve(strict=True)
    environment = {
        "PATH": str(python.parent) + ":/usr/bin:/bin",
        "HOME": str(home),
        "TMPDIR": str(temporary),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_RUNTIME_DIR": str(runtime),
        "CRAWL4AI_BASE_DIRECTORY": str(crawl4ai),
        "PLAYWRIGHT_BROWSERS_PATH": str(browsers),
        "PATCHRIGHT_BROWSERS_PATH": str(browsers),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    if not policy.public_browser_headless:
        for name in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY"):
            value = os.environ.get(name)
            if value:
                environment[name] = value
    return environment


async def _terminate(process, grace):
    from .browser_use_provider import _terminate
    await _terminate(process, grace)


async def _read_packet(process, request, maximum):
    request_bytes = json.dumps(
        request, sort_keys=True, separators=(",", ":")).encode()
    if len(request_bytes) > maximum:
        raise WebFailure(
            "LIMIT_EXCEEDED", "Crawl4AI request packet exceeds policy")
    process.stdin.write(request_bytes)
    await process.stdin.drain()
    process.stdin.close()
    chunks = bytearray()
    while True:
        remaining = maximum + 1 - len(chunks)
        if remaining <= 0:
            raise WebFailure(
                "LIMIT_EXCEEDED", "Crawl4AI worker packet exceeds policy")
        chunk = await process.stdout.read(min(65536, remaining))
        if not chunk:
            break
        chunks.extend(chunk)
    await process.wait()
    if len(chunks) > maximum:
        raise WebFailure(
            "LIMIT_EXCEEDED", "Crawl4AI worker packet exceeds policy")
    try:
        packet = json.loads(chunks)
    except (ValueError, UnicodeError):
        raise WebFailure(
            "PROVIDER_DOWN", "Crawl4AI worker returned an invalid packet") from None
    if process.returncode != 0 or type(packet) is not dict:
        raise WebFailure("PROVIDER_DOWN", "Crawl4AI worker failed")
    return packet


class Crawl4AIProvider:
    def __init__(self):
        from .providers import ProviderManifest
        self.manifest = ProviderManifest(
            "crawl4ai",
            config.SDK_VERSION,
            rendering=True,
            requires_local_browser=True,
            paid=False,
            authentication=False,
            navigation=False,
            cost_bounded=True,
            operations=("read", "extract"),
        )
        # Plugin-catalog provenance includes this immutable executor identity:
        # worker source, pyvenv.cfg contents, exact installed distributions,
        # interpreter executable and every owned browser-tree file. Any change
        # requires a new Runtime.
        (self.executor_binding_id,
         self._executor_guard) = config.runtime_binding_snapshot()

    def _runtime_available(self, *, exact=False):
        if self.executor_binding_id is None:
            return False
        if exact:
            return config.runtime_binding_id() == self.executor_binding_id
        return config.runtime_guard_matches(self._executor_guard)

    def available(self, configured):
        return self._runtime_available()

    def health(self):
        health = config.health()
        if (health.get("available") is not True
                or health.get("runtime_binding_id")
                    != self.executor_binding_id):
            return {
                "available": False,
                "code": "PROVIDER_UNAVAILABLE",
                "sdk_version": config.SDK_VERSION,
            }
        return health

    async def acquire(self, request, services):
        policy = request.policy
        if request.operation not in self.manifest.operations:
            raise WebFailure(
                "POLICY_DENIED", "Crawl4AI does not declare this operation")
        if policy.identity:
            raise WebFailure(
                "IDENTITY_POLICY_DENIED",
                "Anonymous Crawl4AI cannot execute a named identity")
        if not policy.allow_local_browser:
            raise WebFailure(
                "POLICY_DENIED", "Crawl4AI requires local browser permission")
        _validate_url(request.url)
        if not self._runtime_available(exact=True):
            raise WebFailure(
                "PROVIDER_UNAVAILABLE",
                "Pinned Crawl4AI executor changed or is unavailable")
        python = config.worker_python()
        worker = Path(__file__).with_name("crawl4ai_worker.py")
        packet_maximum = _packet_max_bytes(policy.max_bytes)
        payload = {
            "schema": config.WORKER_SCHEMA,
            "provider_version": config.SDK_VERSION,
            "url": request.url,
            "operation": request.operation,
            "timeout_seconds": policy.timeout_seconds,
            "max_bytes": policy.max_bytes,
            "settle_ms": policy.settle_ms,
            "headless": policy.public_browser_headless,
            "wait_selector": policy.wait_selector,
            "wait_state": policy.wait_state,
            "content_ready_selector": policy.content_ready_selector,
            "content_ready_timeout_seconds":
                policy.content_ready_timeout_seconds,
        }
        with tempfile.TemporaryDirectory(
                prefix="frankensurf-crawl4ai-") as directory:
            environment = _worker_environment(directory, policy)
            try:
                process = await asyncio.create_subprocess_exec(
                    str(python), str(worker),
                    "--request-max-bytes",
                    str(packet_maximum),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=environment,
                    cwd=directory,
                    start_new_session=os.name == "posix",
                )
            except OSError:
                raise WebFailure(
                    "PROVIDER_UNAVAILABLE",
                    "Pinned Crawl4AI worker could not start") from None
            try:
                packet = await asyncio.wait_for(
                    _read_packet(process, payload, packet_maximum),
                    policy.timeout_seconds
                    + policy.provider_deadline_grace_seconds,
                )
            except TimeoutError:
                raise WebFailure(
                    "TIMEOUT", "Crawl4AI acquisition deadline exceeded",
                    cost_usd=0) from None
            except WebFailure as failure:
                failure.cost_usd = 0
                raise
            finally:
                await _terminate(
                    process, policy.provider_cleanup_grace_seconds)

        allowed = {
            "schema", "provider_version", "status", "failure", "url",
            "content", "content_type", "http_status", "cost_usd",
            "content_readiness",
        }
        if (set(packet) - allowed
                or packet.get("schema") != config.WORKER_SCHEMA
                or packet.get("provider_version") != config.SDK_VERSION
                or type(packet.get("cost_usd")) not in (int, float)
                or packet.get("cost_usd") != 0):
            raise WebFailure(
                "PROVIDER_DOWN", "Crawl4AI worker protocol mismatch",
                cost_usd=0)
        final_url = packet.get("url")
        if final_url is not None:
            _validate_url(final_url)
        status = packet.get("http_status")
        if status is not None and (type(status) is not int
                                   or not 100 <= status <= 599):
            raise WebFailure(
                "PROVIDER_DOWN", "Crawl4AI returned invalid status metadata",
                cost_usd=0)
        content = packet.get("content")
        if packet.get("status") != "ok":
            failure_content_type = packet.get("content_type")
            if (content is not None
                    and (not isinstance(content, str)
                         or failure_content_type
                            != "text/html; rendered=1"
                         or len(content.encode()) > policy.max_bytes)):
                raise WebFailure(
                    "PROVIDER_DOWN",
                    "Crawl4AI returned invalid bounded failure evidence",
                    cost_usd=0,
                )
            if content is None and failure_content_type is not None:
                raise WebFailure(
                    "PROVIDER_DOWN",
                    "Crawl4AI returned invalid failure evidence metadata",
                    cost_usd=0,
                )
            code = packet.get("failure")
            failure = WebFailure(
                code if code in _FAILURES else "PROVIDER_DOWN",
                "Crawl4AI public acquisition failed",
                status,
                response_url=final_url,
                cost_usd=0,
            )
            _retain_failure(content, policy, services)
            raise failure
        required = {
            "schema", "provider_version", "status", "url", "content",
            "content_type", "http_status", "cost_usd",
        }
        if (not required <= set(packet) or not isinstance(final_url, str)
                or not isinstance(content, str)
                or packet.get("content_type") != "text/html; rendered=1"
                or status is None):
            raise WebFailure(
                "PROVIDER_DOWN",
                "Crawl4AI returned an invalid acquisition envelope",
                cost_usd=0)
        raw = content.encode()
        if len(raw) > policy.max_bytes:
            raise WebFailure(
                "LIMIT_EXCEEDED",
                "Crawl4AI response exceeds policy byte budget",
                response_url=final_url,
                cost_usd=0,
            )
        if failure := _status_failure(status):
            error = WebFailure(
                failure, "Crawl4AI response requires stop", status,
                response_url=final_url, cost_usd=0)
            _retain_failure(content, policy, services)
            raise error
        if _challenge(content):
            error = WebFailure(
                "CAPTCHA", "Crawl4AI observed a challenge page", status,
                response_url=final_url, cost_usd=0)
            _retain_failure(content, policy, services)
            raise error
        parsed = parse_content(
            content, packet["content_type"], final_url, None)
        if (len(parsed["text"].strip()) < policy.rendered_min_text_chars
                and "<script" in content.lower()):
            error = WebFailure(
                "VISUAL_REQUIRED",
                "Crawl4AI returned an unresolved JavaScript shell", status,
                response_url=final_url, cost_usd=0)
            _retain_failure(content, policy, services)
            raise error
        result = {
            "url": final_url,
            "content": content,
            "raw": raw,
            "content_type": packet["content_type"],
            "http_status": status,
            "headers": {},
            "cost_usd": 0,
        }
        if "content_readiness" in packet:
            result["content_readiness"] = packet["content_readiness"]
        return result
