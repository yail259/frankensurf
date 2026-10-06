"""Replaceable Browser Use provider for attested LOCAL_ONLY ``web.do``."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import os
from pathlib import Path
import tempfile

from . import browser_use_config as config
from .browser_use_provider import _read_packet, _terminate
from .web_do import (
    LOCAL_REVERSIBLE_DRAFT_CONTRACT,
    RAW_BROWSER_CONTROL_CONTRACT,
    RAW_CONTROL_REQUIRED_ACTION_CLASSES,
)


@dataclass(frozen=True)
class LocalActionExecution:
    """Private one-run executor grant; no browser state is represented here."""

    endpoint: str = field(repr=False)
    identity_scope: str = field(repr=False)
    profile_version: int
    executor_id: str


def _snapshot(value, maximum_html, maximum_image):
    if type(value) is not dict or set(value) != {
            "url", "html", "screenshot"}:
        raise ValueError()
    if (not isinstance(value["url"], str)
            or not isinstance(value["html"], str)
            or len(value["html"].encode()) > maximum_html
            or not isinstance(value["screenshot"], str)):
        raise ValueError()
    import base64
    try:
        raw = base64.b64decode(value["screenshot"], validate=True)
    except (TypeError, ValueError):
        raise ValueError() from None
    if len(raw) > maximum_image:
        raise ValueError()
    return value


class BrowserUseActionProvider:
    @property
    def manifest(self):
        from .providers import ProviderManifest
        return ProviderManifest(
            "browser_use_local_cdp_do", config.SDK_VERSION,
            rendering=True, requires_local_browser=True,
            authentication=True, navigation=True, cost_bounded=True,
            operations=("do",),
            action_classes=RAW_CONTROL_REQUIRED_ACTION_CLASSES,
            action_contracts=(LOCAL_REVERSIBLE_DRAFT_CONTRACT,
                              RAW_BROWSER_CONTROL_CONTRACT))

    def available(self, configured):
        return config.installed()

    async def acquire(self, request, services):
        from .runtime import WebFailure
        raise WebFailure("POLICY_DENIED",
            "Action-only provider cannot be used for read acquisition")

    async def perform(self, request, services):
        from .runtime import WebFailure
        if services.authenticated_action is None:
            raise WebFailure("PROVIDER_UNAVAILABLE",
                "Authenticated action service is unavailable")
        return await services.authenticated_action(request, self._execute)

    async def _execute(self, request, grant):
        from .runtime import WebFailure
        if not isinstance(grant, LocalActionExecution):
            raise WebFailure("IDENTITY_POLICY_DENIED",
                "Action executor grant is invalid")
        if not config.installed():
            raise WebFailure("PROVIDER_UNAVAILABLE",
                "Pinned Browser Use action SDK is unavailable")
        try:
            interpreter = config.python_path()
            if not interpreter.is_absolute() or not interpreter.is_file():
                raise ValueError()
        except (OSError, ValueError, config.ConfigurationError):
            raise WebFailure("PROVIDER_UNAVAILABLE",
                "Pinned Browser Use action SDK is unavailable") from None
        policy, intent = request.policy, request.intent
        origins = policy.browser_do_allowed_origins or (
            config.origin(intent.url),)
        if config.origin(intent.url) not in origins:
            raise WebFailure("POLICY_DENIED",
                "Action target is outside the WebPolicy origin scope")
        worker = Path(__file__).with_name("browser_use_action_worker.py")
        with tempfile.TemporaryDirectory(
                prefix="frankensurf-browser-use-do-") as job:
            job = Path(job)
            marker = job / "effect-started"
            payload = {
                "endpoint": grant.endpoint,
                "intent": intent.private_dict(),
                "origins": list(origins),
                "downloads_path": str(job / "downloads"),
                "effect_marker": str(marker),
                "policy": {
                    "timeout_seconds": policy.timeout_seconds,
                    "action_timeout_seconds":
                        policy.browser_do_action_timeout_seconds,
                    "settle_ms": policy.browser_do_settle_ms,
                    "max_bytes": policy.max_bytes,
                    "max_image_bytes": policy.max_image_bytes,
                    "allowed_tools": list(policy.browser_do_allowed_tools),
                },
            }
            process = await asyncio.create_subprocess_exec(
                str(interpreter), str(worker), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=os.name == "posix")
            try:
                packet = await asyncio.wait_for(_read_packet(
                    process, payload, policy.browser_do_packet_max_bytes),
                    policy.timeout_seconds
                        + policy.provider_deadline_grace_seconds)
            except TimeoutError:
                packet = {"status": "failed", "failure": "TIMEOUT",
                    "effect_started": marker.exists(), "steps": [],
                    "sdk_version": config.SDK_VERSION}
            except WebFailure:
                packet = {"status": "failed", "failure": "PROVIDER_DOWN",
                    "effect_started": marker.exists(), "steps": [],
                    "sdk_version": config.SDK_VERSION}
            finally:
                await _terminate(
                    process, policy.provider_cleanup_grace_seconds)
            marker_exists = marker.exists()
            if marker_exists:
                packet["effect_started"] = True
            self._validate_packet(packet, intent, policy, origins,
                                  marker_exists)
            return packet

    @staticmethod
    def _validate_packet(packet, intent, policy, origins, marker_exists):
        from .runtime import WebFailure, _validate_url
        allowed = {"status", "failure", "effect_started", "steps",
                   "sdk_version", "before", "after", "url"}
        if (type(packet) is not dict or set(packet) - allowed
                or packet.get("status") not in {"completed", "failed"}
                or packet.get("sdk_version") != config.SDK_VERSION
                or type(packet.get("effect_started")) is not bool
                or packet.get("effect_started") != marker_exists
                or not isinstance(packet.get("steps"), list)
                or any(type(step) is not dict
                    or set(step) != {"index", "tool", "status"}
                    or type(step["index"]) is not int
                    or step["tool"] not in policy.browser_do_allowed_tools
                    or step["status"] != "completed"
                    for step in packet.get("steps", []))):
            raise WebFailure("PROVIDER_DOWN",
                "Browser action worker returned an invalid packet")
        if (len(packet["steps"]) > len(intent.actions)
                or any(step["index"] != index
                       or step["tool"] != intent.actions[index].tool
                       for index, step in enumerate(packet["steps"]))):
            raise WebFailure("PROVIDER_DOWN",
                "Browser action worker returned an invalid step sequence")
        if (not packet["effect_started"]
                and any(intent.actions[step["index"]].mutating
                        for step in packet["steps"])):
            raise WebFailure("PROVIDER_DOWN",
                "Browser action worker returned an invalid effect boundary")
        for phase in ("before", "after"):
            if phase in packet:
                try:
                    snapshot = _snapshot(packet[phase], policy.max_bytes,
                                         policy.max_image_bytes)
                    _validate_url(snapshot["url"])
                    if config.origin(snapshot["url"]) not in origins:
                        raise ValueError()
                except (ValueError, TypeError, WebFailure):
                    raise WebFailure("PROVIDER_DOWN",
                        "Browser action worker returned invalid evidence") from None
        if packet["status"] == "completed":
            if ("failure" in packet or not marker_exists
                    or not {"before", "after", "url"} <= set(packet)
                    or len(packet["steps"]) != len(intent.actions)):
                raise WebFailure("PROVIDER_DOWN",
                    "Browser action worker returned an incomplete outcome")
            try:
                _validate_url(packet["url"])
            except WebFailure:
                raise WebFailure("PROVIDER_DOWN",
                    "Browser action worker returned an invalid URL") from None
            if packet["url"] != packet["after"]["url"]:
                raise WebFailure("PROVIDER_DOWN",
                    "Browser action worker returned an inconsistent final URL")
            if config.origin(packet["url"]) not in origins:
                raise WebFailure("POLICY_DENIED",
                    "Browser action left the policy origin scope")
        else:
            failure = packet.get("failure")
            from .browser_use_action_worker import FAILURES
            if failure not in FAILURES:
                raise WebFailure("PROVIDER_DOWN",
                    "Browser action worker returned an invalid failure")
