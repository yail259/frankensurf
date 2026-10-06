#!/usr/bin/env python3
"""Manage FrankenSurf's pinned local helper services without replacing them.

This operator tool deliberately has a small authority surface. It may create or
start the two pinned Docker services owned by FrankenSurf, and it may update the
restart policy of an exact, healthy container. It never stops, removes, renames,
or replaces a container. Browser-agent model serving remains externally managed;
``status`` only checks its configured loopback health endpoint.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


SEARCH_IMAGE = (
    "ghcr.io/searxng/searxng@"
    "sha256:a07a5cd2da2c63d66e559f9e4d3a3db106cfc6c32fb0ac70abe91cc28bcd7350"
)
STEEL_IMAGE = (
    "ghcr.io/steel-dev/steel-browser-api@"
    "sha256:2948a8d8ba1103146dac1c32a121e511955f43976da6905ada0c32fd19b41f59"
)

CONFIG_VERSION = 1
DEFAULT_ENDPOINTS = {
    "search": "http://127.0.0.1:8088",
    "steel": "http://127.0.0.1:3300",
    "browser_agent_model": "http://127.0.0.1:18087",
}
EXEC_ENDPOINT_VARIABLES = {
    "search": "FRANKENSURF_SEARCH_URL",
    "steel": "FRANKENSURF_STEEL_URL",
}


class LifecycleError(RuntimeError):
    """A safe lifecycle action could not be completed."""


@dataclass(frozen=True)
class ContainerSpec:
    identifier: str
    name: str
    image: str
    ports: Mapping[str, tuple[str, str]]
    environment: Mapping[str, str]
    health_url: str
    health_kind: str


SEARCH = ContainerSpec(
    identifier="search",
    name="frankensurf-search",
    image=SEARCH_IMAGE,
    ports={"8080/tcp": ("127.0.0.1", "8088")},
    environment={},
    health_url="http://127.0.0.1:8088/healthz",
    health_kind="searxng",
)
STEEL = ContainerSpec(
    identifier="steel",
    name="frankensurf-steel-local",
    image=STEEL_IMAGE,
    ports={
        "3000/tcp": ("127.0.0.1", "3300"),
        "9223/tcp": ("127.0.0.1", "9323"),
    },
    environment={
        "DOMAIN": "127.0.0.1:3300",
        "CDP_DOMAIN": "127.0.0.1:9323",
        "LOG_STORAGE_ENABLED": "false",
    },
    health_url="http://127.0.0.1:3300/v1/health",
    health_kind="steel",
)
MANAGED_SERVICES = {"search": SEARCH, "steel": STEEL}


Run = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
Probe = Callable[[str, str, float], tuple[bool, str]]


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command), text=True, capture_output=True, check=False
        )
    except FileNotFoundError as exc:
        raise LifecycleError(f"Required command is not installed: {command[0]}") from exc


def _command_ok(result: subprocess.CompletedProcess[str], action: str) -> None:
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise LifecycleError(f"{action} failed: {detail or 'unknown command failure'}")


def _inspect_optional(name: str, run: Run) -> dict[str, Any] | None:
    result = run(("docker", "inspect", name))
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        if "no such object" in detail.lower() or "no such container" in detail.lower():
            return None
        raise LifecycleError(f"Could not inspect Docker container {name}: {detail}")
    try:
        records = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise LifecycleError(f"Docker returned invalid inspect data for {name}") from exc
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        raise LifecycleError(f"Docker returned unexpected inspect data for {name}")
    return records[0]


def _environment(record: Mapping[str, Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in record.get("Config", {}).get("Env", []) or []:
        if isinstance(item, str) and "=" in item:
            key, value = item.split("=", 1)
            values[key] = value
    return values


def _validate_container(spec: ContainerSpec, record: Mapping[str, Any]) -> None:
    actual_image = record.get("Config", {}).get("Image")
    if actual_image != spec.image:
        raise LifecycleError(
            f"{spec.name} uses {actual_image!r}, expected pinned image {spec.image!r}; "
            "refusing to mutate or replace it"
        )
    bindings = record.get("HostConfig", {}).get("PortBindings", {}) or {}
    for container_port, expected in spec.ports.items():
        entries = bindings.get(container_port)
        actual = []
        if isinstance(entries, list):
            actual = [
                (str(entry.get("HostIp", "")), str(entry.get("HostPort", "")))
                for entry in entries
                if isinstance(entry, dict)
            ]
        if actual != [expected]:
            raise LifecycleError(
                f"{spec.name} has {container_port} bindings {actual!r}, expected "
                f"{[expected]!r}; refusing to mutate or replace it"
            )
    actual_environment = _environment(record)
    for key, expected in spec.environment.items():
        if actual_environment.get(key) != expected:
            raise LifecycleError(
                f"{spec.name} has unexpected {key}; refusing to mutate or replace it"
            )


def _state(record: Mapping[str, Any]) -> str:
    return str(record.get("State", {}).get("Status", "unknown"))


def _restart_policy(record: Mapping[str, Any]) -> str:
    return str(
        record.get("HostConfig", {}).get("RestartPolicy", {}).get("Name", "no") or "no"
    )


def _probe(url: str, kind: str, timeout: float) -> tuple[bool, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "frankensurf-health/1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return False, f"HTTP {response.status}"
            raw = response.read(2 * 1024 * 1024)
    except (OSError, urllib.error.URLError) as exc:
        return False, str(exc)
    if kind == "searxng":
        return (raw.strip() == b"OK"), (
            "ok" if raw.strip() == b"OK" else "SearXNG health endpoint did not report OK"
        )
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False, "health response was not JSON"
    if kind == "steel" and payload != {"status": "ok"}:
        return False, "Steel health payload did not report ok"
    if kind == "llama" and payload != {"status": "ok"}:
        return False, "model health payload did not report ok"
    return True, "ok"


def _wait_healthy(spec: ContainerSpec, timeout: float, probe: Probe) -> None:
    deadline = time.monotonic() + timeout
    last_detail = "not checked"
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LifecycleError(
                f"{spec.name} did not become healthy within {timeout:g}s: {last_detail}"
            )
        healthy, last_detail = probe(
            spec.health_url, spec.health_kind, min(5.0, max(0.1, remaining))
        )
        if healthy:
            return
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))


def _preflight_steel(run: Run) -> dict[str, Any] | None:
    primary = _inspect_optional(STEEL.name, run)
    legacy = _inspect_optional("frankensurf-steel", run)
    if primary is not None:
        _validate_container(STEEL, primary)
        if legacy is not None and _state(legacy) == "running":
            raise LifecycleError(
                "Both frankensurf-steel-local and legacy frankensurf-steel are running; "
                "their CDP bindings are ambiguous. Stop the legacy container explicitly "
                "before managing the authoritative local service."
            )
        return primary
    if legacy is not None:
        raise LifecycleError(
            "Legacy container frankensurf-steel exists while authoritative "
            "frankensurf-steel-local is absent. It is not replaced automatically; "
            "inspect and retire or rename the legacy container explicitly first."
        )
    return None


def _preflight(spec: ContainerSpec, run: Run) -> dict[str, Any] | None:
    if spec.identifier == "steel":
        return _preflight_steel(run)
    record = _inspect_optional(spec.name, run)
    if record is not None:
        _validate_container(spec, record)
    return record


def _prepare_search_settings(repo_root: Path) -> Path:
    folder = repo_root / "results" / "searxng-config"
    if folder.exists() and folder.is_symlink():
        raise LifecycleError(f"Search configuration directory may not be a symlink: {folder}")
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder.chmod(0o700)
    settings = folder / "settings.yml"
    if settings.exists():
        info = settings.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise LifecycleError(f"Search settings must be a regular non-symlink file: {settings}")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise LifecycleError(f"Search settings are not owned by the current user: {settings}")
    else:
        content = (
            "use_default_settings: true\n"
            "server:\n"
            f"  secret_key: {secrets.token_hex(32)}\n"
            "  limiter: false\n"
            "  public_instance: false\n"
            "search:\n"
            "  formats:\n"
            "    - html\n"
            "    - json\n"
            "  safe_search: 0\n"
            "outgoing:\n"
            "  request_timeout: 8.0\n"
        )
        descriptor = os.open(
            settings,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    settings.chmod(0o600)
    return folder


def _create_container(spec: ContainerSpec, repo_root: Path, run: Run) -> None:
    pull = run(("docker", "pull", spec.image))
    _command_ok(pull, f"Pulling {spec.image}")
    if spec.identifier == "search":
        settings = _prepare_search_settings(repo_root)
        command = (
            "docker", "run", "-d", "--name", spec.name,
            "--restart", "unless-stopped", "--memory=768m",
            "-p", "127.0.0.1:8088:8080",
            "-v", f"{settings}:/etc/searxng", spec.image,
        )
    else:
        command = (
            "docker", "run", "-d", "--name", spec.name,
            "--restart", "unless-stopped", "--shm-size=1g", "--memory=3g",
            "-p", "127.0.0.1:3300:3000",
            "-p", "127.0.0.1:9323:9223",
            "-e", "DOMAIN=127.0.0.1:3300",
            "-e", "CDP_DOMAIN=127.0.0.1:9323",
            "-e", "DEFAULT_TIMEZONE=Australia/Sydney",
            "-e", "LOG_STORAGE_ENABLED=false",
            "--label", "frankensurf.task=au-access", spec.image,
        )
    created = run(command)
    _command_ok(created, f"Creating {spec.name}")


def start_services(
    identifiers: Sequence[str],
    *,
    repo_root: Path,
    timeout: float,
    run: Run = _run,
    probe: Probe = _probe,
) -> list[dict[str, Any]]:
    """Start exact pinned services, then make healthy containers restart-persistent."""
    specs = [MANAGED_SERVICES[identifier] for identifier in identifiers]
    # Validate the complete requested set before any mutation. A mismatch cannot
    # cause a different service to be started or have its restart policy changed.
    records = {spec.identifier: _preflight(spec, run) for spec in specs}
    results: list[dict[str, Any]] = []
    for spec in specs:
        record = records[spec.identifier]
        created = record is None
        started = False
        if created:
            _create_container(spec, repo_root, run)
            started = True
            restart_policy = "unless-stopped"
        else:
            restart_policy = _restart_policy(record)
            if _state(record) != "running":
                action = run(("docker", "start", spec.name))
                _command_ok(action, f"Starting {spec.name}")
                started = True
        _wait_healthy(spec, timeout, probe)
        restart_changed = False
        if restart_policy != "unless-stopped":
            update = run(("docker", "update", "--restart", "unless-stopped", spec.name))
            _command_ok(update, f"Updating restart policy for {spec.name}")
            restart_changed = True
        results.append(
            {
                "service": spec.identifier,
                "container": spec.name,
                "endpoint": DEFAULT_ENDPOINTS[spec.identifier],
                "healthy": True,
                "created": created,
                "started": started,
                "restart_policy": "unless-stopped",
                "restart_policy_changed": restart_changed,
            }
        )
    return results


def _default_config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return base / "frankensurf" / "services.json"


def _validate_endpoint(value: str, label: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise LifecycleError(f"Invalid {label} endpoint: {exc}") from exc
    if parsed.scheme != "http":
        raise LifecycleError(f"{label} endpoint must use http on loopback")
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise LifecycleError(f"{label} endpoint must use an explicit loopback host")
    if port is None:
        raise LifecycleError(f"{label} endpoint must include an explicit port")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise LifecycleError(f"{label} endpoint may not contain credentials, query, or fragment")
    if parsed.path not in {"", "/"}:
        raise LifecycleError(f"{label} endpoint must be an origin without a path")
    host = f"[{parsed.hostname}]" if parsed.hostname == "::1" else parsed.hostname
    return f"http://{host}:{port}"


def _assert_private_directory(folder: Path, *, create: bool) -> None:
    if folder.exists() or folder.is_symlink():
        info = folder.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise LifecycleError(f"Configuration directory must not be a symlink: {folder}")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise LifecycleError(f"Configuration directory is not owned by this user: {folder}")
    elif create:
        folder.mkdir(parents=True, mode=0o700)
    else:
        raise LifecycleError(f"Configuration directory does not exist: {folder}")
    if create:
        folder.chmod(0o700)
    elif stat.S_IMODE(folder.stat().st_mode) != 0o700:
        raise LifecycleError(f"Configuration directory must have mode 0700: {folder}")


def write_config(path: Path, endpoints: Mapping[str, str]) -> dict[str, Any]:
    expected = set(DEFAULT_ENDPOINTS)
    if set(endpoints) != expected:
        raise LifecycleError("Endpoint configuration has missing or unknown service keys")
    normalized = {
        key: _validate_endpoint(str(endpoints[key]), key) for key in sorted(expected)
    }
    folder = path.parent
    _assert_private_directory(folder, create=True)
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise LifecycleError(f"Configuration file must not be a symlink: {path}")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise LifecycleError(f"Configuration file is not owned by this user: {path}")
    payload = {"version": CONFIG_VERSION, "endpoints": normalized}
    descriptor, temporary_name = tempfile.mkstemp(prefix=".services-", dir=folder)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        directory_descriptor = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()
    return payload


def read_config(path: Path) -> dict[str, Any]:
    _assert_private_directory(path.parent, create=False)
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise LifecycleError(f"Service configuration does not exist: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise LifecycleError(f"Configuration file must not be a symlink: {path}")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise LifecycleError(f"Configuration file is not owned by this user: {path}")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise LifecycleError(f"Configuration file must have mode 0600: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleError(f"Could not read service configuration: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {"version", "endpoints"}:
        raise LifecycleError("Service configuration has unknown or missing fields")
    if payload["version"] != CONFIG_VERSION or not isinstance(payload["endpoints"], dict):
        raise LifecycleError("Unsupported service configuration version or endpoint map")
    endpoints = payload["endpoints"]
    if set(endpoints) != set(DEFAULT_ENDPOINTS):
        raise LifecycleError("Service configuration has unknown or missing endpoint keys")
    payload["endpoints"] = {
        key: _validate_endpoint(str(endpoints[key]), key) for key in sorted(endpoints)
    }
    return payload


def exec_environment(path: Path, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return an environment with only allowlisted endpoint values imported."""
    payload = read_config(path)
    environment = dict(os.environ if base is None else base)
    for key, variable in EXEC_ENDPOINT_VARIABLES.items():
        environment[variable] = payload["endpoints"][key]
    return environment


