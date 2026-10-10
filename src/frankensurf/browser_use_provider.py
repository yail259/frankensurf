"""Optional locally installed Browser Use agent, behind the acquisition contract."""
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import tempfile
from time import monotonic
from dataclasses import asdict

from . import browser_use_config as config

FAILURES = frozenset({"PROVIDER_UNAVAILABLE", "PROVIDER_DOWN", "TIMEOUT", "BUDGET_EXHAUSTED",
                      "POLICY_DENIED", "CONTENT_MISMATCH", "LIMIT_EXCEEDED", "BLOCKED", "CAPTCHA"})


async def _terminate(process, grace):
    deadline = monotonic() + grace
    if process.returncode is not None and os.name != "posix":
        return
    try:
        if os.name == "posix": os.killpg(process.pid, signal.SIGTERM)
        else: process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), max(0, deadline - monotonic()))
    except TimeoutError:
        try:
            if os.name == "posix": os.killpg(process.pid, signal.SIGKILL)
            else: process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
    if os.name == "posix":
        while _group_running(process.pid) and monotonic() < deadline:
            await asyncio.sleep(min(0.05, max(0, deadline - monotonic())))
        if _group_running(process.pid):
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass


def _group_running(group):
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = path.read_text().rpartition(")")[2].split()
            if fields[0] != "Z" and int(fields[2]) == group:
                return True
        except (OSError, ValueError, IndexError):
            continue
    return False


async def _read_packet(process, request, maximum):
    process.stdin.write(json.dumps(request).encode())
    await process.stdin.drain()
    process.stdin.close()
    chunks = bytearray()
    while chunk := await process.stdout.read(min(65536, maximum + 1 - len(chunks))):
        chunks.extend(chunk)
        if len(chunks) > maximum:
            from .runtime import WebFailure
            raise WebFailure("LIMIT_EXCEEDED", "Browser agent packet exceeds caller budget")
    await process.wait()
    try:
        packet = json.loads(chunks)
    except (ValueError, UnicodeError):
        from .runtime import WebFailure
        raise WebFailure("PROVIDER_DOWN", "Browser agent returned an invalid packet") from None
    if process.returncode != 0 or not isinstance(packet, dict):
        from .runtime import WebFailure
        raise WebFailure("PROVIDER_DOWN", "Browser agent worker failed")
    return packet


def _retain_failure(content, policy):
    if not policy.retain_public_failure_evidence or policy.identity or not isinstance(content, str):
        return []
    raw = content.encode()
    if len(raw) > policy.max_bytes:
        return []
    directory = Path.home() / ".local/share/frankensurf"
    digest = hashlib.sha256(raw).hexdigest()
    from .repair import _write_private_bytes
    return [_write_private_bytes(
        directory, ("browser-use-evidence", digest + ".html"), raw)]


