"""Private local identity authority. Credentials and browser profiles stay separate.

Registry data is operator configuration, not browser authentication. Only LOCAL_ONLY
execution is supported in this release. Resolved bindings are private inputs
for the executor; ``status`` is the deliberately smaller agent-facing surface.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import ntpath
import os
import re
import stat
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Iterator, Mapping, Protocol
from urllib.parse import urlparse


_AUTHORITY_MODES = {"LOCAL_ONLY", "SYNC_ALLOWED", "CLOUD_MANAGED", "EPHEMERAL"}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_VAULT_REF = re.compile(r"local:[0-9a-f]{32}\Z")
_IDENTITY_KEYS = {"id", "executor_id", "authority_mode", "health", "revoked", "generation",
                  "domains", "image_domains", "allowed_actions", "auth_check", "vault_refs"}
_IDENTITY_OPTIONAL_KEYS = {"snapshot_policy"}
_EXECUTOR_KEYS = {"id", "endpoint", "provider", "user_data_dir", "profile_directory", "profile_ref",
                  "profile_version", "context_selector", "network_context", "geography", "health", "generation"}


class IdentityFailure(Exception):
    """Typed failure whose message never embeds private registry values."""
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _fail(code: str, message: str):
    raise IdentityFailure(code, message) from None


def _identifier(value, label="identifier") -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        _fail("IDENTITY_CONFIG_INVALID", "Invalid " + label)
    return value


def _integer(value, minimum=1):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _fail("IDENTITY_CONFIG_INVALID", "Invalid registry version")
    return value


def _text(value, maximum=256, optional=False):
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(c) < 32 for c in value):
        _fail("IDENTITY_CONFIG_INVALID", "Invalid identity context")
    return value


def _scope(value):
    value = _text(value, 253).lower().rstrip(".")
    wildcard = value.startswith("*.")
    base = value[2:] if wildcard else value
    if not base or "/" in base or ":" in base or "*" in base or "@" in base:
        _fail("IDENTITY_CONFIG_INVALID", "Invalid domain scope")
    try:
        base = base.encode("idna").decode("ascii")
    except UnicodeError:
        _fail("IDENTITY_CONFIG_INVALID", "Invalid domain scope")
    if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in base.split(".")):
        _fail("IDENTITY_CONFIG_INVALID", "Invalid domain scope")
    return "*." + base if wildcard else base


def _scopes(values, required=False):
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple)) or len(values) > 64:
        _fail("IDENTITY_CONFIG_INVALID", "Invalid domain scopes")
    result = list(dict.fromkeys(_scope(v) for v in values))
    if required and not result:
        _fail("IDENTITY_CONFIG_INVALID", "Identity requires a domain scope")
    return result


def _url_host(url):
    if not isinstance(url, str) or any(ord(c) < 32 for c in url):
        _fail("IDENTITY_DOMAIN_DENIED", "Identity URL is outside its scope")
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        parsed.port
    except ValueError:
        _fail("IDENTITY_DOMAIN_DENIED", "Identity URL is outside its scope")
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        _fail("IDENTITY_DOMAIN_DENIED", "Identity URL is outside its scope")
    try:
        return host.lower().rstrip(".").encode("idna").decode("ascii")
    except UnicodeError:
        _fail("IDENTITY_DOMAIN_DENIED", "Identity URL is outside its scope")


def _matches(host: str, scopes) -> bool:
    return any(host.endswith(s[1:]) and host != s[2:] if s.startswith("*.") else host == s for s in scopes)


def _endpoint(value):
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        _fail("IDENTITY_CONFIG_INVALID", "Executor requires a loopback CDP endpoint")
    try:
        parsed = urlparse(value)
        parsed.port
    except ValueError:
        _fail("IDENTITY_CONFIG_INVALID", "Executor requires a loopback CDP endpoint")
    if (parsed.scheme not in {"http", "https"} or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        _fail("IDENTITY_CONFIG_INVALID", "Executor requires a loopback CDP endpoint")
    return value.rstrip("/")


def _profile_path(value):
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        _fail("IDENTITY_CONFIG_INVALID", "Executor requires an absolute browser profile binding")
    drive, _ = ntpath.splitdrive(value)
    if drive or value.startswith("\\\\"):
        if not re.fullmatch(r"[A-Za-z]:", drive) or not ntpath.isabs(value):
            _fail("IDENTITY_CONFIG_INVALID", "Executor requires a local absolute browser profile binding")
        return ntpath.normcase(ntpath.normpath(value))
    if not Path(value).expanduser().is_absolute() or value.startswith("//"):
        _fail("IDENTITY_CONFIG_INVALID", "Executor requires an absolute browser profile binding")
    return str(Path(value).expanduser().resolve())


def _profile_directory(value):
    if not isinstance(value, str) or not value or value in {".", ".."} or any(c in value for c in "/\\\x00") or len(value) > 128 or any(ord(c) < 32 for c in value):
        _fail("IDENTITY_CONFIG_INVALID", "Invalid browser profile directory")
    return value


def _private_dir(path: Path):
    try:
        if path.is_symlink():
            _fail("IDENTITY_STORE_UNSAFE", "Private identity directory cannot be a symlink")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            _fail("IDENTITY_STORE_UNSAFE", "Private identity directory ownership is invalid")
        path.chmod(0o700)
    except OSError:
        _fail("IDENTITY_STORE_UNSAFE", "Private identity directory is unavailable")


def _open_private(path: Path, flags, *, create=False):
    try:
        fd = os.open(path, flags | os.O_NOFOLLOW | (os.O_CREAT if create else 0), 0o600)
    except OSError:
        _fail("IDENTITY_STORE_UNSAFE", "Private identity file could not be opened safely")
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        _fail("IDENTITY_STORE_UNSAFE", "Private identity file permissions or ownership are invalid")
    return fd


def _atomic_json(path: Path, value):
    _private_dir(path.parent)
    temporary = None
    raw_fd = None
    try:
        raw_fd, temporary = tempfile.mkstemp(prefix=".identity-", dir=path.parent)
        os.fchmod(raw_fd, 0o600)
        stream = os.fdopen(raw_fd, "w")
        raw_fd = None
        with stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink():
            _fail("IDENTITY_STORE_UNSAFE", "Private identity file cannot be a symlink")
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        _fail("IDENTITY_STORE_UNSAFE", "Private identity registry could not be saved")
    finally:
        if raw_fd is not None:
            try:
                os.close(raw_fd)
            except OSError:
                _fail("IDENTITY_STORE_UNSAFE", "Private identity file could not be closed")
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            except OSError:
                _fail("IDENTITY_STORE_UNSAFE", "Private identity temporary file could not be cleaned up")


@dataclass(frozen=True)
class ResolvedIdentity:
    id: str
    generation: int
    executor_id: str
    executor_generation: int
    provider: str
    authority_mode: str
    profile_version: int
    network_context: str
    geography: str | None
    domain_scope: tuple[str, ...]
    image_domain_scope: tuple[str, ...]
    cache_scope: str
    endpoint: str = field(repr=False)
    profile_ref: str = field(repr=False)
    profile_binding: Mapping[str, str] = field(repr=False)
    auth_check: Mapping[str, str] | None = field(repr=False)
    request_url: str = field(repr=False)
    image_request: bool = field(repr=False)
    action: str = field(repr=False)
    registry_ref: str = field(repr=False)
    snapshot_policy: Mapping | None = field(default=None, repr=False)

    @property
    def user_data_dir(self):
        return self.profile_binding["user_data_dir"]

    @property
    def profile_directory(self):
        return self.profile_binding["profile_directory"]

    @property
    def context_selector(self):
        return self.profile_binding["context_selector"]

    @property
    def cdp_url(self):
        return self.endpoint


class IdentityRegistry:
    """Operator-owned registry, reloaded before every authority decision.

    Locks live beside this registry, so distinct Runtime evidence/cache directories
    cannot make simultaneous use of the same identity or canonical browser profile.
    Registry editing is an operator action; no MCP write endpoint is provided.
    """
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or os.environ.get("FRANKENSURF_IDENTITIES") or "~/.frankensurf/identities.json").expanduser().absolute()
        self.lease_dir = self.path.parent / (self.path.name + ".leases")

    def _empty(self):
        return {"version": 1, "identities": {}, "executors": {}}

    def _load(self):
        if not self.path.exists() and not self.path.is_symlink():
            return self._empty()
        fd = _open_private(self.path, os.O_RDONLY)
        try:
            with os.fdopen(fd, "r") as stream:
                raw = stream.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                _fail("IDENTITY_REGISTRY_INVALID", "Identity registry exceeds its size limit")
            data = json.loads(raw)
            self._validate(data)
            return data
        except (json.JSONDecodeError, UnicodeError, TypeError, KeyError, ValueError):
            _fail("IDENTITY_REGISTRY_INVALID", "Identity registry is malformed")
        except OSError:
            _fail("IDENTITY_STORE_UNSAFE", "Private identity registry could not be read")

    def _validate(self, data):
        if not isinstance(data, dict) or set(data) != {"version", "identities", "executors"} or type(data["version"]) is not int or data["version"] != 1:
            _fail("IDENTITY_REGISTRY_INVALID", "Identity registry schema is unsupported")
        for section in ("identities", "executors"):
            if not isinstance(data[section], dict) or len(data[section]) > 1024:
                _fail("IDENTITY_REGISTRY_INVALID", "Identity registry section is invalid")
        for key, executor in data["executors"].items():
            if not isinstance(executor, dict) or set(executor) != _EXECUTOR_KEYS or executor.get("id") != key:
                _fail("IDENTITY_REGISTRY_INVALID", "Executor record is malformed")
            normalized = self._executor_record(key, **{k: v for k, v in executor.items() if k not in {"id", "generation", "provider", "context_selector"}})
            if executor["provider"] != "local_cdp" or executor["context_selector"] != "default":
                _fail("IDENTITY_REGISTRY_INVALID", "Executor backend or context is unsupported")
            _integer(executor["generation"])
            if any(executor[k] != normalized[k] for k in normalized if k != "generation"):
                _fail("IDENTITY_REGISTRY_INVALID", "Executor record is not canonical")
        for key, identity in data["identities"].items():
            if (not isinstance(identity, dict) or not _IDENTITY_KEYS.issubset(identity)
                    or set(identity) - _IDENTITY_KEYS - _IDENTITY_OPTIONAL_KEYS or identity.get("id") != key):
                _fail("IDENTITY_REGISTRY_INVALID", "Identity record is malformed")
            normalized = self._identity_record(key, **{k: v for k, v in identity.items() if k not in {"id", "generation", "revoked"}})
            _integer(identity["generation"])
            if not isinstance(identity["revoked"], bool):
                _fail("IDENTITY_REGISTRY_INVALID", "Identity revocation flag is invalid")
            if any(identity[k] != normalized[k] for k in normalized if k not in {"generation", "revoked"}):
                _fail("IDENTITY_REGISTRY_INVALID", "Identity record is not canonical")
            if identity["executor_id"] not in data["executors"]:
                _fail("IDENTITY_REGISTRY_INVALID", "Identity executor is not registered")

    def _executor_record(self, executor_id, *, endpoint, user_data_dir, profile_directory="Default", profile_ref=None,
                         profile_version=1, network_context="local", geography=None, health="healthy"):
        executor_id = _identifier(executor_id, "executor identifier")
        if health not in {"healthy", "offline", "disabled"}:
            _fail("IDENTITY_CONFIG_INVALID", "Invalid executor health")
        return {"id": executor_id, "endpoint": _endpoint(endpoint), "provider": "local_cdp",
                "user_data_dir": _profile_path(user_data_dir), "profile_directory": _profile_directory(profile_directory),
                "profile_ref": _identifier(profile_ref or executor_id, "profile reference"), "profile_version": _integer(profile_version),
                "context_selector": "default", "network_context": _text(network_context), "geography": _text(geography, optional=True),
                "health": health, "generation": 1}

    def _identity_record(self, identity_id, *, executor_id, domains, image_domains=(), authority_mode="LOCAL_ONLY",
                         health="healthy", allowed_actions=("READ_AUTHENTICATED",), auth_check=None, vault_refs=(), snapshot_policy=None):
        if authority_mode not in _AUTHORITY_MODES or health not in {"healthy", "challenge", "reauth", "disabled"}:
            _fail("IDENTITY_CONFIG_INVALID", "Invalid identity authority or health")
        if isinstance(allowed_actions, str) or not isinstance(allowed_actions, (list, tuple)) or not allowed_actions:
            _fail("IDENTITY_CONFIG_INVALID", "Invalid identity action policy")
        from .actions import validate_action_classes
        try:
            allowed_actions = validate_action_classes(tuple(allowed_actions))
        except ValueError:
            _fail("IDENTITY_CONFIG_INVALID", "Invalid identity action policy")
        if "READ_PUBLIC" in allowed_actions:
            _fail("IDENTITY_CONFIG_INVALID", "Public read authority cannot be assigned to an identity")
        domains = _scopes(domains, required=True)
        image_domains = _scopes(image_domains)
        if auth_check is not None:
            if not isinstance(auth_check, dict) or not set(auth_check).issubset({"url", "authenticated_selector", "login_selector"}):
                _fail("IDENTITY_CONFIG_INVALID", "Invalid authentication check")
            if "url" not in auth_check or "authenticated_selector" not in auth_check:
                _fail("IDENTITY_CONFIG_INVALID", "Authentication check requires a positive selector")
            if not _matches(_url_host(auth_check["url"]), domains):
                _fail("IDENTITY_CONFIG_INVALID", "Authentication check is outside identity scope")
            auth_check = {"url": _text(auth_check["url"], 4096), "authenticated_selector": _text(auth_check["authenticated_selector"], 1024),
                          **({"login_selector": _text(auth_check["login_selector"], 1024)} if auth_check.get("login_selector") is not None else {})}
        if isinstance(vault_refs, str) or not isinstance(vault_refs, (list, tuple)) or any(not isinstance(r, str) or not _VAULT_REF.fullmatch(r) for r in vault_refs):
            _fail("IDENTITY_CONFIG_INVALID", "Invalid opaque vault reference")
        record = {"id": _identifier(identity_id, "identity identifier"), "executor_id": _identifier(executor_id, "executor identifier"),
                "authority_mode": authority_mode, "health": health, "revoked": False, "generation": 1,
                "domains": domains, "image_domains": image_domains, "allowed_actions": list(allowed_actions),
                "auth_check": auth_check, "vault_refs": list(dict.fromkeys(vault_refs))}
        if snapshot_policy is not None:
            if not isinstance(snapshot_policy, dict) or set(snapshot_policy) != {"path_prefixes","root_selectors"}:
                _fail("IDENTITY_CONFIG_INVALID", "Invalid visible snapshot policy")
            prefixes, roots = snapshot_policy["path_prefixes"], snapshot_policy["root_selectors"]
            if (not isinstance(prefixes, (list, tuple)) or not prefixes or
                    any(not isinstance(p, str) or not p.startswith("/") or not p.endswith("/") or
                        any(part in p for part in ("//", "..", "%", "?", "#")) for p in prefixes)):
                _fail("IDENTITY_CONFIG_INVALID", "Visible snapshot requires explicit path prefixes")
            if not isinstance(roots, (list, tuple)) or not roots:
                _fail("IDENTITY_CONFIG_INVALID", "Visible snapshot requires explicit region selectors")
            record["snapshot_policy"] = {"path_prefixes":list(dict.fromkeys(prefixes)),
                                         "root_selectors":list(dict.fromkeys(_text(root, 1024) for root in roots))}
        return record

    @contextmanager
    def _mutation(self):
        _private_dir(self.path.parent)
        fd = _open_private(self.path.parent / (self.path.name + ".lock"), os.O_RDWR, create=True)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            data = self._load()
            yield data
            self._validate(data)
            _atomic_json(self.path, data)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _replace(self, section, record):
        with self._mutation() as data:
            old = data[section].get(record["id"])
            if old:
                record["generation"] = old["generation"]
                if record != old:
                    record["generation"] += 1
            data[section][record["id"]] = record
        return self._safe_identity(record) if section == "identities" else self._safe_executor(record)

    def enroll_executor(self, executor_id, **options):
        return self._replace("executors", self._executor_record(executor_id, **options))

    def enroll_identity(self, identity_id, **options):
        record = self._identity_record(identity_id, **options)
        with self._mutation() as data:
            if record["executor_id"] not in data["executors"]:
                _fail("IDENTITY_EXECUTOR_UNKNOWN", "Identity executor is not registered")
            old = data["identities"].get(identity_id)
            if old:
                record["generation"] = old["generation"] + (record != {**old, "generation": 1})
            data["identities"][identity_id] = record
        return self._safe_identity(record)

    def revoke(self, identity_id):
        with self._mutation() as data:
            identity = data["identities"].get(identity_id)
            if not identity:
                _fail("IDENTITY_UNKNOWN", "Identity is not registered")
            if not identity["revoked"] or identity["health"] != "disabled":
                identity["revoked"], identity["health"] = True, "disabled"
                identity["generation"] += 1
        return self._safe_identity(identity)

    disable = revoke

    def set_executor_health(self, executor_id, health):
        if health not in {"healthy", "offline", "disabled"}:
            _fail("IDENTITY_CONFIG_INVALID", "Invalid executor health")
        with self._mutation() as data:
            executor = data["executors"].get(executor_id)
            if not executor:
                _fail("IDENTITY_EXECUTOR_UNKNOWN", "Executor is not registered")
            if executor["health"] != health:
                executor["health"], executor["generation"] = health, executor["generation"] + 1
        return self._safe_executor(executor)

    def set_identity_health(self, identity_id, health):
        if health not in {"healthy", "challenge", "reauth", "disabled"}:
            _fail("IDENTITY_CONFIG_INVALID", "Invalid identity health")
        with self._mutation() as data:
            identity = data["identities"].get(identity_id)
            if not identity:
                _fail("IDENTITY_UNKNOWN", "Identity is not registered")
            if identity["health"] != health:
                identity["health"], identity["generation"] = health, identity["generation"] + 1
        return self._safe_identity(identity)

    @staticmethod
    def _safe_identity(identity):
        return {k: identity[k] for k in ("id", "executor_id", "authority_mode", "health", "revoked", "generation", "domains", "image_domains", "allowed_actions")} | {
            "auth_check_configured": identity["auth_check"] is not None, "vault_reference_count": len(identity["vault_refs"]),
            "read_mode":"owner_visible_snapshot" if identity.get("snapshot_policy") else "passive_read"}

    @staticmethod
    def _safe_executor(executor):
        return {k: executor[k] for k in ("id", "health", "generation", "profile_version", "network_context", "geography")}

    def status(self, identity_id=None):
        data = self._load()
        identities = list(data["identities"].values())
        executors = list(data["executors"].values())
        if identity_id is not None:
            if identity_id not in data["identities"]:
                _fail("IDENTITY_UNKNOWN", "Identity is not registered")
            identities = [data["identities"][identity_id]]
            executors = [data["executors"][identities[0]["executor_id"]]]
        return {"version": 1, "supported_authority_modes": ["LOCAL_ONLY"], "identity_count": len(identities), "executor_count": len(executors),
                "identities": [self._safe_identity(i) for i in identities], "executors": [self._safe_executor(e) for e in executors]}

    def resolve(self, identity_id, url, *, provider=None, action="READ_AUTHENTICATED", allow_local_browser=True, image=False):
        data = self._load()
        identity = data["identities"].get(identity_id)
        if not identity:
            _fail("IDENTITY_UNKNOWN", "Identity is not registered")
        if identity["revoked"] or identity["health"] == "disabled":
            _fail("IDENTITY_REVOKED", "Identity is disabled or revoked")
        if identity["authority_mode"] != "LOCAL_ONLY":
            _fail("IDENTITY_AUTHORITY_UNSUPPORTED", "Only local identity authority is implemented")
        if not allow_local_browser:
            _fail("IDENTITY_POLICY_DENIED", "Identity requires local browser execution")
        if provider not in {None, "local_cdp"}:
            _fail("IDENTITY_PROVIDER_DENIED", "Identity cannot be routed to this provider")
        if action not in identity["allowed_actions"]:
            _fail("IDENTITY_ACTION_DENIED", "Identity does not authorize this action")
        scopes = identity["domains"] + (identity["image_domains"] if image else [])
        if not _matches(_url_host(url), scopes):
            _fail("IDENTITY_DOMAIN_DENIED", "URL is outside identity scope")
        if identity["health"] == "reauth":
            _fail("IDENTITY_REAUTH_REQUIRED", "Identity requires manual reauthentication")
        if identity["health"] == "challenge":
            _fail("CAPTCHA", "Identity requires supervised authentication")
        executor = data["executors"][identity["executor_id"]]
        if executor["health"] == "offline":
            _fail("IDENTITY_EXECUTOR_OFFLINE", "Local identity executor is offline")
        if executor["health"] == "disabled":
            _fail("IDENTITY_EXECUTOR_DISABLED", "Local identity executor is disabled")
        cache_scope = hashlib.sha256(json.dumps([identity, executor], sort_keys=True).encode()).hexdigest()
        binding = MappingProxyType({k: executor[k] for k in ("user_data_dir", "profile_directory", "context_selector")})
        return ResolvedIdentity(id=identity["id"], generation=identity["generation"], executor_id=executor["id"],
            executor_generation=executor["generation"], provider="local_cdp", authority_mode=identity["authority_mode"],
            profile_version=executor["profile_version"], network_context=executor["network_context"], geography=executor["geography"],
            domain_scope=tuple(identity["domains"]), image_domain_scope=tuple(identity["image_domains"]), cache_scope=cache_scope,
            endpoint=executor["endpoint"], profile_ref=executor["profile_ref"], profile_binding=binding,
            auth_check=MappingProxyType(identity["auth_check"]) if identity["auth_check"] else None,
            request_url=url, image_request=image, action=action, registry_ref=str(self.path),
            snapshot_policy=MappingProxyType({k:tuple(v) for k,v in identity["snapshot_policy"].items()}) if identity.get("snapshot_policy") else None)

    def recheck(self, resolved: ResolvedIdentity, url=None, *, image=None):
        if resolved.registry_ref != str(self.path):
            _fail("IDENTITY_CHANGED", "Identity authority source changed")
        current = self.resolve(resolved.id, resolved.request_url if url is None else url, provider=resolved.provider,
                               action=resolved.action,
                               image=resolved.image_request if image is None else image)
        if current.cache_scope != resolved.cache_scope:
            _fail("IDENTITY_CHANGED", "Identity configuration changed during execution")
        return current

    revalidate = recheck

    def permits_url(self, resolved: ResolvedIdentity, url, *, image=False):
        self.recheck(resolved, url, image=image)
        return True

    @contextmanager
    def lease(self, resolved: ResolvedIdentity) -> Iterator[ResolvedIdentity]:
        self.recheck(resolved)
        _private_dir(self.lease_dir)
        keys = ["identity:" + resolved.id, "endpoint:" + resolved.endpoint,
                "profile:" + resolved.user_data_dir + "\0" + resolved.profile_directory]
        fds = []
        try:
            for key in sorted(keys):
                file = self.lease_dir / (hashlib.sha256(key.encode()).hexdigest() + ".lock")
                fd = _open_private(file, os.O_RDWR, create=True)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(fd)
                    _fail("IDENTITY_IN_USE", "Identity or canonical browser profile is already in use")
                fds.append(fd)
            self.recheck(resolved)
            yield resolved
        finally:
            for fd in reversed(fds):
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)


class CredentialVault(Protocol):
    """Opaque credential references; browser cookies/profile state never belong here."""
    def put(self, value: bytes | str) -> str: ...
    def get(self, reference: str) -> bytes: ...
    def delete(self, reference: str) -> None: ...


class LocalSecretVault:
    """Private local secret store separate from both registry and Chrome profile.

    This backend relies on OS account/file permissions, not encryption at rest.
    There is deliberately no list/export API. Only authorized executor code should
    call ``get``; identity resolution never loads secret bytes.
    """
    def __init__(self, directory: str | Path):
        self.directory = Path(directory).expanduser().absolute()

    def _file(self, reference):
        if not isinstance(reference, str) or not _VAULT_REF.fullmatch(reference):
            _fail("VAULT_REFERENCE_INVALID", "Credential reference is invalid")
        return self.directory / (reference[6:] + ".secret")

    def put(self, value):
        if isinstance(value, str):
            value = value.encode()
        if not isinstance(value, bytes) or not value or len(value) > 1024 * 1024:
            _fail("VAULT_VALUE_INVALID", "Credential value is invalid")
        _private_dir(self.directory)
        reference = "local:" + uuid.uuid4().hex
        fd = _open_private(self._file(reference), os.O_WRONLY | os.O_EXCL, create=True)
        with os.fdopen(fd, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        return reference

    def get(self, reference):
        file = self._file(reference)
        if not file.exists():
            _fail("VAULT_REFERENCE_MISSING", "Credential reference is unavailable")
        fd = _open_private(file, os.O_RDONLY)
        with os.fdopen(fd, "rb") as stream:
            value = stream.read(1024 * 1024 + 1)
        if len(value) > 1024 * 1024:
            _fail("VAULT_VALUE_INVALID", "Credential value exceeds its size limit")
        return value

    def delete(self, reference):
        file = self._file(reference)
        if not file.exists():
            return
        fd = _open_private(file, os.O_RDONLY)
        os.close(fd)
        file.unlink()
