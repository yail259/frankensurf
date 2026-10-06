"""Private operator configuration; never exposed through agent tools or receipts."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit, urlunsplit

SDK_VERSION = "0.13.10"
READ_ACTIONS = frozenset({"navigate_public", "inspect_page", "follow_link", "click_element", "search_site", "wait_readiness", "scroll_page", "done"})
CONFIG_FIELDS = frozenset({"factory", "billing", "revision", "python", "browser"})
ENV_FIELDS = {"factory": "FRANKENSURF_BROWSER_USE_MODEL_FACTORY", "billing": "FRANKENSURF_BROWSER_USE_BILLING",
              "revision": "FRANKENSURF_BROWSER_USE_BINDING_REVISION", "python": "FRANKENSURF_BROWSER_USE_PYTHON",
              "browser": "FRANKENSURF_BROWSER_USE_BROWSER"}


class ConfigurationError(ValueError):
    """Only fixed error messages cross the provider boundary."""


@dataclass(frozen=True, repr=False)
class BindingSnapshot:
    factory: Path
    callable_name: str
    factory_bytes: bytes
    billing: str
    revision: str
    python: Path
    browser: Path
    fingerprint: str


def origin(url):
    parsed = urlsplit(url)
    parsed.port
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Invalid public URL")
    return parsed.scheme + "://" + parsed.netloc.lower()


def subject_url(url):
    origin(url)
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path or "/", parsed.query, parsed.fragment))


def config_path():
    return Path.home() / ".config/frankensurf/browser-agent.json"


def _private_config():
    path = config_path()
    # An unsafe existing file never silently turns into environment-only config.
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        return {}
    except OSError:
        raise ConfigurationError("Operator configuration unavailable") from None
    try:
        for item in (path.parent.parent, path.parent):
            item_stat = item.lstat()
            if (not stat.S_ISDIR(item_stat.st_mode) or item_stat.st_uid != os.getuid()
                    or item_stat.st_mode & (0o077 if item == path.parent else 0o022)):
                raise ConfigurationError("Operator configuration permissions invalid")
        if (not stat.S_ISREG(file_stat.st_mode) or file_stat.st_uid != os.getuid()
                or file_stat.st_mode & 0o077):
            raise ConfigurationError("Operator configuration permissions invalid")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            observed = os.fstat(stream.fileno())
            if (observed.st_ino, observed.st_dev) != (file_stat.st_ino, file_stat.st_dev):
                raise ConfigurationError("Operator configuration changed")
            def unique_pairs(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ConfigurationError("Operator configuration schema invalid")
                    result[key] = value
                return result
            value = json.load(stream, object_pairs_hook=unique_pairs)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ConfigurationError("Operator configuration unavailable") from None
    if (not isinstance(value, dict) or set(value) - CONFIG_FIELDS
            or not {"factory", "billing", "revision"} <= set(value)
            or any(not isinstance(item, str) or not item for item in value.values())):
        raise ConfigurationError("Operator configuration schema invalid")
    return value


def settings():
    value = _private_config()
    for field, variable in ENV_FIELDS.items():
        if variable in os.environ:
            value[field] = os.environ[variable]
    return value


def _absolute_file(value):
    candidate = Path(value).expanduser()
    if not candidate.is_absolute() or not candidate.is_file():
        raise ConfigurationError("Operator dependency unavailable")
    return candidate


def python_path():
    return Path(settings().get("python", str(Path.home() / ".local/share/frankensurf/agent-venv/bin/python"))).expanduser()


def factory_spec():
    try:
        return _factory_spec(settings().get("factory", ""))
    except ConfigurationError:
        return None


def _factory_spec(value):
    path, separator, name = value.rpartition(":")
    if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name):
        raise ConfigurationError("Model factory unavailable")
    source = _absolute_file(path)
    if source.is_symlink():
        raise ConfigurationError("Model factory symlink forbidden")
    return source.resolve(), name


def billing():
    try:
        value = settings().get("billing")
        return value if value in {"paid", "unmetered"} else None
    except ConfigurationError:
        return None


def browser_path():
    value = settings().get("browser")
    if value:
        try:
            return _absolute_file(value).resolve()
        except ConfigurationError:
            return None
    candidates = sorted((Path.home() / ".cache/ms-playwright").glob("chromium-*/chrome-linux*/chrome"))
    return next((item.resolve() for item in reversed(candidates) if item.is_file()), None)


def _installed(executable):
    # Resolving its Python symlink would lose the isolated virtualenv root.
    if not executable.is_absolute() or not executable.is_file():
        return False
    root = executable.parent.parent
    sites = list((root / "lib").glob("python*/site-packages")) + [root / "Lib/site-packages"]
    return any((site / ("browser_use-" + SDK_VERSION + ".dist-info")).is_dir() for site in sites)


def installed():
    try:
        return _installed(python_path())
    except ConfigurationError:
        return False


def snapshot():
    if os.name != "posix":
        raise ConfigurationError("Owned POSIX process group required")
    value = settings()
    source, name = _factory_spec(value.get("factory", ""))
    metadata = value.get("billing")
    revision = value.get("revision", "")
    if metadata not in {"paid", "unmetered"} or not isinstance(revision, str):
        raise ConfigurationError("Model binding metadata invalid")
    interpreter = _absolute_file(value.get("python", str(Path.home() / ".local/share/frankensurf/agent-venv/bin/python")))
    interpreter = interpreter.parent.resolve() / interpreter.name
    if not _installed(interpreter):
        raise ConfigurationError("Pinned isolated SDK unavailable")
    configured_browser = value.get("browser")
    if configured_browser:
        executable = _absolute_file(configured_browser).resolve()
    else:
        candidates = sorted((Path.home() / ".cache/ms-playwright").glob("chromium-*/chrome-linux*/chrome"))
        executable = next((item.resolve() for item in reversed(candidates) if item.is_file()), None)
    if executable is None:
        raise ConfigurationError("Owned local browser unavailable")
    try:
        descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            source_bytes = stream.read()
    except OSError:
        raise ConfigurationError("Model factory unavailable") from None
    provenance = {"sdk": SDK_VERSION, "factory": str(source), "callable": name,
                  "factory_sha256": hashlib.sha256(source_bytes).hexdigest(),
                  "billing": metadata, "revision": revision, "python": str(interpreter), "browser": str(executable)}
    fingerprint = hashlib.sha256(json.dumps(provenance, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return BindingSnapshot(source, name, source_bytes, metadata, revision, interpreter, executable, fingerprint)


def configured():
    try:
        snapshot()
        return True
    except (ConfigurationError, OSError, ValueError):
        return False


def manifest_metadata():
    try:
        binding = snapshot()
        return SDK_VERSION + "+" + binding.fingerprint, binding.billing == "paid"
    except (ConfigurationError, OSError, ValueError):
        return SDK_VERSION + "+unconfigured", False


def manifest_version():
    return manifest_metadata()[0]
