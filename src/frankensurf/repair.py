"""Policy-bounded public-read repair coordination.

Intelligence proposes a small declarative change at the provider edge. Core owns
the original request, retained evidence, validation, canary and promotion state.
Draft proposals are artifacts only; ordinary routing and adapters never load them.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
import hashlib
import json
import math
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass, fields, replace
from pathlib import Path

from bs4 import BeautifulSoup

WORKLOAD_ASSERTIONS_SCHEMA = "frankensurf.workload-assertions/v1"
REPAIR_INPUT_SCHEMA = "frankensurf.repair-input/v1"
REPAIR_PROPOSAL_SCHEMA = "frankensurf.repair-proposal/v1"
REPAIR_VALIDATION_SCHEMA = "frankensurf.repair-validation/v1"
REPAIR_OPERATION_SCHEMA = "frankensurf.repair-operation/v1"

_ID = re.compile(r"[a-z][a-z0-9_.-]{0,127}")
_PATH = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_TRACE = re.compile(r"[0-9a-f]{32}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class RepairPolicy:
    """Explicit authority and budgets for one public-read repair operation."""

    diagnosis_provider: str = "browser_use"
    canary_provider: str = "browser_use"
    allowed_route_providers: tuple[str, ...] | None = None
    run_live_canary: bool = False
    max_canary_attempts: int = 1
    timeout_seconds: float = 60
    allow_local_browser: bool = True
    allow_paid_fallbacks: bool = False
    max_cost_usd: float | None = None
    allowed_failure_codes: tuple[str, ...] = (
        "SCHEMA_CHANGED", "BLOCKED", "CAPTCHA", "VISUAL_REQUIRED", "TIMEOUT",
        "BUDGET_EXHAUSTED", "PROVIDER_DOWN", "PROVIDER_UNAVAILABLE",
    )
    max_fixture_bytes: int = 40 * 1024 * 1024
    max_fixture_sources: int = 4
    max_input_bytes: int = 256 * 1024
    max_proposal_bytes: int = 64 * 1024
    max_proposal_bindings: int = 16
    max_string_chars: int = 2048
    allowed_attributes: tuple[str, ...] = (
        "content", "href", "src", "value", "data-id", "data-product-id",
    )

    def __post_init__(self):
        for name in ("diagnosis_provider", "canary_provider"):
            value = getattr(self, name)
            if not isinstance(value, str) or _ID.fullmatch(value) is None:
                raise ValueError(name + " must be a valid provider ID")
        if (self.allowed_route_providers is not None
                and (type(self.allowed_route_providers) is not tuple
                     or not self.allowed_route_providers
                     or any(not isinstance(value, str)
                            or _ID.fullmatch(value) is None
                            for value in self.allowed_route_providers)
                     or len(set(self.allowed_route_providers))
                        != len(self.allowed_route_providers))):
            raise ValueError(
                "allowed_route_providers must contain distinct provider IDs")
        if type(self.run_live_canary) is not bool:
            raise ValueError("run_live_canary must be boolean")
        for name in ("max_canary_attempts", "max_fixture_bytes",
                     "max_fixture_sources", "max_proposal_bytes",
                     "max_proposal_bindings", "max_string_chars",
                     "max_input_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(name + " must be a positive integer")
        if (type(self.timeout_seconds) not in (int, float)
                or not math.isfinite(self.timeout_seconds)
                or self.timeout_seconds <= 0):
            raise ValueError("timeout_seconds must be positive and finite")
        if type(self.allow_local_browser) is not bool or type(self.allow_paid_fallbacks) is not bool:
            raise ValueError("repair provider grants must be boolean")
        if (self.max_cost_usd is not None
                and (type(self.max_cost_usd) not in (int, float)
                     or not math.isfinite(self.max_cost_usd)
                     or self.max_cost_usd < 0)):
            raise ValueError("max_cost_usd must be finite and nonnegative or None")
        if (type(self.allowed_failure_codes) is not tuple
                or not self.allowed_failure_codes
                or any(not isinstance(value, str) or not value
                       for value in self.allowed_failure_codes)
                or len(set(self.allowed_failure_codes))
                    != len(self.allowed_failure_codes)):
            raise ValueError("allowed_failure_codes must be distinct nonempty codes")
        if (type(self.allowed_attributes) is not tuple
                or any(not isinstance(value, str) or not value
                       for value in self.allowed_attributes)
                or len(set(self.allowed_attributes)) != len(self.allowed_attributes)):
            raise ValueError("allowed_attributes must be distinct nonempty names")


@dataclass(frozen=True)
class RepairProviderRequest:
    url: str
    policy: object
    trace_id: str
    context: dict
    limits: dict


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode()


def _artifact_components(value):
    if (type(value) not in (tuple, list) or not value
            or any(not isinstance(item, str)
                   or item in {"", ".", ".."}
                   or Path(item).name != item
                   for item in value)):
        raise ValueError("Private artifact path is invalid")
    return tuple(value)


def _absolute_path(value):
    return Path(os.path.abspath(os.fspath(Path(value).expanduser())))


def _artifact_path(root, components):
    return _absolute_path(root).joinpath(*_artifact_components(components))


def _owned_directory(descriptor):
    info = os.fstat(descriptor)
    owner_ok = not hasattr(os, "getuid") or info.st_uid == os.getuid()
    return stat.S_ISDIR(info.st_mode) and owner_ok


@contextmanager
def _anchored_directory(root, components=(), *, create=False, private=False):
    """Traverse beneath one state root without following any child symlink."""
    components = tuple(components)
    if components:
        _artifact_components(components)
    root = Path(root).expanduser()
    if create:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    descriptors = []
    try:
        current = os.open(root, flags)
        descriptors.append(current)
        if not _owned_directory(current):
            raise OSError("Private artifact root is not owner controlled")
        for component in components:
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=current)
                except FileExistsError:
                    pass
            current = os.open(component, flags, dir_fd=current)
            descriptors.append(current)
            if not _owned_directory(current):
                raise OSError(
                    "Private artifact directory is not owner controlled")
            if private and os.fstat(current).st_mode & 0o077:
                os.fchmod(current, 0o700)
                if os.fstat(current).st_mode & 0o077:
                    raise OSError(
                        "Private artifact directory is not private")
        yield current
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read_private_bytes(root, components, maximum):
    """Read a bounded private file through an anchored no-follow traversal."""
    components = _artifact_components(components)
    if type(maximum) is not int or maximum < 1:
        raise ValueError("Private artifact byte budget is invalid")
    with _anchored_directory(root, components[:-1]) as directory:
        descriptor = os.open(
            components[-1],
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory)
        try:
            info = os.fstat(descriptor)
            owner_ok = (not hasattr(os, "getuid")
                        or info.st_uid == os.getuid())
            if (not stat.S_ISREG(info.st_mode) or not owner_ok
                    or info.st_mode & 0o077 or info.st_size > maximum):
                raise ValueError(
                    "Private artifact ownership or permissions are invalid")
            chunks = bytearray()
            while len(chunks) <= maximum:
                chunk = os.read(
                    descriptor, min(65536, maximum + 1 - len(chunks)))
                if not chunk:
                    break
                chunks.extend(chunk)
            if (len(chunks) > maximum
                    or len(chunks) != os.fstat(descriptor).st_size):
                raise ValueError(
                    "Private artifact exceeds policy or changed while reading")
            return bytes(chunks)
        finally:
            os.close(descriptor)


def _write_private_bytes(root, components, raw):
    """Atomically replace a file beneath an anchored private directory tree."""
    components = _artifact_components(components)
    if not isinstance(raw, bytes):
        raise ValueError("Private artifact is invalid")
    filename = components[-1]
    temporary = "." + filename + "-" + uuid.uuid4().hex + ".tmp"
    with _anchored_directory(
            root, components[:-1], create=True, private=True) as directory:
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0),
                0o600, dir_fd=directory)
            try:
                view = memoryview(raw)
                while view:
                    written = os.write(descriptor, view)
                    if written < 1:
                        raise OSError(
                            "Private artifact write did not complete")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(
                temporary, filename,
                src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
    return {
        "path": str(_artifact_path(root, components)),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
    }


def _evidence_location(state_dir, value):
    try:
        path = _absolute_path(value)
        if not Path(value).expanduser().is_absolute():
            raise ValueError()
        state_root = _absolute_path(state_dir)
        state_evidence = state_root / "evidence"
        browser_parent = _absolute_path(
            Path.home() / ".local/share/frankensurf")
        browser_root = browser_parent / "browser-use-evidence"
        try:
            relative = path.relative_to(state_evidence)
            if relative.parts:
                return state_root, ("evidence", *relative.parts)
        except ValueError:
            pass
        relative = path.relative_to(browser_root)
        if relative.parts:
            return browser_parent, (
                "browser-use-evidence", *relative.parts)
    except (OSError, RuntimeError, TypeError, ValueError):
        pass
    raise ValueError("Failure evidence path is outside private evidence roots")


def _bounded_json(value, maximum):
    try:
        raw = _canonical_json(value)
        if len(raw) > maximum:
            raise ValueError()
        return json.loads(raw)
    except (TypeError, ValueError, OverflowError, UnicodeError):
        raise ValueError("JSON value exceeds its declared repair boundary") from None


def normalize_workload_assertions(value, *, max_count=32,
                                  max_bytes=64 * 1024):
    """Return a closed-schema JSON copy used by execution and later repair."""
    if value is None:
        return None
    value = _bounded_json(value, max_bytes)
    if (type(value) is not dict or set(value) != {"schema", "checks"}
            or value.get("schema") != WORKLOAD_ASSERTIONS_SCHEMA
            or type(value.get("checks")) is not list
            or not value["checks"] or len(value["checks"]) > max_count):
        raise ValueError("Invalid workload assertion set")
    normalized = []
    for check in value["checks"]:
        if type(check) is not dict:
            raise ValueError("Invalid workload assertion")
        operator = check.get("operator")
        expected_keys = ({"path", "operator"} if operator == "nonempty"
                         else {"path", "operator", "value"})
        if set(check) != expected_keys:
            raise ValueError("Invalid workload assertion")
        path = check.get("path")
        if not isinstance(path, str) or _PATH.fullmatch(path) is None:
            raise ValueError("Invalid workload assertion path")
        if operator == "minimum_count":
            if type(check.get("value")) is not int or check["value"] < 1:
                raise ValueError("Invalid workload minimum count")
        elif operator == "equals":
            if type(check.get("value")) not in (str, int, float, bool, type(None)):
                raise ValueError("Invalid workload equality value")
            if type(check.get("value")) is float and not math.isfinite(check["value"]):
                raise ValueError("Invalid workload equality value")
        elif operator != "nonempty":
            raise ValueError("Invalid workload assertion operator")
        normalized.append(dict(check))
    paths = [check["path"] for check in normalized]
    if len(set(paths)) != len(paths):
        raise ValueError("Workload assertion paths must be distinct")
    return {"schema": WORKLOAD_ASSERTIONS_SCHEMA, "checks": normalized}


def _path_value(root, path):
    value = root
    for segment in path.split("."):
        if not isinstance(value, dict) or segment not in value:
            return False, None
        value = value[segment]
    return True, value


def evaluate_workload_assertions(structured, assertions):
    if assertions is None:
        return {"schema": WORKLOAD_ASSERTIONS_SCHEMA, "status": "not_requested",
                "checks": []}
    rows = []
    for check in assertions["checks"]:
        present, value = _path_value(structured, check["path"])
        operator = check["operator"]
        if operator == "nonempty":
            passed = present and value is not None and value != "" and value != [] and value != {}
        elif operator == "equals":
            passed = present and type(value) is type(check["value"]) and value == check["value"]
        else:
            passed = (present and isinstance(value, (str, list, tuple, dict))
                      and len(value) >= check["value"])
        row = {"path": check["path"], "operator": operator,
               "passed": bool(passed),
               "observed_type": type(value).__name__ if present else "missing"}
        if present and isinstance(value, (str, list, tuple, dict)):
            row["observed_count"] = len(value)
        rows.append(row)
    return {"schema": WORKLOAD_ASSERTIONS_SCHEMA,
            "status": "passed" if all(row["passed"] for row in rows) else "failed",
            "checks": rows}


def enforce_workload_assertions(structured, assertions):
    validation = evaluate_workload_assertions(structured, assertions)
    if validation["status"] == "failed":
        from .runtime import WebFailure
        failure = WebFailure(
            "SCHEMA_CHANGED",
            "Extracted projection does not satisfy the original workload assertions")
        failure.workload_assertions = validation
        raise failure
    return validation


def _evidence_rows(receipt):
    rows = []

    def visit(value):
        if type(value) is dict:
            evidence = value.get("evidence")
            if type(evidence) is list:
                for item in evidence:
                    if (type(item) is dict and set(item) == {"path", "sha256", "bytes"}
                            and item not in rows):
                        rows.append(dict(item))
            children = value.get("children")
            if type(children) is dict:
                visit(children)
            attempts = value.get("attempts")
            if type(attempts) is list:
                for item in attempts:
                    visit(item)
        elif type(value) is list:
            for item in value:
                visit(item)

    visit(receipt)
    return rows


def store_repair_input(state_dir, trace_id, context, receipt):
    """Persist exact public failure inputs privately and return a safe reference."""
    if not isinstance(trace_id, str) or _TRACE.fullmatch(trace_id) is None:
        return None
    if receipt.get("identity") or receipt.get("action_class") != "READ_PUBLIC":
        return None
    policy_snapshot = context.get("policy")
    evidence_limit = (policy_snapshot.get("max_bytes")
                      if isinstance(policy_snapshot, dict) else None)
    if type(evidence_limit) is not int or evidence_limit < 1:
        return None
    retained_evidence = []
    for descriptor in _evidence_rows(receipt):
        try:
            _load_evidence(
                descriptor, evidence_limit, state_dir=state_dir)
            retained_evidence.append(descriptor)
        except (OSError, ValueError):
            continue
    payload = {
        "schema": REPAIR_INPUT_SCHEMA,
        "trace_id": trace_id,
        "operation": receipt.get("operation"),
        "requested_url": receipt.get("requested_url"),
        "failure": copy.deepcopy(receipt.get("failure")),
        "failure_stage": receipt.get("failure_stage"),
        "adapter": {
            "id": receipt.get("adapter"),
            "version": receipt.get("adapter_version"),
            "binding_id": receipt.get("adapter_binding_id"),
        },
        "provider": {
            "id": receipt.get("method"),
            "version": receipt.get("provider_version"),
            "binding_id": receipt.get("provider_binding_id"),
        },
        "baseline": {
            "status": receipt.get("status"),
            "latency_ms": receipt.get("latency_ms"),
            "cost_usd": receipt.get("cost_usd"),
        },
        "policy": copy.deepcopy(policy_snapshot),
        "workload_assertions": copy.deepcopy(
            context.get("workload_assertions")),
        "failure_evidence": retained_evidence,
    }
    raw = _canonical_json(payload)
    try:
        return _write_private_bytes(
            state_dir, ("repairs", "inputs", trace_id + ".json"), raw)
    except (OSError, ValueError):
        return None


def _load_trace_repair_reference(state_dir, trace_id, maximum):
    if not isinstance(trace_id, str) or _TRACE.fullmatch(trace_id) is None:
        raise ValueError("Trace does not bind an eligible repair input")
    raw = _read_private_bytes(
        state_dir, ("traces", trace_id + ".json"), maximum)
    value = json.loads(raw)
    receipt = value.get("receipt") if isinstance(value, dict) else None
    recovery = receipt.get("recovery") if isinstance(receipt, dict) else None
    reference = (recovery.get("repair_input")
                 if isinstance(recovery, dict) else None)
    if (not isinstance(receipt, dict)
            or receipt.get("trace_id") != trace_id
            or receipt.get("status") != "failed"
            or type(reference) is not dict
            or set(reference) != {"path", "sha256", "bytes"}
            or not isinstance(reference.get("path"), str)
            or not isinstance(reference.get("sha256"), str)
            or _SHA256.fullmatch(reference["sha256"]) is None
            or type(reference.get("bytes")) is not int
            or reference["bytes"] < 0):
        raise ValueError("Trace does not bind an eligible repair input")
    return reference


def _load_repair_input(state_dir, trace_id, maximum, *, expected=None):
    components = ("repairs", "inputs", trace_id + ".json")
    path = _artifact_path(state_dir, components)
    raw = _read_private_bytes(state_dir, components, maximum)
    value = json.loads(raw)
    if (type(value) is not dict or value.get("schema") != REPAIR_INPUT_SCHEMA
            or value.get("trace_id") != trace_id):
        raise ValueError("Repair input is invalid")
    artifact = {"path": str(path.absolute()),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw)}
    if expected is not None:
        try:
            expected_path = _absolute_path(expected["path"])
            actual_path = path
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            raise ValueError("Repair input trace binding is invalid") from None
        if (expected_path != actual_path
                or expected.get("sha256") != artifact["sha256"]
                or expected.get("bytes") != artifact["bytes"]):
            raise ValueError("Repair input changed after its source trace")
    return value, artifact


def _load_evidence(descriptor, maximum, *, state_dir):
    if (type(descriptor) is not dict or set(descriptor) != {"path", "sha256", "bytes"}
            or not isinstance(descriptor.get("path"), str)
            or not isinstance(descriptor.get("sha256"), str)
            or _SHA256.fullmatch(descriptor["sha256"]) is None
            or type(descriptor.get("bytes")) is not int
            or descriptor["bytes"] < 0 or descriptor["bytes"] > maximum):
        raise ValueError("Failure evidence descriptor is invalid")
    root, components = _evidence_location(
        state_dir, descriptor["path"])
    raw = _read_private_bytes(root, components, maximum)
    if (len(raw) != descriptor["bytes"]
            or hashlib.sha256(raw).hexdigest() != descriptor["sha256"]):
        raise ValueError("Failure evidence changed after the failed operation")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("Failure evidence is not a supported text fixture") from None


def _normalize_proposal(raw, policy, assertion_paths):
    raw = _bounded_json(raw, policy.max_proposal_bytes)
    if type(raw) is not dict:
        raise ValueError("Repair proposal must be an object")
    kind = raw.get("kind")
    summary = raw.get("summary")
    if (not isinstance(summary, str) or not summary.strip()
            or len(summary) > policy.max_string_chars):
        raise ValueError("Repair proposal summary is invalid")
    if kind == "adapter_patch":
        if set(raw) != {"kind", "summary", "bindings"}:
            raise ValueError("Adapter proposal schema is invalid")
        bindings = raw.get("bindings")
        if (type(bindings) is not list or not bindings
                or len(bindings) > policy.max_proposal_bindings):
            raise ValueError("Adapter proposal bindings are invalid")
        normalized = []
        for binding in bindings:
            if (type(binding) is not dict
                    or set(binding) not in (
                        {"path", "selector", "source"},
                        {"path", "selector", "source", "attribute"})):
                raise ValueError("Adapter proposal binding is invalid")
            path, selector, source = (
                binding.get("path"), binding.get("selector"),
                binding.get("source"))
            if (not isinstance(path, str) or _PATH.fullmatch(path) is None
                    or path not in assertion_paths
                    or not isinstance(selector, str) or not selector.strip()
                    or len(selector) > policy.max_string_chars
                    or source not in {"text", "attribute"}):
                raise ValueError("Adapter proposal binding is invalid")
            result = {"path": path, "selector": selector, "source": source}
            if source == "attribute":
                attribute = binding.get("attribute")
                if attribute not in policy.allowed_attributes:
                    raise ValueError("Adapter proposal attribute is not allowed")
                result["attribute"] = attribute
            elif "attribute" in binding:
                raise ValueError("Text binding cannot name an attribute")
            normalized.append(result)
        paths = [binding["path"] for binding in normalized]
        if len(set(paths)) != len(paths) or set(paths) != set(assertion_paths):
            raise ValueError("Adapter proposal must bind every original assertion exactly once")
        return {"kind": kind, "summary": summary.strip(),
                "bindings": normalized}
    if kind == "route_patch":
        if set(raw) != {"kind", "summary", "provider", "readiness_selector"}:
            raise ValueError("Route proposal schema is invalid")
        provider = raw.get("provider")
        selector = raw.get("readiness_selector")
        if (not isinstance(provider, str) or _ID.fullmatch(provider) is None
                or not isinstance(selector, str) or not selector.strip()
                or len(selector) > policy.max_string_chars):
            raise ValueError("Route proposal is invalid")
        return {"kind": kind, "summary": summary.strip(),
                "provider": provider, "readiness_selector": selector}
    raise ValueError("Repair proposal kind is unsupported")


def _set_path(root, path, value):
    current = root
    segments = path.split(".")
    for segment in segments[:-1]:
        current = current.setdefault(segment, {})
    current[segments[-1]] = value


def _adapter_candidate(content, proposal):
    soup = BeautifulSoup(content, "html.parser")
    candidate = {}
    selector_checks = []
    for binding in proposal["bindings"]:
        try:
            matches = soup.select(binding["selector"])
        except Exception:
            matches = []
        passed = len(matches) == 1
        value = None
        if passed:
            element = matches[0]
            value = (element.get_text(" ", strip=True)
                     if binding["source"] == "text"
                     else element.get(binding["attribute"]))
            passed = isinstance(value, str) and bool(value.strip())
            if passed:
                value = value.strip()
                _set_path(candidate, binding["path"], value)
        selector_checks.append({
            "path": binding["path"], "selector": binding["selector"],
            "matched": len(matches), "passed": bool(passed),
        })
    return candidate, selector_checks


def _validate_adapter_content(content, proposal, assertions, label):
    candidate, selector_checks = _adapter_candidate(content, proposal)
    assertions_result = evaluate_workload_assertions(candidate, assertions)
    passed = (all(row["passed"] for row in selector_checks)
              and assertions_result["status"] == "passed")
    return {"source": label, "status": "passed" if passed else "failed",
            "selector_checks": selector_checks,
            "assertions": assertions_result}


def _validate_route_content(runtime, content, content_type, url, adapter,
                            proposal, assertions, label, policy):
    soup = BeautifulSoup(content, "html.parser")
    try:
        readiness_count = len(soup.select(proposal["readiness_selector"]))
    except Exception:
        readiness_count = 0
    projection = None
    assertion_result = {"schema": WORKLOAD_ASSERTIONS_SCHEMA,
                        "status": "failed", "checks": []}
    if readiness_count and adapter:
        try:
            from .adapters import AdapterRequest
            projection = runtime.adapters.project(adapter, AdapterRequest(
                content, content_type, url, policy=policy,
                requested_url=url))
            assertion_result = evaluate_workload_assertions(
                projection.get("structured"), assertions)
        except Exception:
            projection = None
    passed = readiness_count > 0 and projection is not None and assertion_result["status"] == "passed"
    return {"source": label, "status": "passed" if passed else "failed",
            "readiness_selector": proposal["readiness_selector"],
            "readiness_matches": readiness_count,
            "assertions": assertion_result}


def _policy_from_snapshot(snapshot):
    from .runtime import WebPolicy
    if type(snapshot) is not dict:
        raise ValueError("Original policy snapshot is invalid")
    known = {item.name: item for item in fields(WebPolicy)}
    if set(snapshot) - set(known):
        raise ValueError("Original policy snapshot has unknown fields")
    values = dict(snapshot)
    prototype = WebPolicy()
    optional_tuple_fields = {
        "provider_candidates",
        "search_source_candidates", "search_source_allow",
        "browser_agent_allowed_origins",
    }
    for name, value in list(values.items()):
        if (isinstance(value, list)
                and (isinstance(getattr(prototype, name), tuple)
                     or name in optional_tuple_fields)):
            values[name] = tuple(value)
    return WebPolicy(**values)


def _json_artifact_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      indent=2, allow_nan=False).encode()


def _write_json_artifact(state_dir, components, value):
    raw = _json_artifact_bytes(value)
    return _write_private_bytes(state_dir, components, raw)


def _failure_result(trace_id, code, message, started):
    return {
        "schema": REPAIR_OPERATION_SCHEMA,
        "status": "failed",
        "trace_id": trace_id,
        "proposal": None,
        "validation": None,
        "promotion": {"state": "not_eligible", "automatic": False},
        "receipt": {
            "trace_id": uuid.uuid4().hex,
            "operation": "repair",
            "action_class": "READ_PUBLIC",
            "status": "failed",
            "failure": {"code": code, "message": message},
            "latency_ms": round((time.monotonic() - started) * 1000),
        },
    }


def _repair_cost(values):
    values = list(values)
    if not values or any(value is None for value in values):
        return None
    if any(type(value) not in (int, float) or not math.isfinite(value)
           or value < 0 for value in values):
        raise ValueError("Repair provider returned an invalid cost")
    return sum(values)


async def run_repair(runtime, trace_id, policy=None):
    """Diagnose and validate one retained public failure without promotion."""
    from .providers import ProviderRequest
    from .runtime import WebFailure, _validate_url, utcnow

    started = time.monotonic()
    policy = policy or RepairPolicy()
    if not isinstance(policy, RepairPolicy):
        raise ValueError("policy must be a RepairPolicy")
    if not isinstance(trace_id, str) or _TRACE.fullmatch(trace_id) is None:
        raise ValueError("Invalid trace identifier")
    try:
        source_reference = _load_trace_repair_reference(
            runtime.state_dir, trace_id,
            max(policy.max_input_bytes, policy.max_fixture_bytes))
        repair_input, input_artifact = _load_repair_input(
            runtime.state_dir, trace_id, policy.max_input_bytes,
            expected=source_reference)
        failure_code = (repair_input.get("failure") or {}).get("code")
        if failure_code not in policy.allowed_failure_codes:
            return _failure_result(trace_id, "REPAIR_NOT_ELIGIBLE",
                "The retained public failure is outside repair policy", started)
        if repair_input.get("operation") not in {"read", "extract"}:
            return _failure_result(trace_id, "REPAIR_NOT_ELIGIBLE",
                "Only public read and extraction failures are repairable", started)
        assertions = normalize_workload_assertions(
            repair_input.get("workload_assertions"),
            max_count=policy.max_proposal_bindings,
            max_bytes=policy.max_proposal_bytes)
        if assertions is None:
            return _failure_result(trace_id, "REPAIR_ASSERTIONS_REQUIRED",
                "Repair needs assertions retained by the original workload", started)
        evidence_rows = repair_input.get("failure_evidence")
        if type(evidence_rows) is not list or not evidence_rows:
            return _failure_result(trace_id, "REPAIR_EVIDENCE_REQUIRED",
                "The failed operation retained no public fixture", started)
        fixtures = []
        for descriptor in evidence_rows[:policy.max_fixture_sources]:
            try:
                fixtures.append((descriptor, _load_evidence(
                    descriptor, policy.max_fixture_bytes,
                    state_dir=runtime.state_dir)))
            except (OSError, ValueError):
                continue
        if not fixtures:
            return _failure_result(trace_id, "REPAIR_EVIDENCE_INVALID",
                "Retained public failure evidence is unavailable or changed", started)

        original_policy = _policy_from_snapshot(repair_input.get("policy"))
        if original_policy.identity:
            return _failure_result(trace_id, "REPAIR_NOT_ELIGIBLE",
                "Named identity repair requires owner authority", started)
        diagnosis_policy = replace(
            original_policy,
            action_classes=("READ_PUBLIC",),
            identity=None,
            provider=policy.diagnosis_provider,
            provider_candidates=None,
            freshness="now",
            allow_local_browser=policy.allow_local_browser,
            allow_paid_fallbacks=policy.allow_paid_fallbacks,
            max_cost_usd=policy.max_cost_usd,
            timeout_seconds=policy.timeout_seconds,
            retain_public_failure_evidence=True,
        )
        requested_url = repair_input.get("requested_url")
        if not isinstance(requested_url, str):
            raise ValueError("Repair input URL is invalid")
        context = {
            "failure": copy.deepcopy(repair_input.get("failure")),
            "failure_stage": repair_input.get("failure_stage"),
            "operation": repair_input.get("operation"),
            "adapter": copy.deepcopy(repair_input.get("adapter")),
            "workload_assertions": copy.deepcopy(assertions),
            "available_providers": [
                item["id"] for item in runtime.providers.inspect()
                if item.get("enabled")],
        }
        provider_request = RepairProviderRequest(
            requested_url, diagnosis_policy, trace_id, context,
            {"max_proposal_bytes": policy.max_proposal_bytes,
             "max_string_chars": policy.max_string_chars,
             "max_proposal_bindings": policy.max_proposal_bindings})
        diagnosis_manifest = runtime.providers.require_enabled(
            policy.diagnosis_provider, diagnosis_policy)
        diagnosis_started = time.monotonic()
        diagnosis = await runtime.providers.diagnose(
            policy.diagnosis_provider, provider_request,
            runtime._provider_services())
        diagnosis_latency_ms = round(
            (time.monotonic() - diagnosis_started) * 1000)
        if type(diagnosis) is not dict:
            raise WebFailure("PROVIDER_DOWN",
                             "Repair diagnosis plugin returned an invalid result")
        allowed = {"url", "content", "raw", "content_type", "http_status",
                   "headers", "cost_usd", "proposal"}
        if set(diagnosis) - allowed:
            raise WebFailure("PROVIDER_DOWN",
                             "Repair diagnosis plugin returned unknown fields")
        content = diagnosis.get("content")
        raw = diagnosis.get("raw")
        if (not isinstance(content, str) or not isinstance(raw, bytes)
                or raw != content.encode()
                or len(raw) > min(
                    policy.max_fixture_bytes, diagnosis_policy.max_bytes)
                or diagnosis.get("content_type") != "text/html; rendered=1"
                or not isinstance(diagnosis.get("url"), str)
                or diagnosis.get("http_status") is not None
                    and (type(diagnosis["http_status"]) is not int
                         or not 100 <= diagnosis["http_status"] <= 599)
                or type(diagnosis.get("headers")) is not dict):
            raise WebFailure("PROVIDER_DOWN",
                             "Repair diagnosis plugin omitted current browser evidence")
        _validate_url(diagnosis["url"])
        diagnosis_cost = _repair_cost([diagnosis.get("cost_usd")])
        if (policy.max_cost_usd is not None
                and (diagnosis_cost is None
                     or diagnosis_cost > policy.max_cost_usd)):
            raise WebFailure(
                "BUDGET_EXHAUSTED",
                "Repair diagnosis cannot satisfy the aggregate cost cap")
        diagnosis_evidence = runtime._save_bytes(raw, ".html")
        assertion_paths = [check["path"] for check in assertions["checks"]]
        normalized = _normalize_proposal(
            diagnosis.get("proposal"), policy, assertion_paths)
        target = {
            "operation": repair_input["operation"],
            "url": requested_url,
            "failure_trace_id": trace_id,
            "repair_input_sha256": input_artifact["sha256"],
            "identity_class": "public",
            "adapter": copy.deepcopy(repair_input.get("adapter")),
        }
        proposal_body = {
            "schema": REPAIR_PROPOSAL_SCHEMA,
            "version": 1,
            "kind": normalized["kind"],
            "target": target,
            "change": normalized,
        }
        proposal_id = hashlib.sha256(_canonical_json(proposal_body)).hexdigest()
        proposal = {**proposal_body, "id": proposal_id,
                    "state": "diagnosed"}

        validations = []
        adapter_id = (repair_input.get("adapter") or {}).get("id")
        if normalized["kind"] == "adapter_patch":
            for _, fixture in fixtures:
                validations.append(_validate_adapter_content(
                    fixture, normalized, assertions, "retained_failure_fixture"))
            validations.append(_validate_adapter_content(
                content, normalized, assertions, "diagnosis_live_observation"))
        else:
            if (policy.allowed_route_providers is not None
                    and normalized["provider"]
                    not in policy.allowed_route_providers):
                raise ValueError(
                    "Route proposal names a provider outside repair policy")
            runtime.providers.require_enabled(
                normalized["provider"], diagnosis_policy)
            if normalized["provider"] == policy.diagnosis_provider:
                validations.append(_validate_route_content(
                    runtime, content, diagnosis["content_type"], diagnosis["url"],
                    adapter_id, normalized, assertions,
                    "diagnosis_live_observation", diagnosis_policy))

        fixture_passed = any(
            item["source"] == "retained_failure_fixture"
            and item["status"] == "passed" for item in validations)
        live_passed = any(
            item["source"] == "diagnosis_live_observation"
            and item["status"] == "passed" for item in validations)
        if normalized["kind"] == "route_patch":
            fixture_passed = True
            if normalized["provider"] != policy.diagnosis_provider:
                live_passed = True
        validation_status = "passed" if fixture_passed and live_passed else "failed"

        canary_provider = (normalized["provider"]
                           if normalized["kind"] == "route_patch"
                           else policy.canary_provider)
        canary = {
            "status": "not_requested",
            "provider": canary_provider,
            "attempts": [],
            "independent_from_diagnosis": (
                canary_provider != policy.diagnosis_provider),
        }
        if validation_status == "passed" and policy.run_live_canary:
            canary["status"] = "failed"
            canary_costs = []
            for index in range(policy.max_canary_attempts):
                spent_canary = _repair_cost(canary_costs)
                if (policy.max_cost_usd is not None
                        and spent_canary is None and canary_costs):
                    canary["attempts"].append({
                        "index": index + 1, "status": "failed",
                        "provider": canary_provider, "latency_ms": 0,
                        "cost_usd": None,
                        "failure": {"code": "BUDGET_EXHAUSTED",
                                    "message": "Prior canary cost is unknown"}})
                    break
                remaining_cost = (
                    None if policy.max_cost_usd is None
                    else policy.max_cost_usd - diagnosis_cost
                        - (spent_canary or 0))
                if remaining_cost is not None and remaining_cost < 0:
                    canary["attempts"].append({
                        "index": index + 1, "status": "failed",
                        "provider": canary_provider, "latency_ms": 0,
                        "cost_usd": 0,
                        "failure": {"code": "BUDGET_EXHAUSTED",
                                    "message": "Aggregate repair cost cap is exhausted"}})
                    break
                canary_policy = replace(
                    diagnosis_policy, provider=canary_provider,
                    provider_candidates=None, content_ready_selector=(
                        normalized.get("readiness_selector")
                        if normalized["kind"] == "route_patch"
                        else diagnosis_policy.content_ready_selector),
                    max_cost_usd=remaining_cost)
                canary_started = time.monotonic()
                attempt_cost_recorded = False
                try:
                    response = await runtime.providers.acquire(
                        canary_provider,
                        ProviderRequest(requested_url, canary_policy),
                        runtime._provider_services())
                    canary_evidence = runtime._save_bytes(
                        response["raw"], ".html" if "html" in response["content_type"]
                        else ".json")
                    if normalized["kind"] == "adapter_patch":
                        check = _validate_adapter_content(
                            response["content"], normalized, assertions,
                            "independent_live_canary")
                    else:
                        check = _validate_route_content(
                            runtime, response["content"], response["content_type"],
                            response["url"], adapter_id, normalized, assertions,
                            "independent_live_canary", canary_policy)
                    canary_cost = _repair_cost([
                        response.get("cost_usd")])
                    canary_costs.append(canary_cost)
                    attempt_cost_recorded = True
                    if (remaining_cost is not None
                            and (canary_cost is None
                                 or canary_cost > remaining_cost)):
                        raise WebFailure(
                            "BUDGET_EXHAUSTED",
                            "Repair canary cannot satisfy the remaining cost cap",
                            cost_usd=canary_cost)
                    canary["attempts"].append({
                        "index": index + 1, "status": check["status"],
                        "provider": canary_provider,
                        "latency_ms": round(
                            (time.monotonic() - canary_started) * 1000),
                        "cost_usd": canary_cost,
                        "evidence": [canary_evidence], "validation": check})
                    if check["status"] == "passed":
                        canary["status"] = "passed"
                        break
                except WebFailure as error:
                    error_cost = getattr(error, "cost_usd", None)
                    if not attempt_cost_recorded:
                        canary_costs.append(error_cost)
                    canary["attempts"].append({
                        "index": index + 1, "status": "failed",
                        "provider": canary_provider,
                        "latency_ms": round(
                            (time.monotonic() - canary_started) * 1000),
                        "cost_usd": error_cost,
                        "failure": {"code": error.code,
                                    "message": error.message}})
        if validation_status == "failed":
            state = "validation_failed"
            promotion_state = "not_eligible"
        elif (canary["status"] == "passed"
              and normalized["kind"] == "adapter_patch"
              and canary["independent_from_diagnosis"]):
            state = "canary_validated"
            promotion_state = "awaiting_explicit_promotion"
        elif (canary["status"] == "passed"
              and normalized["kind"] == "adapter_patch"):
            state = "canary_not_independent"
            promotion_state = "not_eligible"
        elif canary["status"] == "passed":
            state = "canary_validated"
            promotion_state = "proposal_only"
        elif policy.run_live_canary:
            state = "canary_failed"
            promotion_state = "not_eligible"
        else:
            state = "fixture_validated"
            promotion_state = "awaiting_live_canary"
        proposal["state"] = state
        passed_canary = next((
            attempt for attempt in canary["attempts"]
            if attempt.get("status") == "passed"), None)
        performance_comparison = {
            "status": "recorded" if passed_canary else "not_available",
            "baseline": copy.deepcopy(repair_input.get("baseline")),
            "diagnosis": {
                "provider": policy.diagnosis_provider,
                "latency_ms": diagnosis_latency_ms,
                "cost_usd": diagnosis_cost,
            },
            "candidate_canary": (
                {key: passed_canary.get(key) for key in (
                    "provider", "latency_ms", "cost_usd")}
                if passed_canary else None),
            "interpretation": (
                "Recorded measurements only; no superiority or reliability claim"),
        }
        validation = {
            "schema": REPAIR_VALIDATION_SCHEMA,
            "proposal_id": proposal_id,
            "repair_input_sha256": input_artifact["sha256"],
            "status": validation_status,
            "original_assertions": copy.deepcopy(assertions),
            "checks": validations,
            "live_canary": canary,
            "performance_comparison": performance_comparison,
        }
        proposal["validation_sha256"] = hashlib.sha256(
            _json_artifact_bytes(validation)).hexdigest()
        promotion = {
            "state": promotion_state,
            "automatic": False,
            "ordinary_execution_changed": False,
            "required_action": (
                "Run an independent live canary"
                if promotion_state == "awaiting_live_canary"
                else "Explicit operator promotion after benchmark comparison"
                if promotion_state == "awaiting_explicit_promotion"
                else "Route proposals require a route-overlay activation implementation"
                if promotion_state == "proposal_only"
                else "Revise and revalidate the proposal"),
        }
        proposal_artifact = _write_json_artifact(
            runtime.state_dir,
            ("repairs", "proposals", proposal_id, "proposal.json"),
            proposal)
        validation_artifact = _write_json_artifact(
            runtime.state_dir,
            ("repairs", "proposals", proposal_id, "validation.json"),
            validation)
        receipt = {
            "trace_id": uuid.uuid4().hex,
            "operation": "repair",
            "action_class": "READ_PUBLIC",
            "status": "observed",
            "observed_at": utcnow(),
            "latency_ms": round((time.monotonic() - started) * 1000),
            "provider": policy.diagnosis_provider,
            "provider_version": diagnosis_manifest.version,
            "provider_binding_id": runtime.providers.binding_id(
                policy.diagnosis_provider),
            "cost_usd": _repair_cost([
                diagnosis_cost,
                *[attempt.get("cost_usd")
                  for attempt in canary["attempts"]],
            ]),
            "diagnosis_cost_usd": diagnosis_cost,
            "canary_cost_usd": _repair_cost([
                attempt.get("cost_usd")
                for attempt in canary["attempts"]]),
            "source_trace_id": trace_id,
            "input_artifact": input_artifact,
            "failure_evidence": [row for row, _ in fixtures],
            "diagnosis_evidence": [diagnosis_evidence],
            "proposal_artifact": proposal_artifact,
            "validation_artifact": validation_artifact,
            "automatic_promotion": False,
        }
        result = {
            "schema": REPAIR_OPERATION_SCHEMA,
            "status": "proposal_ready" if validation_status == "passed"
                      else "validation_failed",
            "trace_id": trace_id,
            "proposal": proposal,
            "validation": validation,
            "promotion": promotion,
            "receipt": receipt,
        }
        _write_json_artifact(
            runtime.state_dir,
            ("repairs", "operations", receipt["trace_id"] + ".json"),
            result)
        return result
    except WebFailure as error:
        return _failure_result(trace_id, error.code, error.message, started)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return _failure_result(trace_id, "REPAIR_INPUT_INVALID",
            "Repair inputs, evidence or proposal failed Core validation", started)


ACTIVE_OVERLAYS_SCHEMA = "frankensurf.active-repair-overlays/v1"


@dataclass(frozen=True)
class PromotionPolicy:
    """Local owner policy for atomic activation and disable operations."""

    allowed_kinds: tuple[str, ...] = ("adapter_patch", "module_patch")
    max_reason_chars: int = 2048
    max_registry_bytes: int = 4 * 1024 * 1024
    max_revalidation_bytes: int = 40 * 1024 * 1024

    def __post_init__(self):
        if (type(self.allowed_kinds) is not tuple
                or not self.allowed_kinds
                or any(value not in {"adapter_patch", "route_patch", "module_patch"}
                       for value in self.allowed_kinds)
                or len(set(self.allowed_kinds)) != len(self.allowed_kinds)):
            raise ValueError("allowed_kinds must contain distinct repair kinds")
        if (type(self.max_reason_chars) is not int
                or self.max_reason_chars < 1
                or type(self.max_registry_bytes) is not int
                or self.max_registry_bytes < 1
                or type(self.max_revalidation_bytes) is not int
                or self.max_revalidation_bytes < 1):
            raise ValueError("promotion limits must be positive integers")


def _registry_path(state_dir):
    return _artifact_path(state_dir, ("repairs", "active.json"))


def _load_overlay_registry(state_dir, maximum=None):
    try:
        raw = _read_private_bytes(
            state_dir, ("repairs", "active.json"),
            maximum or 4 * 1024 * 1024)
    except FileNotFoundError:
        return {
            "schema": ACTIVE_OVERLAYS_SCHEMA,
            "revision": 0,
            "overlays": {},
            "history": [],
        }
    value = json.loads(raw)
    if (type(value) is not dict
            or set(value) != {
                "schema", "revision", "overlays", "history"}
            or value.get("schema") != ACTIVE_OVERLAYS_SCHEMA
            or type(value.get("revision")) is not int
            or value["revision"] < 0
            or type(value.get("overlays")) is not dict
            or type(value.get("history")) is not list):
        raise ValueError("Repair overlay registry is invalid")
    return value


def _atomic_overlay_registry(state_dir, value, maximum):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     indent=2, allow_nan=False).encode()
    if len(raw) > maximum:
        raise ValueError("Repair overlay registry exceeds owner policy")
    return _write_private_bytes(
        state_dir, ("repairs", "active.json"), raw)


@contextmanager
def _overlay_registry_transaction(state_dir):
    """Serialize each registry read-modify-replace transaction."""
    import fcntl

    with _anchored_directory(
            state_dir, ("repairs",), create=True, private=True) as directory:
        descriptor = os.open(
            ".active.lock",
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600, dir_fd=directory)
        try:
            info = os.fstat(descriptor)
            owner_ok = (not hasattr(os, "getuid")
                        or info.st_uid == os.getuid())
            if (not stat.S_ISREG(info.st_mode) or not owner_ok
                    or info.st_mode & 0o077):
                raise OSError("Repair registry lock is not owner private")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _proposal_id(value):
    if (type(value) is not dict
            or value.get("schema") != REPAIR_PROPOSAL_SCHEMA
            or value.get("version") != 1):
        raise ValueError("Repair proposal version is unsupported")
    body = {key: value[key] for key in (
        "schema", "version", "kind", "target", "change")}
    return hashlib.sha256(_canonical_json(body)).hexdigest()


def promote_repair(runtime, proposal_id, expected_sha256, policy=None):
    """Atomically activate a canary-validated declarative adapter overlay."""
    from .runtime import utcnow

    policy = policy or PromotionPolicy()
    if not isinstance(policy, PromotionPolicy):
        raise ValueError("policy must be a PromotionPolicy")
    if (not isinstance(proposal_id, str)
            or _SHA256.fullmatch(proposal_id) is None
            or not isinstance(expected_sha256, str)
            or _SHA256.fullmatch(expected_sha256) is None):
        raise ValueError("Promotion requires proposal ID and SHA-256")
    proposal_raw = _read_private_bytes(
        runtime.state_dir,
        ("repairs", "proposals", proposal_id, "proposal.json"),
        policy.max_revalidation_bytes)
    observed_hash = hashlib.sha256(proposal_raw).hexdigest()
    if observed_hash != expected_sha256:
        raise ValueError("Proposal hash does not match explicit promotion request")
    proposal = json.loads(proposal_raw)
    if (type(proposal) is not dict
            or proposal.get("id") != proposal_id
            or _proposal_id(proposal) != proposal_id
            or proposal.get("state") != "canary_validated"
            or proposal.get("kind") not in policy.allowed_kinds):
        raise ValueError("Proposal is not eligible for promotion")
    if proposal["kind"] == "module_patch":
        return _promote_module_patch(runtime, proposal_id, proposal, expected_sha256, policy)
    if proposal["kind"] != "adapter_patch":
        raise ValueError("This release activates declarative adapter overlays only")
    validation_raw = _read_private_bytes(
        runtime.state_dir,
        ("repairs", "proposals", proposal_id, "validation.json"),
        policy.max_revalidation_bytes)
    validation = json.loads(validation_raw)
    if (type(validation) is not dict
            or validation.get("schema") != REPAIR_VALIDATION_SCHEMA
            or validation.get("proposal_id") != proposal_id
            or proposal.get("validation_sha256")
                != hashlib.sha256(validation_raw).hexdigest()
            or validation.get("repair_input_sha256")
                != (proposal.get("target") or {}).get(
                    "repair_input_sha256")
            or validation.get("status") != "passed"
            or (validation.get("live_canary") or {}).get("status")
                != "passed"
            or not (validation.get("live_canary") or {}).get(
                "independent_from_diagnosis")
            or (validation.get("performance_comparison") or {}).get(
                "status") != "recorded"):
        raise ValueError(
            "Promotion requires fixture, independent canary and performance validation")
    target = proposal.get("target")
    adapter = target.get("adapter") if isinstance(target, dict) else None
    adapter_id = adapter.get("id") if isinstance(adapter, dict) else None
    if (not isinstance(target, dict)
            or not isinstance(adapter, dict)
            or target.get("identity_class") != "public"
            or target.get("operation") != "extract"
            or not isinstance(adapter_id, str)
            or runtime.adapters.require_enabled(adapter_id).version
                != adapter.get("version")
            or runtime.adapters.binding_id(adapter_id)
                != adapter.get("binding_id")):
        raise ValueError("Target adapter binding changed after validation")
    assertions = normalize_workload_assertions(
        validation.get("original_assertions"))
    source_trace_id = target.get("failure_trace_id")
    source_reference = _load_trace_repair_reference(
        runtime.state_dir, source_trace_id,
        policy.max_revalidation_bytes)
    input_value, input_artifact = _load_repair_input(
        runtime.state_dir, source_trace_id,
        policy.max_revalidation_bytes, expected=source_reference)
    if input_artifact["sha256"] != target.get("repair_input_sha256"):
        raise ValueError("Repair input changed after proposal validation")
    fixtures = []
    for descriptor in input_value.get("failure_evidence", []):
        try:
            fixtures.append(_load_evidence(
                descriptor, policy.max_revalidation_bytes,
                state_dir=runtime.state_dir))
        except (OSError, ValueError):
            continue
    if not fixtures or not any(
            _validate_adapter_content(
                fixture, proposal["change"], assertions,
                "promotion_fixture_revalidation")["status"] == "passed"
            for fixture in fixtures):
        raise ValueError("Retained fixture no longer validates the proposal")
    passed_canaries = [
        attempt for attempt in validation["live_canary"].get("attempts", [])
        if attempt.get("status") == "passed"]
    canary_descriptors = [
        descriptor for attempt in passed_canaries
        for descriptor in attempt.get("evidence", [])]
    canary_sources = []
    for descriptor in canary_descriptors:
        canary_sources.append(_load_evidence(
            descriptor, policy.max_revalidation_bytes,
            state_dir=runtime.state_dir))
    if not canary_sources or not any(
            _validate_adapter_content(
                source, proposal["change"], assertions,
                "promotion_canary_revalidation")["status"] == "passed"
            for source in canary_sources):
        raise ValueError("Live canary evidence no longer validates the proposal")

    with _overlay_registry_transaction(runtime.state_dir):
        registry = _load_overlay_registry(
            runtime.state_dir, policy.max_registry_bytes)
        previous = copy.deepcopy(registry["overlays"].get(proposal_id))
        now = utcnow()
        overlay = {
            "id": proposal_id,
            "status": "active",
            "proposal_id": proposal_id,
            "proposal_version": proposal["version"],
            "proposal_sha256": expected_sha256,
            "validation_sha256": hashlib.sha256(
                validation_raw).hexdigest(),
            "activated_at": now,
            "disabled_at": None,
            "disable_reason": None,
            "target": copy.deepcopy(target),
            "change": copy.deepcopy(proposal["change"]),
            "assertions": copy.deepcopy(assertions),
        }
        registry["revision"] += 1
        registry["overlays"][proposal_id] = overlay
        registry["history"].append({
            "event": "activate",
            "overlay_id": proposal_id,
            "at": now,
            "revision": registry["revision"],
            "previous_status": (
                previous.get("status") if isinstance(previous, dict)
                else None),
            "proposal_sha256": expected_sha256,
        })
        registry_artifact = _atomic_overlay_registry(
            runtime.state_dir, registry, policy.max_registry_bytes)
    return {
        "status": "promoted",
        "overlay": copy.deepcopy(overlay),
        "registry_revision": registry["revision"],
        "registry_artifact": registry_artifact,
        "automatic": False,
        "rollback": {
            "operation": "disable-repair",
            "overlay_id": proposal_id,
        },
    }


def _promote_module_patch(runtime, proposal_id, proposal, expected_sha256, policy):
    """Owner promotion of an agent-proposed site module version (see site_modules.py)."""
    from .runtime import utcnow
    from .site_modules import promote_module_patch
    validation_raw = _read_private_bytes(
        runtime.state_dir, ("repairs", "proposals", proposal_id, "validation.json"),
        policy.max_revalidation_bytes)
    validation = json.loads(validation_raw)
    if (type(validation) is not dict
            or validation.get("schema") != REPAIR_VALIDATION_SCHEMA
            or validation.get("proposal_id") != proposal_id
            or proposal.get("validation_sha256") != hashlib.sha256(validation_raw).hexdigest()):
        raise ValueError("Module repair validation changed after the proposal")
    activated = promote_module_patch(runtime, proposal, validation, validation_raw, expected_sha256)
    with _overlay_registry_transaction(runtime.state_dir):
        registry = _load_overlay_registry(runtime.state_dir, policy.max_registry_bytes)
        now = utcnow()
        overlay = {
            "id": proposal_id, "status": "active", "kind": "module_patch",
            "proposal_id": proposal_id, "proposal_version": proposal["version"],
            "proposal_sha256": expected_sha256,
            "validation_sha256": hashlib.sha256(validation_raw).hexdigest(),
            "activated_at": now, "disabled_at": None, "disable_reason": None,
            "target": copy.deepcopy(proposal["target"]), "change": copy.deepcopy(proposal["change"]),
            "module": activated,
        }
        registry["revision"] += 1
        registry["overlays"][proposal_id] = overlay
        registry["history"].append({"event": "activate", "overlay_id": proposal_id, "at": now,
                                    "revision": registry["revision"], "kind": "module_patch",
                                    "proposal_sha256": expected_sha256})
        registry_artifact = _atomic_overlay_registry(runtime.state_dir, registry, policy.max_registry_bytes)
    return {"status": "promoted", "module": activated, "registry_revision": registry["revision"],
            "registry_artifact": registry_artifact, "automatic": False,
            "rollback": {"operation": "disable-repair", "overlay_id": proposal_id,
                         "restores_version": activated["previous"]["version"]}}


def disable_repair(runtime, overlay_id, reason, policy=None):
    """Atomically disable an active overlay while retaining rollback history."""
    from .runtime import utcnow

    policy = policy or PromotionPolicy()
    if not isinstance(policy, PromotionPolicy):
        raise ValueError("policy must be a PromotionPolicy")
    if (not isinstance(overlay_id, str)
            or _SHA256.fullmatch(overlay_id) is None
            or not isinstance(reason, str) or not reason.strip()
            or len(reason) > policy.max_reason_chars):
        raise ValueError("Disable operation requires a valid overlay and reason")
    with _overlay_registry_transaction(runtime.state_dir):
        registry = _load_overlay_registry(
            runtime.state_dir, policy.max_registry_bytes)
        overlay = registry["overlays"].get(overlay_id)
        if not isinstance(overlay, dict) or overlay.get("status") != "active":
            raise ValueError("Repair overlay is not active")
        if overlay.get("kind") == "module_patch":
            # Roll the site module back to the version the patch replaced, but
            # never over a version someone saved after the promotion.
            current = runtime.site_modules.get(overlay["module"]["id"])
            if current.fingerprint != overlay["module"]["sha256"]:
                raise ValueError("The module changed after promotion; edit it directly instead")
            runtime.site_modules.put(overlay["target"]["module"]["base_record"])
        now = utcnow()
        overlay["status"] = "disabled"
        overlay["disabled_at"] = now
        overlay["disable_reason"] = reason.strip()
        registry["revision"] += 1
        registry["history"].append({
            "event": "disable",
            "overlay_id": overlay_id,
            "at": now,
            "revision": registry["revision"],
            "reason": reason.strip(),
            "rollback_of_revision": registry["revision"] - 1,
        })
        registry_artifact = _atomic_overlay_registry(
            runtime.state_dir, registry, policy.max_registry_bytes)
    return {
        "status": "disabled",
        "overlay_id": overlay_id,
        "disabled_at": now,
        "reason": reason.strip(),
        "registry_revision": registry["revision"],
        "registry_artifact": registry_artifact,
    }


def _candidate_text(candidate):
    values = []

    def visit(value):
        if isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, str) and value not in values:
            values.append(value)

    visit(candidate)
    return "\n".join(values)


def active_overlay_projection(state_dir, adapter_id, adapter_version,
                              adapter_binding_id, requested_url, content,
                              assertions, *, identity_class,
                              max_registry_bytes):
    """Project through one exact active overlay, or return None."""
    try:
        registry = _load_overlay_registry(
            state_dir, max_registry_bytes)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    matches = []
    for overlay in registry["overlays"].values():
        if not isinstance(overlay, dict):
            continue
        target = overlay.get("target") or {}
        target_adapter = target.get("adapter") or {}
        if (isinstance(target, dict)
                and isinstance(target_adapter, dict)
                and overlay.get("status") == "active"
                and target.get("identity_class") == identity_class == "public"
                and target.get("operation") == "extract"
                and target.get("url") == requested_url
                and target_adapter.get("id") == adapter_id
                and target_adapter.get("version") == adapter_version
                and target_adapter.get("binding_id")
                    == adapter_binding_id
                and overlay.get("change", {}).get("kind")
                    == "adapter_patch"):
            matches.append(overlay)
    if len(matches) != 1:
        return None
    overlay = matches[0]
    try:
        stored_assertions = normalize_workload_assertions(
            overlay.get("assertions"))
        candidate, selector_checks = _adapter_candidate(
            content, overlay["change"])
    except (KeyError, TypeError, ValueError):
        return None
    if assertions != stored_assertions:
        return None
    validation = evaluate_workload_assertions(candidate, stored_assertions)
    if (not all(row["passed"] for row in selector_checks)
            or validation["status"] != "passed"):
        return None
    projection = {
        "text": _candidate_text(candidate),
        "structured": candidate,
        "image_urls": [],
    }
    metadata = {
        "id": overlay["id"],
        "proposal_id": overlay["proposal_id"],
        "proposal_version": overlay["proposal_version"],
        "proposal_sha256": overlay["proposal_sha256"],
        "activated_at": overlay["activated_at"],
        "registry_revision": registry["revision"],
        "status": "active",
    }
    return projection, metadata, validation