class BrowserUseProvider:
    @property
    def manifest(self):
        from .providers import ProviderManifest
        version, paid = config.manifest_metadata()
        # Route scope: a model-driven agent takes tens of seconds a page, so
        # it runs when a call names it (tasks, repair), not in ordinary reads.
        return ProviderManifest("browser_use", version, rendering=True,
                                requires_local_browser=True, paid=paid,
                                navigation=False, cost_bounded=True,
                                diagnosis=True, route_scope_required=True)

    def available(self, configured):
        return config.configured()

    async def diagnose(self, request, services):
        """Explore a public failure and return one untrusted declarative proposal."""
        from .repair import RepairProviderRequest
        from .runtime import WebFailure, _validate_url
        if not isinstance(request, RepairProviderRequest):
            raise WebFailure("POLICY_DENIED", "Invalid repair diagnosis request")
        policy = request.policy
        if policy.identity:
            raise WebFailure(
                "IDENTITY_POLICY_DENIED",
                "Public browser repair cannot execute a named identity")
        if not policy.allow_local_browser:
            raise WebFailure("POLICY_DENIED",
                             "Local browser permission required")
        try:
            binding = config.snapshot()
        except (config.ConfigurationError, OSError, ValueError):
            raise WebFailure(
                "PROVIDER_UNAVAILABLE",
                "Browser Use repair requires its pinned isolated SDK, local browser and operator model binding with billing metadata")
        expected_version = config.SDK_VERSION + "+" + binding.fingerprint
        if (self.manifest.version != expected_version
                or self.manifest.paid != (binding.billing == "paid")
                or not self.manifest.diagnosis):
            raise WebFailure(
                "PROVIDER_UNAVAILABLE",
                "Browser agent model binding changed after catalog startup")
        if binding.billing == "paid" and not policy.allow_paid_fallbacks:
            raise WebFailure(
                "POLICY_DENIED",
                "Configured model requires paid fallback permission")
        _validate_url(request.url)
        if (type(request.context) is not dict
                or type(request.limits) is not dict
                or set(request.limits) != {
                    "max_proposal_bytes", "max_string_chars",
                    "max_proposal_bindings"}
                or any(type(value) is not int or value < 1
                       for value in request.limits.values())):
            raise WebFailure("POLICY_DENIED",
                             "Repair diagnosis boundary is invalid")
        try:
            context_bytes = json.dumps(
                request.context, sort_keys=True, separators=(",", ":"),
                allow_nan=False).encode()
        except (TypeError, ValueError, OverflowError):
            raise WebFailure("POLICY_DENIED",
                             "Repair diagnosis context is invalid") from None
        if len(context_bytes) > request.limits["max_proposal_bytes"]:
            raise WebFailure("LIMIT_EXCEEDED",
                             "Repair diagnosis context exceeds policy")
        origins = policy.browser_agent_allowed_origins or (
            config.origin(request.url),)
        entry = policy.browser_agent_entry_url or request.url
        if (config.origin(request.url) not in origins
                or config.origin(entry) not in origins):
            raise WebFailure(
                "POLICY_DENIED",
                "Browser repair request and entry must satisfy origin policy")
        payload = {
            "url": request.url,
            "policy": asdict(policy),
            "origins": origins,
            "browser_executable": str(binding.browser),
            "binding_fingerprint": binding.fingerprint,
            "repair": {
                "context": request.context,
                "limits": request.limits,
            },
        }
        worker = Path(__file__).with_name("browser_use_worker.py")
        with tempfile.TemporaryDirectory(
                prefix="frankensurf-browser-repair-") as job:
            payload["job_dir"] = job
            process = await asyncio.create_subprocess_exec(
                str(binding.python), str(worker),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=os.name == "posix")
            try:
                packet = await asyncio.wait_for(
                    _read_packet(
                        process, payload,
                        policy.browser_agent_packet_max_bytes),
                    policy.timeout_seconds
                    + policy.provider_deadline_grace_seconds)
            except TimeoutError:
                raise WebFailure(
                    "TIMEOUT",
                    "Browser agent repair deadline exceeded",
                    cost_usd=None) from None
            except WebFailure as failure:
                failure.cost_usd = None
                raise
            finally:
                await _terminate(
                    process, policy.provider_cleanup_grace_seconds)
        cost = packet.get("cost_usd")
        if (cost is not None
                and (type(cost) not in (int, float)
                     or not math.isfinite(cost) or cost < 0)):
            raise WebFailure(
                "PROVIDER_DOWN",
                "Browser repair returned invalid cost metadata",
                cost_usd=None)
        allowed = {
            "status", "failure", "url", "content", "content_type",
            "cost_usd", "binding_fingerprint", "proposal",
            "content_readiness",
        }
        if set(packet) - allowed:
            raise WebFailure(
                "PROVIDER_DOWN",
                "Browser repair returned unknown packet fields",
                cost_usd=cost)
        try:
            unchanged = config.snapshot().fingerprint == binding.fingerprint
        except (config.ConfigurationError, OSError, ValueError):
            unchanged = False
        if (not unchanged
                or packet.get("binding_fingerprint")
                    != binding.fingerprint):
            raise WebFailure(
                "PROVIDER_UNAVAILABLE",
                "Browser agent model binding changed during repair",
                cost_usd=cost)
        final_url = packet.get("url")
        if not isinstance(final_url, str):
            raise WebFailure(
                "PROVIDER_DOWN",
                "Browser repair omitted observed URL", cost_usd=cost)
        try:
            _validate_url(final_url)
        except WebFailure as failure:
            failure.cost_usd = cost
            raise
        if config.origin(final_url) not in origins:
            raise WebFailure(
                "POLICY_DENIED",
                "Browser repair observed a disallowed origin",
                cost_usd=cost)
        content = packet.get("content")
        if (content is not None
                and (not isinstance(content, str)
                     or len(content.encode()) > policy.max_bytes)):
            raise WebFailure(
                "LIMIT_EXCEEDED",
                "Browser repair content exceeds caller budget",
                cost_usd=cost)
        if packet.get("status") != "ok":
            code = packet.get("failure")
            failure = WebFailure(
                code if isinstance(code, str) and code in FAILURES
                else "PROVIDER_DOWN",
                "Local browser repair diagnosis failed",
                response_url=final_url, cost_usd=cost)
            failure._public_failure_evidence = _retain_failure(
                content, policy)
            raise failure
        if config.subject_url(final_url) != config.subject_url(request.url):
            failure = WebFailure(
                "CONTENT_MISMATCH",
                "Browser repair did not observe the requested subject",
                response_url=final_url, cost_usd=cost)
            failure._public_failure_evidence = _retain_failure(
                content, policy)
            raise failure
        if (not isinstance(content, str)
                or packet.get("content_type")
                    != "text/html; rendered=1"):
            raise WebFailure(
                "PROVIDER_DOWN",
                "Browser repair omitted observed HTML",
                cost_usd=cost)
        proposal = packet.get("proposal")
        try:
            proposal_bytes = json.dumps(
                proposal, sort_keys=True, separators=(",", ":"),
                allow_nan=False).encode()
        except (TypeError, ValueError, OverflowError):
            raise WebFailure(
                "PROVIDER_DOWN",
                "Browser repair returned an invalid proposal",
                cost_usd=cost) from None
        if (type(proposal) is not dict
                or len(proposal_bytes)
                    > request.limits["max_proposal_bytes"]):
            raise WebFailure(
                "LIMIT_EXCEEDED",
                "Browser repair proposal exceeds policy",
                cost_usd=cost)
        return {
            "url": final_url,
            "content": content,
            "raw": content.encode(),
            "content_type": "text/html; rendered=1",
            "http_status": None,
            "headers": {},
            "cost_usd": cost,
            "proposal": proposal,
        }

    async def acquire(self, request, services):
        from .runtime import WebFailure, _validate_url, _challenge
        policy = request.policy
        if policy.identity:
            raise WebFailure("IDENTITY_POLICY_DENIED", "Public browser agent cannot execute a named identity")
        if not policy.allow_local_browser:
            raise WebFailure("POLICY_DENIED", "Local browser permission required")
        if policy.navigation_page is not None and policy.navigation_page > 1:
            raise WebFailure("POLICY_DENIED", "Browser agent does not provide numbered pagination")
        try:
            binding = config.snapshot()
        except (config.ConfigurationError, OSError, ValueError):
            raise WebFailure("PROVIDER_UNAVAILABLE", "Browser Use requires its pinned isolated SDK, local browser and operator model binding with billing metadata")
        expected_version = config.SDK_VERSION + "+" + binding.fingerprint
        if (self.manifest.version != expected_version
                or self.manifest.paid != (binding.billing == "paid")):
            raise WebFailure(
                "PROVIDER_UNAVAILABLE",
                "Browser agent model binding changed after catalog startup")
        if binding.billing == "paid" and not policy.allow_paid_fallbacks:
            raise WebFailure("POLICY_DENIED", "Configured model requires paid fallback permission")
        _validate_url(request.url)
        origins = policy.browser_agent_allowed_origins or (config.origin(request.url),)
        entry = policy.browser_agent_entry_url or request.url
        if config.origin(request.url) not in origins or config.origin(entry) not in origins:
            raise WebFailure("POLICY_DENIED", "Browser agent request and entry must satisfy origin policy")
        payload = {"url": request.url, "policy": asdict(policy), "origins": origins,
                   "browser_executable": str(binding.browser), "binding_fingerprint": binding.fingerprint}
        worker = Path(__file__).with_name("browser_use_worker.py")
        with tempfile.TemporaryDirectory(prefix="frankensurf-browser-use-") as job:
            payload["job_dir"] = job
            process = await asyncio.create_subprocess_exec(str(binding.python), str(worker),
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL, start_new_session=os.name == "posix")
            try:
                packet = await asyncio.wait_for(_read_packet(process, payload, policy.browser_agent_packet_max_bytes),
                                                policy.timeout_seconds + policy.provider_deadline_grace_seconds)
            except TimeoutError:
                raise WebFailure("TIMEOUT", "Browser agent acquisition deadline exceeded", cost_usd=None) from None
            except WebFailure as failure:
                failure.cost_usd = None
                raise
            finally:
                await _terminate(process, policy.provider_cleanup_grace_seconds)
        cost = packet.get("cost_usd")
        if cost is not None and (type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0):
            raise WebFailure("PROVIDER_DOWN", "Browser agent returned invalid cost metadata", cost_usd=None)
        if set(packet) - {"status", "failure", "url", "content", "content_type", "cost_usd", "binding_fingerprint", "content_readiness"}:
            raise WebFailure("PROVIDER_DOWN", "Browser agent returned unknown packet fields", cost_usd=cost)
        try:
            unchanged = config.snapshot().fingerprint == binding.fingerprint
        except (config.ConfigurationError, OSError, ValueError):
            unchanged = False
        if not unchanged or packet.get("binding_fingerprint") != binding.fingerprint:
            raise WebFailure("PROVIDER_UNAVAILABLE", "Browser agent model binding changed during acquisition", cost_usd=cost)
        final_url = packet.get("url")
        if not isinstance(final_url, str):
            raise WebFailure("PROVIDER_DOWN", "Browser agent omitted observed URL", cost_usd=cost)
        try:
            _validate_url(final_url)
        except WebFailure as failure:
            if packet.get("status") != "ok":
                # A blank or error tab: the agent never reached the page, so the
                # tool failed, not the URL. Unavailable, not down: a second try
                # of an agent that never navigated costs the same and fails alike.
                raise WebFailure("PROVIDER_UNAVAILABLE", "Browser agent never reached the page",
                                 cost_usd=cost) from None
            failure.cost_usd = cost
            raise
        if config.origin(final_url) not in origins:
            raise WebFailure("POLICY_DENIED", "Browser agent observed a disallowed origin", cost_usd=cost)
        content = packet.get("content")
        if content is not None and (not isinstance(content, str) or len(content.encode()) > policy.max_bytes):
            raise WebFailure("LIMIT_EXCEEDED", "Browser agent content exceeds caller budget", cost_usd=cost)
        if packet.get("status") != "ok":
            code = packet.get("failure")
            failure = WebFailure(code if isinstance(code, str) and code in FAILURES else "PROVIDER_DOWN",
                                 "Local browser agent acquisition failed", response_url=final_url, cost_usd=cost)
            failure._public_failure_evidence = _retain_failure(content, policy)
            raise failure
        if (not policy.browser_agent_task
                and config.subject_url(final_url) != config.subject_url(request.url)):
            failure = WebFailure("CONTENT_MISMATCH", "Browser agent did not observe the requested subject", response_url=final_url, cost_usd=cost)
            failure._public_failure_evidence = _retain_failure(content, policy)
            raise failure
        if not isinstance(content, str) or packet.get("content_type") != "text/html; rendered=1":
            raise WebFailure("PROVIDER_DOWN", "Browser agent omitted observed HTML", cost_usd=cost)
        if _challenge(content):
            failure = WebFailure("CAPTCHA", "Browser agent observed an access challenge", response_url=final_url, cost_usd=cost)
            failure._public_failure_evidence = _retain_failure(content, policy)
            raise failure
        content_readiness = packet.get("content_readiness")
        return {"url": final_url, "content": content, "raw": content.encode(),
                "content_type": "text/html; rendered=1",
                "http_status": None, "headers": {}, "cost_usd": cost,
                **({"content_readiness": content_readiness}
                   if content_readiness is not None else {})}
