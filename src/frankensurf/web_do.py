"""Typed ``web.do`` requests and the private local idempotency journal.

Action values are execution inputs.  Public plans, receipts and journal keys do
not contain field values, cookies, credentials, or browser profile material.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit

from .actions import ACTION_CLASSES, READ_ACTION_CLASSES
from .browser_use_config import origin


ACTION_TOOLS = (
    "fill", "click", "wait_for", "assert_text", "assert_value")
_MUTATING_TOOLS = frozenset({"fill", "click"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_IDEMPOTENCY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*\Z")
LOCAL_REVERSIBLE_DRAFT_CONTRACT = (
    "local_fixture.reversible_draft.v1")
RAW_BROWSER_CONTROL_CONTRACT = "browser.raw_control.v1"
RAW_CONTROL_REQUIRED_ACTION_CLASSES = (
    "WRITE_REVERSIBLE",
    "WRITE_EXTERNAL",
    "PURCHASE/FINANCIAL",
    "ACCOUNT_SECURITY",
)


@dataclass(frozen=True)
class ActionContract:
    """Core-owned semantic binding for a low-level provider plan.

    ``required_action_classes`` is the complete authority ceiling Core and the
    named identity must grant for the contract.  It is deliberately explicit:
    action classes are independent capabilities, not an implied hierarchy.
    """

    id: str
    action_class: str
    loopback_only: bool
    required_action_classes: tuple[str, ...]
    validation: str
    explicit_provider_required: bool
    requires_explicit_origins: bool
    semantic_result: str

    def __post_init__(self):
        if (self.action_class not in ACTION_CLASSES
                or type(self.required_action_classes) is not tuple
                or not self.required_action_classes
                or any(value not in ACTION_CLASSES
                       or value in READ_ACTION_CLASSES
                       for value in self.required_action_classes)
                or len(set(self.required_action_classes))
                    != len(self.required_action_classes)
                or self.action_class not in self.required_action_classes
                or self.validation not in {"reversible_draft", "raw_control"}
                or type(self.explicit_provider_required) is not bool
                or type(self.requires_explicit_origins) is not bool
                or self.semantic_result not in {"contract_bound", "unknown"}):
            raise ValueError("Invalid action operation contract")

    def validate(self, intent):
        if intent.action_class != self.action_class:
            raise PermissionError(
                "Intent action class does not match its operation contract")
        parsed = urlsplit(intent.url)
        if (self.loopback_only
                and (parsed.scheme != "http"
                     or parsed.hostname not in _LOOPBACK_HOSTS)):
            raise PermissionError(
                "Operation contract is limited to loopback fixtures")
        if (self.validation == "raw_control"
                and parsed.scheme != "https"
                and not (parsed.scheme == "http"
                         and parsed.hostname in _LOOPBACK_HOSTS)):
            raise PermissionError(
                "Raw browser control requires HTTPS outside loopback")
        if self.validation == "reversible_draft":
            clicks = [index for index, action in enumerate(intent.actions)
                      if action.tool == "click"]
            mutations = [index for index, action in enumerate(intent.actions)
                         if action.mutating]
            if (len(clicks) != 1 or not any(
                    action.tool == "fill" for action in intent.actions)
                    or clicks[0] != mutations[-1]):
                raise PermissionError(
                    "Reversible draft contract requires fills followed by one save")
        elif (not any(action.tool in _MUTATING_TOOLS
                      for action in intent.actions)
              or any(action.tool not in ACTION_TOOLS
                     for action in intent.actions)):
            raise PermissionError(
                "Raw browser control requires bounded browser actions")

    def validate_origin(self, value):
        try:
            if origin(value) != value:
                raise ValueError()
        except (TypeError, ValueError):
            raise PermissionError(
                "Operation contract requires an exact HTTP(S) origin") from None
        parsed = urlsplit(value)
        if (self.loopback_only
                and (parsed.scheme != "http"
                     or parsed.hostname not in _LOOPBACK_HOSTS)):
            raise PermissionError(
                "Operation contract origin is outside its fixture scope")
        if (self.validation == "raw_control"
                and parsed.scheme != "https"
                and not (parsed.scheme == "http"
                         and parsed.hostname in _LOOPBACK_HOSTS)):
            raise PermissionError(
                "Raw browser control requires HTTPS outside loopback")


ACTION_CONTRACTS = {
    LOCAL_REVERSIBLE_DRAFT_CONTRACT: ActionContract(
        id=LOCAL_REVERSIBLE_DRAFT_CONTRACT,
        action_class="WRITE_REVERSIBLE",
        loopback_only=True,
        required_action_classes=("WRITE_REVERSIBLE",),
        validation="reversible_draft",
        explicit_provider_required=False,
        requires_explicit_origins=False,
        semantic_result="contract_bound"),
    RAW_BROWSER_CONTROL_CONTRACT: ActionContract(
        id=RAW_BROWSER_CONTROL_CONTRACT,
        action_class="WRITE_EXTERNAL",
        loopback_only=False,
        required_action_classes=RAW_CONTROL_REQUIRED_ACTION_CLASSES,
        validation="raw_control",
        explicit_provider_required=True,
        requires_explicit_origins=True,
        semantic_result="unknown"),
}


def require_action_contract(identifier):
    contract = ACTION_CONTRACTS.get(identifier)
    if contract is None:
        raise PermissionError("Action operation contract is unavailable")
    return contract


def _printable(value):
    return (isinstance(value, str) and bool(value)
            and not any(ord(character) < 32 or ord(character) == 127
                        for character in value))


@dataclass(frozen=True)
class BrowserAction:
    """One bounded deterministic browser tool invocation."""

    tool: str
    selector: str
    value: str | None = None
    state: str | None = None

    def __post_init__(self):
        if self.tool not in ACTION_TOOLS or not _printable(self.selector):
            raise ValueError("Invalid browser action")
        if self.tool in {"fill", "assert_text", "assert_value"}:
            if not isinstance(self.value, str) or self.state is not None:
                raise ValueError("Browser action requires a string value")
        elif self.tool == "wait_for":
            if self.value is not None or self.state not in {"attached", "visible"}:
                raise ValueError("Wait action requires an explicit state")
        elif self.value is not None or self.state is not None:
            raise ValueError("Browser action contains unsupported fields")

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict:
            raise ValueError("Browser action must be an object")
        tool = value.get("tool")
        fields = ({"tool", "selector", "value"}
                  if tool in {"fill", "assert_text", "assert_value"}
                  else {"tool", "selector", "state"}
                  if tool == "wait_for" else {"tool", "selector"})
        if set(value) != fields:
            raise ValueError("Browser action schema is invalid")
        return cls(**value)

    @property
    def mutating(self):
        return self.tool in _MUTATING_TOOLS

    def private_dict(self):
        result = {"tool": self.tool, "selector": self.selector}
        if self.value is not None:
            result["value"] = self.value
        if self.state is not None:
            result["state"] = self.state
        return result

    def public_dict(self):
        return {"tool": self.tool, "mutating": self.mutating}


@dataclass(frozen=True)
class WebIntent:
    """Explicit action intent accepted by :meth:`Runtime.do`.

    ``action_class`` has no default: callers must state the authority requested.
    """

    url: str
    contract: str
    action_class: str
    idempotency_key: str
    description: str
    actions: tuple[BrowserAction, ...]

    def __post_init__(self):
        try:
            origin(self.url)
        except (TypeError, ValueError):
            raise ValueError("Intent URL must be HTTP(S) without credentials") from None
        if self.action_class not in ACTION_CLASSES:
            raise ValueError("Intent requires a FrankenSurf action class")
        if self.action_class in READ_ACTION_CLASSES:
            raise ValueError("web.do requires an explicit non-read action class")
        if (not _printable(self.idempotency_key)
                or _IDEMPOTENCY.fullmatch(self.idempotency_key) is None):
            raise ValueError("Invalid idempotency key")
        if not _printable(self.description):
            raise ValueError("Intent description is required")
        if (type(self.actions) is not tuple or not self.actions
                or any(not isinstance(action, BrowserAction)
                       for action in self.actions)):
            raise ValueError("Intent requires typed browser actions")
        if not any(action.mutating for action in self.actions):
            raise ValueError("web.do requires at least one mutating action")
        try:
            require_action_contract(self.contract).validate(self)
        except PermissionError as error:
            raise ValueError(str(error)) from None

    @classmethod
    def from_dict(cls, value):
        required = {"url", "contract", "action_class", "idempotency_key",
                    "description", "actions"}
        if type(value) is not dict or set(value) != required:
            raise ValueError("Action intent schema is invalid")
        actions = value["actions"]
        if not isinstance(actions, list):
            raise ValueError("Intent actions must be a list")
        return cls(value["url"], value["contract"], value["action_class"],
                   value["idempotency_key"], value["description"],
                   tuple(BrowserAction.from_dict(action) for action in actions))

    def validate_policy(self, policy):
        if len(self.actions) > policy.browser_do_max_actions:
            raise ValueError("Intent exceeds the policy action budget")
        if len(self.description.encode()) > policy.browser_do_description_max_bytes:
            raise ValueError("Intent description exceeds the policy byte budget")
        if len(self.idempotency_key.encode()) > policy.browser_do_idempotency_key_max_bytes:
            raise ValueError("Idempotency key exceeds the policy byte budget")
        for action in self.actions:
            if action.tool not in policy.browser_do_allowed_tools:
                raise PermissionError("Intent tool is outside WebPolicy")
            if len(action.selector.encode()) > policy.browser_do_selector_max_bytes:
                raise ValueError("Action selector exceeds the policy byte budget")
            if (action.value is not None
                    and len(action.value.encode())
                        > policy.browser_do_value_max_bytes):
                raise ValueError("Action value exceeds the policy byte budget")

    def private_dict(self):
        return {"url": self.url, "contract": self.contract,
                "action_class": self.action_class,
                "idempotency_key": self.idempotency_key,
                "description": self.description,
                "actions": [action.private_dict() for action in self.actions]}

    def public_plan(self):
        contract = require_action_contract(self.contract)
        return {"contract": self.contract, "description": self.description,
                "required_action_classes": list(
                    contract.required_action_classes),
                "semantic_result": contract.semantic_result,
                "action_count": len(self.actions),
                "actions": [{"index": index, **action.public_dict()}
                            for index, action in enumerate(self.actions)]}

    def fingerprint(self, authority):
        encoded = json.dumps({"intent": self.private_dict(),
            "authority": authority}, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False).encode()
        return hashlib.sha256(encoded).hexdigest()


class JournalFailure(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


class ActionJournal:
    """Append-only, owner-private action state used to fence uncertain replay."""

    schema = "frankensurf.action-journal/v1"

    def __init__(self, state_dir, max_bytes):
        self.directory = Path(state_dir).expanduser() / "actions"
        self.path = self.directory / "journal.jsonl"
        self.max_bytes = max_bytes

    @contextmanager
    def _locked(self):
        descriptor = None
        directory_descriptor = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory_descriptor = os.open(
                self.directory,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0))
            directory_info = os.fstat(directory_descriptor)
            owner_ok = (not hasattr(os, "getuid")
                        or directory_info.st_uid == os.getuid())
            if not stat.S_ISDIR(directory_info.st_mode) or not owner_ok:
                raise OSError()
            os.fchmod(directory_descriptor, 0o700)
            if os.fstat(directory_descriptor).st_mode & 0o077:
                raise OSError()
            descriptor = os.open("journal.jsonl",
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600, dir_fd=directory_descriptor)
            info = os.fstat(descriptor)
            file_owner_ok = (not hasattr(os, "getuid")
                             or info.st_uid == os.getuid())
            if (not stat.S_ISREG(info.st_mode) or not file_owner_ok
                    or info.st_mode & 0o077):
                raise OSError()
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield descriptor
        except OSError:
            raise JournalFailure("EXECUTION_OUTCOME_UNKNOWN",
                "Private action journal is unavailable") from None
        finally:
            if descriptor is not None:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                    os.close(descriptor)
                except OSError:
                    pass
            if directory_descriptor is not None:
                try:
                    os.close(directory_descriptor)
                except OSError:
                    pass

    def _records(self, descriptor):
        size = os.fstat(descriptor).st_size
        if size > self.max_bytes:
            raise JournalFailure("EXECUTION_OUTCOME_UNKNOWN",
                "Action journal exceeds its policy byte budget")
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks = bytearray()
        while len(chunks) < size:
            chunk = os.read(descriptor, size - len(chunks))
            if not chunk:
                break
            chunks.extend(chunk)
        raw = bytes(chunks)
        try:
            rows = [json.loads(line) for line in raw.decode().splitlines()
                    if line]
            if (len(raw) != size
                    or any(not self._valid_record(row) for row in rows)
                    or not self._valid_history(rows)):
                raise ValueError()
            return rows
        except (UnicodeError, ValueError, TypeError):
            raise JournalFailure("EXECUTION_OUTCOME_UNKNOWN",
                "Action journal cannot be reconciled") from None

    @classmethod
    def _valid_record(cls, row):
        if type(row) is not dict or row.get("schema") != cls.schema:
            return False
        state = row.get("state")
        required = {"schema", "recorded_at", "key", "request_fingerprint",
                    "trace_id", "state"}
        if state == "completed":
            required.add("result")
        if (set(row) != required
                or state not in {"executing", "completed",
                                 "failed_before_effect", "uncertain"}
                or re.fullmatch(r"[0-9a-f]{64}", row.get("key", "")) is None
                or re.fullmatch(r"[0-9a-f]{64}",
                                row.get("request_fingerprint", "")) is None
                or re.fullmatch(r"[0-9a-f]{32}",
                                row.get("trace_id", "")) is None
                or state == "completed" and type(row.get("result")) is not dict):
            return False
        try:
            recorded = datetime.fromisoformat(row["recorded_at"])
        except (TypeError, ValueError):
            return False
        return recorded.tzinfo is not None

    @staticmethod
    def _valid_history(rows):
        """Require each idempotency key to follow the journal state machine."""
        latest = {}
        for row in rows:
            key = row["key"]
            previous = latest.get(key)
            state = row["state"]
            if state == "executing":
                if (previous is not None
                        and previous["state"] != "failed_before_effect"):
                    return False
            elif (previous is None or previous["state"] != "executing"
                    or previous["request_fingerprint"]
                        != row["request_fingerprint"]
                    or previous["trace_id"] != row["trace_id"]):
                return False
            latest[key] = row
        return True

    def _append(self, descriptor, row):
        row = {"schema": self.schema,
               "recorded_at": datetime.now(timezone.utc).isoformat(), **row}
        try:
            encoded = (json.dumps(row, sort_keys=True, separators=(",", ":"),
                                  ensure_ascii=False, allow_nan=False) + "\n").encode()
        except (TypeError, ValueError, UnicodeError):
            raise JournalFailure("EXECUTION_OUTCOME_UNKNOWN",
                "Action journal record is invalid") from None
        if os.fstat(descriptor).st_size + len(encoded) > self.max_bytes:
            raise JournalFailure("EXECUTION_OUTCOME_UNKNOWN",
                "Action journal cannot retain this outcome within policy")
        os.lseek(descriptor, 0, os.SEEK_END)
        remaining = memoryview(encoded)
        while remaining:
            written = os.write(descriptor, remaining)
            if written < 1:
                raise JournalFailure("EXECUTION_OUTCOME_UNKNOWN",
                    "Action journal write did not complete")
            remaining = remaining[written:]
        os.fsync(descriptor)

    @staticmethod
    def _key(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def begin(self, idempotency_key, request_fingerprint, trace_id):
        key = self._key(idempotency_key)
        with self._locked() as descriptor:
            rows = self._records(descriptor)
            previous = next((row for row in reversed(rows)
                if row.get("key") == key), None)
            if previous is not None:
                if previous.get("request_fingerprint") != request_fingerprint:
                    raise JournalFailure("POLICY_DENIED",
                        "Idempotency key is bound to a different action request")
                state = previous.get("state")
                if state == "completed":
                    return {"execute": False, "result": previous.get("result")}
                if state in {"executing", "uncertain"}:
                    raise JournalFailure("EXECUTION_OUTCOME_UNKNOWN",
                        "Prior action outcome requires reconciliation before replay")
            self._append(descriptor, {"key": key,
                "request_fingerprint": request_fingerprint,
                "trace_id": trace_id, "state": "executing"})
        return {"execute": True, "result": None}

    def finish(self, idempotency_key, request_fingerprint, trace_id, state,
               result=None):
        if state not in {"completed", "failed_before_effect", "uncertain"}:
            raise ValueError("Invalid journal state")
        key = self._key(idempotency_key)
        with self._locked() as descriptor:
            rows = self._records(descriptor)
            previous = next((row for row in reversed(rows)
                if row.get("key") == key), None)
            if (previous is None or previous.get("state") != "executing"
                    or previous.get("request_fingerprint") != request_fingerprint
                    or previous.get("trace_id") != trace_id):
                raise JournalFailure("EXECUTION_OUTCOME_UNKNOWN",
                    "Action journal state changed before outcome retention")
            row = {"key": key, "request_fingerprint": request_fingerprint,
                   "trace_id": trace_id, "state": state}
            if state == "completed":
                row["result"] = result
            self._append(descriptor, row)