def _container_status(spec: ContainerSpec, run: Run, probe: Probe) -> dict[str, Any]:
    try:
        record = _inspect_optional(spec.name, run)
        if record is None:
            return {
                "service": spec.identifier,
                "container": spec.name,
                "present": False,
                "healthy": False,
            }
        try:
            _validate_container(spec, record)
            matches = True
            validation_error = None
        except LifecycleError as exc:
            matches = False
            validation_error = str(exc)
        running = _state(record) == "running"
        healthy, detail = (False, "container is not running")
        if matches and running:
            healthy, detail = probe(spec.health_url, spec.health_kind, 5.0)
        result = {
            "service": spec.identifier,
            "container": spec.name,
            "present": True,
            "matches_pinned_definition": matches,
            "state": _state(record),
            "restart_policy": _restart_policy(record),
            "endpoint": DEFAULT_ENDPOINTS[spec.identifier],
            "healthy": healthy,
            "health_detail": detail,
        }
        if validation_error:
            result["validation_error"] = validation_error
        return result
    except LifecycleError as exc:
        return {
            "service": spec.identifier,
            "container": spec.name,
            "present": None,
            "healthy": False,
            "inspection_error": str(exc),
        }


def status(config_path: Path, *, run: Run = _run, probe: Probe = _probe) -> dict[str, Any]:
    """Inspect configuration, containers, and health without mutating anything."""
    try:
        config = read_config(config_path)
        config_error = None
        model_url = config["endpoints"]["browser_agent_model"] + "/health"
    except LifecycleError as exc:
        config = None
        config_error = str(exc)
        model_url = DEFAULT_ENDPOINTS["browser_agent_model"] + "/health"
    model_healthy, model_detail = probe(model_url, "llama", 5.0)
    browser_config = config_path.parent / "browser-agent.json"
    browser_configured = False
    if browser_config.exists() and not browser_config.is_symlink():
        mode = stat.S_IMODE(browser_config.stat().st_mode)
        browser_configured = browser_config.is_file() and mode == 0o600
    return {
        "config": {
            "path": str(config_path),
            "valid": config is not None,
            "error": config_error,
        },
        "managed": [
            _container_status(SEARCH, run, probe),
            _container_status(STEEL, run, probe),
        ],
        "external": [
            {
                "service": "browser_agent_model",
                "endpoint": model_url.removesuffix("/health"),
                "configured_browser_agent": browser_configured,
                "healthy": model_healthy,
                "health_detail": model_detail,
                "restart_persistent": False,
                "managed": False,
            }
        ],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely manage pinned loopback services used by FrankenSurf"
    )
    parser.add_argument(
        "--config", type=Path, default=_default_config_path(),
        help="private endpoint configuration (default: %(default)s)",
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)
    subparsers.add_parser("status", help="read-only service and endpoint health")
    start = subparsers.add_parser("start", help="start exact pinned containers")
    start.add_argument("service", choices=("all", "search", "steel"), default="all", nargs="?")
    start.add_argument("--health-timeout", type=float, default=45.0)
    configure = subparsers.add_parser("configure", help="atomically write private endpoints")
    configure.add_argument("--search-url", default=DEFAULT_ENDPOINTS["search"])
    configure.add_argument("--steel-url", default=DEFAULT_ENDPOINTS["steel"])
    configure.add_argument(
        "--browser-agent-model-url", default=DEFAULT_ENDPOINTS["browser_agent_model"]
    )
    execute = subparsers.add_parser(
        "exec", help="run a command with only allowlisted endpoint variables imported"
    )
    execute.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.operation == "status":
            print(json.dumps(status(arguments.config), indent=2, sort_keys=True))
        elif arguments.operation == "configure":
            payload = write_config(
                arguments.config,
                {
                    "search": arguments.search_url,
                    "steel": arguments.steel_url,
                    "browser_agent_model": arguments.browser_agent_model_url,
                },
            )
            print(
                json.dumps(
                    {"configured": str(arguments.config), "endpoints": payload["endpoints"]},
                    indent=2,
                    sort_keys=True,
                )
            )
        elif arguments.operation == "start":
            identifiers = (
                ("search", "steel") if arguments.service == "all" else (arguments.service,)
            )
            if arguments.health_timeout <= 0:
                raise LifecycleError("health timeout must be positive")
            repo_root = Path(__file__).resolve().parent.parent
            print(
                json.dumps(
                    start_services(
                        identifiers,
                        repo_root=repo_root,
                        timeout=arguments.health_timeout,
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            command = list(arguments.command)
            if command and command[0] == "--":
                command = command[1:]
            if not command:
                raise LifecycleError("exec requires a command after --")
            environment = exec_environment(arguments.config)
            os.execvpe(command[0], command, environment)
    except LifecycleError as exc:
        print(f"local-services: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
